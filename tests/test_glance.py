from __future__ import annotations

import json
from pathlib import Path

import pytest

from glance import llm
from glance.analyze import analyze
from glance.cli import export_html, main
from glance.extractors import extract
from glance.extractors.sql import parse_ops
from glance.git import collect
from glance.render_md import render

from fixtures import make_python_repo, make_ts_repo


def rules(report):
    return {r["rule"] for r in report["risks"]}


def change(report, kind, name):
    return next(c for c in report["changes"] if c["kind"] == kind and c["name"] == name)


@pytest.fixture(scope="module")
def py_report(tmp_path_factory):
    repo = make_python_repo(tmp_path_factory.mktemp("py"))
    return analyze(collect(repo))


@pytest.fixture(scope="module")
def ts_report(tmp_path_factory):
    repo = make_ts_repo(tmp_path_factory.mktemp("ts"))
    return analyze(collect(repo))


# ------------------------------------------------------------------ end to end: FastAPI

def test_python_structural_changes(py_report):
    loan = change(py_report, "table", "Loan")
    fields = {f["name"]: f["change"] for f in loan["field_changes"]}
    assert fields == {"notes": "removed", "interest_rate": "added", "margin_call_at": "added"}

    mig = change(py_report, "migration", "0002")
    ops = [(o["op"], o["column"]) for o in mig["after"]["meta"]["upgrade"]]
    assert ops == [("add_column", "interest_rate"), ("add_column", "margin_call_at"), ("drop_column", "notes")]

    route = change(py_report, "route", "POST /loans/{loan_id}/margin-check")
    assert route["status"] == "added" and route["after"]["meta"]["dependencies"] == []

    rate = change(py_report, "function", "current_rate")
    assert rate["signature_changed"] and "term_months" in rate["after"]["signature"]
    assert change(py_report, "config", "MAINTENANCE_MARGIN")["after"]["signature"] == "0.25"


def test_python_risks(py_report):
    got = rules(py_report)
    for expected in ("not-null-no-default", "destructive-migration", "irreversible-migration", "removed-still-used",
                     "task-signature", "route-no-auth", "business-constant", "new-env", "required-field",
                     "swallowed", "money-float", "skipped-test", "new-dependency", "debug"):
        assert expected in got, expected
    # response models don't get "new required field" flags
    assert not any(r["rule"] == "required-field" and "LoanOut" in r["title"] for r in py_report["risks"])


def test_reading_order_puts_risky_files_first(py_report):
    order = py_report["reading_order"]
    assert order[0] == "alembic/versions/0002_margin_calls.py"
    assert order.index("tests/test_loans.py") > order.index("app/api/loans.py")


# ------------------------------------------------------------------ end to end: Node / TS

def test_ts_structural_changes(ts_report):
    order = change(ts_report, "table", "Order")
    assert {f["name"] for f in order["field_changes"]} == {"currency", "refundedAt"}
    zod = change(ts_report, "model", "OrderInput")
    assert {(f["name"], f["change"]) for f in zod["field_changes"]} == {("coupon", "removed"), ("currency", "added")}
    assert change(ts_report, "route", "POST /orders/:id/refund")["status"] == "removed"
    assert change(ts_report, "route", "DELETE /orders/:id")["status"] == "added"
    wf = change(ts_report, "task", "CheckoutWorkflow.checkout")
    assert wf["framework"] == "dbos"


def test_ts_risks(ts_report):
    got = rules(ts_report)
    for expected in ("schema-no-migration", "route-removed", "route-no-auth", "workflow-signature", "workflow-body",
                     "workflow-nondeterminism", "enum-value-removed", "secret", "money-float", "swallowed",
                     "tests-removed", "new-dependency", "business-constant", "required-field", "field-removed"):
        assert expected in got, expected


def test_every_risk_points_somewhere(py_report, ts_report):
    for rep in (py_report, ts_report):
        hunk_ids = {h["id"] for f in rep["files"] for h in f["hunks"]}
        for r in rep["risks"]:
            assert set(r["hunk_ids"]) <= hunk_ids
        for c in rep["changes"]:
            assert set(c["hunk_ids"]) <= hunk_ids


# ------------------------------------------------------------------ extractor units

def test_sql_ops():
    ops = parse_ops('''
        ALTER TABLE "Order" ADD COLUMN "currency" TEXT NOT NULL, DROP COLUMN "coupon";
        CREATE INDEX "o_idx" ON "Order"("userId");
        DROP TABLE legacy;
        UPDATE accounts SET tier = 'gold' WHERE equity > 100000;
    ''')
    assert [(o["op"], o["table"], o["column"]) for o in ops] == [
        ("add_column", "Order", "currency"), ("drop_column", "Order", "coupon"),
        ("create_index", "Order", None), ("drop_table", "legacy", None), ("update_rows", "accounts", None)]


def test_fastapi_router_prefix_and_deps():
    src = '''
from fastapi import APIRouter, Depends, Security
router = APIRouter(prefix="/v1/users")
@router.get("/{uid}", response_model=UserOut)
def get_user(uid: int, me: User = Security(require_scope)): ...
'''
    route = next(e for e in extract("api.py", src, "python", "source") if e.kind == "route")
    assert route.name == "GET /v1/users/{uid}"
    assert route.meta["response_model"] == "UserOut"
    assert route.meta["dependencies"] == ["Security(require_scope)"]


def test_nest_trpc_next():
    nest = '''
@Controller("accounts")
@UseGuards(JwtGuard)
export class AccountsController {
  @Post(":id/close")
  close(@Param("id") id: string) { return 1 }
}
'''
    r = [e for e in extract("src/a.controller.ts", nest, "typescript", "source") if e.kind == "route"]
    assert r[0].name == "POST /accounts/:id/close" and "UseGuards(JwtGuard)" in r[0].meta["dependencies"]

    trpc = 'export const appRouter = t.router({ createLoan: protectedProcedure.input(LoanIn).mutation(async () => 1) });'
    r = [e for e in extract("src/router.ts", trpc, "typescript", "source") if e.kind == "route"]
    assert r[0].name == "trpc mutation appRouter.createLoan" and r[0].fields["input"] == "LoanIn"

    nxt = "export async function POST(req: Request) { return Response.json({}) }"
    r = [e for e in extract("app/(api)/loans/[id]/route.ts", nxt, "typescript", "source") if e.kind == "route"]
    assert r[0].name == "POST /loans/:id"


def test_prisma_and_drizzle():
    prisma = 'model Loan {\n  id Int @id\n  rate Decimal @db.Decimal(6,4)\n  @@map("loans")\n}\n'
    e = extract("prisma/schema.prisma", prisma, "prisma", "source")[0]
    assert e.kind == "table" and e.meta["tablename"] == "loans" and e.fields["rate"].startswith("Decimal")
    drizzle = 'export const loans = pgTable("loans", { id: serial("id").primaryKey(), rate: numeric("rate") });'
    e = [x for x in extract("src/db/schema.ts", drizzle, "typescript", "source") if x.kind == "table"][0]
    assert e.meta["tablename"] == "loans" and set(e.fields) == {"id", "rate"}


# ------------------------------------------------------------------ model pass grounding

def test_grounding_drops_uncited_items():
    data = {
        "summary": "x",
        "business_logic": [{"title": "ok", "hunks": ["h_real"]}, {"title": "made up", "hunks": ["h_fake"]}, {"title": "none"}],
        "concerns": [{"title": "c", "severity": "critical", "hunks": ["h_real", "h_fake"]}],
        "walkthrough": [],
        "intent": {"asked": "a", "done": [], "extra": ["plain string"], "missing": []},
    }
    out, dropped = llm.ground(data, {"h_real"})
    assert [b["title"] for b in out["business_logic"]] == ["ok"]
    assert out["concerns"][0]["hunks"] == ["h_real"] and out["concerns"][0]["severity"] == "medium"
    assert out["intent"]["extra"][0] == {"text": "plain string", "hunks": []}
    assert dropped == 2


def test_prompt_contains_hunk_ids(py_report):
    prompt = llm.build_prompt(py_report)
    first = py_report["files"][0]["hunks"][0]["id"]
    assert f"[{first}]" in prompt and "Return ONLY a JSON object" in prompt


# ------------------------------------------------------------------ outputs

def test_markdown_and_html(py_report):
    md = render(py_report)
    assert "## Data layer" in md and "loans.interest_rate" in md
    html = export_html(py_report)
    assert "window.__GLANCE_REPORT__" in html and "</script>" in html
    embedded = html.split("window.__GLANCE_REPORT__ = ", 1)[1].split(";</script>", 1)[0]
    assert json.loads(embedded)["repo"] == py_report["repo"]


def test_cli_json(tmp_path, capsys):
    repo = make_python_repo(tmp_path)
    assert main(["--repo", str(repo), "--format", "json", "--llm", "none"]) == 0
    path = Path(capsys.readouterr().out.strip())
    report = json.loads(path.read_text())
    assert report["llm"]["status"] == "skipped"
    assert ".glance/" in (repo / ".git" / "info" / "exclude").read_text()
