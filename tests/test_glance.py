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
    assert "## Database (tables, migrations)" in md and "## Schemas & types" in md and "loans.interest_rate" in md
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


def test_open_in_editor_endpoint(tmp_path, monkeypatch):
    import http.client
    import json as _json
    import threading
    from http.server import ThreadingHTTPServer

    from glance.server import State, make_handler

    (tmp_path / "a.py").write_text("x = 1\n")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(State({"repo_path": str(tmp_path)})))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def post(body, origin=None):
        c = http.client.HTTPConnection("127.0.0.1", port)
        headers = {"Content-Type": "application/json", "Host": f"127.0.0.1:{port}"}
        if origin:
            headers["Origin"] = origin
        c.request("POST", "/api/open", _json.dumps(body), headers)
        return c.getresponse().status

    try:
        monkeypatch.setenv("GLANCE_EDITOR", "definitely-not-an-editor-xyz")
        assert post({"path": "../etc/passwd", "line": 1}) == 400
        assert post({"path": "/etc/passwd", "line": 1}) == 400
        assert post({"path": "missing.py"}) == 404
        assert post({"path": "a.py", "line": 1}, origin="https://evil.example") == 403
        assert post({"path": "a.py", "line": 1}) == 501          # valid request, no editor CLI
    finally:
        srv.shutdown()


def test_topics_and_layers():
    from glance.analyze import data_layer
    from glance.topics import assign_topics, words

    assert words("SettleSubscriptionChargesWorkflow") == ["settle", "subscription", "charge"]
    ch = lambda i, kind, name, file, fw="": {"id": str(i), "kind": kind, "name": name, "file": file, "framework": fw, "field_changes": []}
    changes = [
        ch(1, "table", "Subscription", "db/schema.prisma", "prisma"),
        ch(2, "table", "SubscriptionCharge", "db/schema.prisma", "prisma"),
        ch(3, "function", "createDueSubscriptionCharges", "jobs/billing.ts"),
        ch(4, "function", "loanInAccountId", "alpaca/firmAccounts.ts"),
        ch(5, "function", "addLoanImpl", "controllers/loanController.ts"),
        ch(6, "function", "payBackLoanImpl", "controllers/loanController.ts"),
        ch(7, "function", "formatDate", "utils/format.ts"),
    ]
    files = [{"path": p} for p in sorted({c["file"] for c in changes})]
    topics = assign_topics(changes, files, [{"file": "controllers/loanController.ts"}])
    by = {c["name"]: c["topic"] for c in changes}
    assert by["Subscription"] == by["createDueSubscriptionCharges"] == "subscription"
    assert by["loanInAccountId"] == by["addLoanImpl"] == "loan"
    assert by["formatDate"] == "other"                      # nothing shared: no one-off topics
    assert [t["id"] for t in topics][-1] == "other"

    assert data_layer({"kind": "table", "file": "x.py", "framework": "sqlalchemy"}) == "database"
    assert data_layer({"kind": "type", "file": "db/schema.prisma", "framework": "prisma"}) == "database"   # Prisma enum
    assert data_layer({"kind": "model", "file": "app/schemas.py", "framework": "pydantic"}) == "schema"
    assert data_layer({"kind": "type", "file": "src/types.ts", "framework": ""}) == "schema"
    assert data_layer({"kind": "function", "file": "a.py", "framework": ""}) is None


def test_untracked_nested_repo_is_skipped(tmp_path):
    import subprocess

    from glance.git import collect

    run = lambda *a, cwd=tmp_path: subprocess.run(a, cwd=cwd, check=True, capture_output=True)
    run("git", "init", "-q", "-b", "main")
    (tmp_path / "a.py").write_text("x = 1\n")
    run("git", "add", "-A"); run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    nested = tmp_path / "worktrees" / "other"
    nested.mkdir(parents=True)
    run("git", "init", "-q", cwd=nested)
    (nested / "b.py").write_text("y = 2\n")
    (tmp_path / "new.py").write_text("z = 3\n")
    paths = [f.path for f in collect(tmp_path, base="main").files]
    assert "new.py" in paths and not any(p.startswith("worktrees") for p in paths)


def test_story_chapters(py_report, ts_report):
    for rep in (py_report, ts_report):
        st = rep["story"]
        assert [c["id"] for c in st["chapters"]] == ["goal", "database", "routes", "schemas", "background", "supporting"]
        units = {u["id"]: u for u in st["units"]}
        assert sorted(i for c in st["chapters"] for i in c["unit_ids"]) == sorted(units)   # every unit in one chapter
        assert not any("test" in u["file"].split("/")[0] or ".test." in u["file"] for u in units.values())
        for c in st["chapters"]:                                                          # new → changed → removed
            order = [{"added": 0, "modified": 1, "removed": 2}[units[i]["status"]] for i in c["unit_ids"]]
            assert order == sorted(order) or c["id"] == "schemas"                         # schemas: route-used first
    py = {u["chapter"] for u in py_report["story"]["units"]}
    assert {"database", "routes", "schemas"} <= py
    ts_bg = [u for u in ts_report["story"]["units"] if u["chapter"] == "background"]
    assert ts_bg, "DBOS workflows should land in background"


def test_story_grounding_drops_unknown_units():
    from glance.llm import ground_story

    out = ground_story({
        "goal": "Users pay a monthly fee.",
        "how_it_works": [{"text": "cited", "units": ["u1", "u99"]}, {"text": "uncited", "units": []}, {"text": "bogus", "units": ["u42"]}],
        "chapters": {"database": "Stores fees.", "nonsense": "x"},
        "units": {"u1": {"what": "a table", "why": "holds fees"}, "u42": {"what": "made up"}},
    }, {"u1", "u2"})
    assert out["how_it_works"] == [{"text": "cited", "units": ["u1"]}]
    assert set(out["units"]) == {"u1"} and set(out["chapters"]) == {"database"}
    assert ground_story("not a dict", {"u1"}) == {}


def test_no_intent_means_no_scope_check(py_report, monkeypatch):
    import glance.llm as L

    fake = '{"summary": "s", "intent": {"asked": "guessed from commits", "done": ["x"], "extra": [], "missing": []}, "business_logic": [], "concerns": [], "walkthrough": []}'
    monkeypatch.setattr(L, "available_provider", lambda p="auto": "claude-cli")
    monkeypatch.setattr(L, "_call_claude_cli", lambda *a, **k: fake)
    rep = dict(py_report, intent=None)
    assert "none given" in L.build_prompt(rep)
    calls = []
    monkeypatch.setattr(L, "_call_claude_cli", lambda *a, **k: calls.append(1) or fake)
    assert L.run(rep)["intent"] is None                          # model guessed one anyway: dropped
    assert L.run(dict(py_report, intent="Add loans"))["intent"]["asked"] == "guessed from commits"
    again = L.run(dict(py_report, intent="Add loans"))           # same diff + task: served from cache
    assert again.get("cached") and len(calls) == 2


def test_saved_intent_is_per_branch(tmp_path):
    import argparse
    import subprocess

    from glance.cli import _read_intent, intent_path

    subprocess.run(["git", "init", "-q", "-b", "feature/x"], cwd=tmp_path, check=True)
    args = argparse.Namespace(intent=None, intent_file=None, head=None)
    assert _read_intent(args, tmp_path) is None
    p = intent_path(tmp_path, args)
    assert p.name == "feature_x.md"
    p.parent.mkdir(parents=True); p.write_text("Add a $2 fee\n")
    assert _read_intent(args, tmp_path) == "Add a $2 fee"
    assert _read_intent(argparse.Namespace(intent="cli wins", intent_file=None, head=None), tmp_path) == "cli wins"


def test_refresh_carries_ai_and_tracks_resolved(py_report):
    import copy

    from glance.llm import attach_evidence, carry_over, resolved_since

    old = copy.deepcopy(py_report)
    files = [f for f in old["files"] if f["hunks"]]
    h1, h2 = files[0]["hunks"][0]["id"], files[-1]["hunks"][0]["id"]
    old["llm"] = attach_evidence({"status": "ok", "concerns": [
        {"severity": "high", "title": "A", "detail": "a", "hunks": [h1]},
        {"severity": "low", "title": "B", "detail": "b", "hunks": [h2]}], "business_logic": [], "walkthrough": [],
        "story": {"units": {old["story"]["units"][0]["id"]: {"what": "w", "why": "y"}}, "how_it_works": []}}, old)
    assert old["llm"]["concerns"][0]["evidence"] and old["llm"]["concerns"][0]["files"] == [files[0]["path"]]

    new = copy.deepcopy(py_report)                 # simulate editing the code behind concern A, in place:
    for f in new["files"]:                         # same positional hunk id, different content
        for h in f["hunks"]:
            if h["id"] == h1:
                h["lines"].append(["+", None, 999, "# fixed"])
    gone = new["risks"].pop(0)                     # and fixing one static risk
    carried = carry_over(old, new)
    a, b = carried["concerns"]
    assert a["stale"] and a["hunks"] == [h1] and not b.get("stale")
    assert carried["carried"] and set(carried["story"]["units"]) == {new["story"]["units"][0]["id"]}
    assert [r["title"] for r in resolved_since(old, new)] == [gone["title"]]
    assert carry_over({"llm": {"status": "skipped"}}, new)["status"] == "skipped"


def _ts_repo(tmp_path, base_files: dict, branch_files: dict):
    import subprocess

    run = lambda *a: subprocess.run(a, cwd=tmp_path, check=True, capture_output=True)
    run("git", "init", "-q", "-b", "main")
    for p, t in base_files.items():
        (tmp_path / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / p).write_text(t)
    run("git", "add", "-A"); run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    run("git", "checkout", "-q", "-b", "feature")
    for p, t in branch_files.items():
        (tmp_path / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / p).write_text(t)
    from glance.analyze import analyze
    from glance.git import collect

    return analyze(collect(tmp_path, base="main"))


def test_house_style_suppresses_style_rules_not_security(tmp_path):
    convention = {f"src/m{i}.ts": "\n".join(f"export const f{i}_{j} = (amount: number) => amount.toFixed(2)" for j in range(4)) + "\n" for i in range(3)}
    new = {"src/fee.ts": 'export function fee(amount: number) {\n  return amount.toFixed(2)\n}\nconst q = db.$queryRawUnsafe(`SELECT ${x}`)\n'}
    rep = _ts_repo(tmp_path, convention, new)
    rules = {r["rule"] for r in rep["risks"]}
    assert "money-float" not in rules and "unsafe-raw" in rules          # style skipped, security kept
    assert rep["suppressed"][0]["rule"] == "money-float" and rep["suppressed"][0]["existing"] >= 8


def test_style_rule_fires_without_convention(tmp_path):
    rep = _ts_repo(tmp_path, {"src/a.ts": "export const a = 1\n"}, {"src/fee.ts": "export function fee(amount: number) {\n  return amount.toFixed(2)\n}\n"})
    assert "money-float" in {r["rule"] for r in rep["risks"]} and rep["suppressed"] == []


def test_alert_catch_env_flags_and_renames(tmp_path):
    base = {"src/util.ts": "export function oldHelper(x: number) {\n  return x + 1\n}\n",
            "src/use.ts": "import { oldHelper } from './util'\nexport const y = oldHelper(1)\n"}
    branch = {
        "src/jobs.ts": "\n".join([
            "export async function run() {",
            "  EmailClient.sendAlert({",
            "    title: 'x',",
            "  }).catch(() => {})",
            "  fetchThings().catch(() => {})",
            "  if (process.env.NEW_FLAG === 'true') return",
            "  const key = process.env.NEW_REQUIRED_KEY",
            "}", ""]),
        "src/jobs.test.ts": "process.env.ONLY_IN_TESTS = 'x'\n",
        # oldHelper moves to a class getter of the same name: refs now point at the live one
        "src/util.ts": "export class Util {\n  static get oldHelper() {\n    return 1\n  }\n}\n",
    }
    rep = _ts_repo(tmp_path, base, branch)
    sw = [r for r in rep["risks"] if r["rule"] == "swallowed"]
    assert len(sw) == 1 and sw[0]["line"] == 5                           # fetchThings() flagged, sendAlert() not
    env = {r["title"]: r["severity"] for r in rep["risks"] if r["rule"] == "new-env"}
    assert env == {"New feature flag: NEW_FLAG": "low", "New env var: NEW_REQUIRED_KEY (required)": "medium"}
    assert "removed-still-used" not in {r["rule"] for r in rep["risks"]}
    assert "sensitive-area" not in {r["rule"] for r in rep["risks"]}


def test_read_first_and_triage_grounding():
    from glance.llm import ground_story

    hunks = {"h_a": {"new_start": 10, "new_len": 30}}
    out = ground_story({
        "units": {"u1": {"what": "w", "why": "y", "attention": "routine", "read_reason": ""},
                  "u2": {"what": "w", "why": "y", "attention": "maybe"}},
        "read_first": [
            {"unit": "u1", "hunk": "h_a", "from": 12, "to": 18, "why": "money math"},
            {"unit": "u1", "hunk": "h_a", "from": 1, "to": 500, "why": "clamped"},
            {"unit": "u1", "hunk": "h_zzz", "from": 1, "to": 2, "why": "unknown hunk"},
            {"unit": "u9", "hunk": "h_a", "from": 12, "to": 13, "why": "unknown unit"}],
    }, {"u1", "u2"}, hunks)
    assert out["units"]["u1"]["attention"] == "routine" and "attention" not in out["units"]["u2"]
    assert [(s["from"], s["to"]) for s in out["read_first"]] == [(12, 18), (10, 39)]


def test_story_triage_defaults(py_report):
    units = py_report["story"]["units"]
    assert all(u["attention"] in ("read", "routine") and u["attention_why"] for u in units)
    assert all(u["attention"] == "read" for u in units if u["chapter"] in ("database", "routes"))


def test_decisions_grounded_and_carried(py_report):
    import copy

    from glance.llm import attach_evidence, carry_over, ground

    files = [f for f in py_report["files"] if f["hunks"]]
    h1 = files[0]["hunks"][0]["id"]
    uid = py_report["story"]["units"][0]["id"]
    data = {"business_logic": [], "concerns": [], "walkthrough": [], "decisions": [
        {"title": "Nightly check", "chose": "cron", "alternative": "on write", "tradeoff": "24h lag", "weight": "huge",
         "hunks": [h1, "h_fake"], "units": [uid, "u_nope"]},
        {"title": "Uncited", "chose": "x", "hunks": ["h_fake"]},
        {"title": "", "hunks": [h1]},
        "not a dict"]}
    out, dropped = ground(data, {h1}, {uid})
    assert dropped == 1
    [d] = out["decisions"]
    assert d["hunks"] == [h1] and d["units"] == [uid] and d["weight"] == "medium"

    old = copy.deepcopy(py_report)
    old["llm"] = attach_evidence({"status": "ok", **out}, old)
    did = old["llm"]["decisions"][0]["id"]
    assert did.startswith("d_") and old["llm"]["decisions"][0]["fingerprint"]
    assert not carry_over(old, copy.deepcopy(py_report))["decisions"][0].get("stale")

    new = copy.deepcopy(py_report)
    for f in new["files"]:
        for hk in f["hunks"]:
            if hk["id"] == h1:
                hk["lines"].append(["+", None, 999, "# changed"])
    carried = carry_over(old, new)["decisions"][0]
    assert carried["stale"] and carried["id"] == did

    legacy = copy.deepcopy(old)
    del legacy["llm"]["decisions"]
    assert "decisions" not in carry_over(legacy, new)

    assert "Decisions to agree with" in render({**py_report, "llm": old["llm"]})
    assert '"decisions"' in llm.build_prompt(py_report)


def test_scenarios_and_questions(py_report):
    import copy

    from glance.llm import attach_evidence, carry_over, ground

    h1 = next(f for f in py_report["files"] if f["hunks"])["hunks"][0]["id"]
    data = {"business_logic": [], "concerns": [], "walkthrough": [],
            "scenarios": [
                {"title": "Fee", "subject": "fee(amount)", "rule": "half price under 10k", "hunks": [h1, "h_x"], "units": ["nope"],
                 "rows": [{"case": "$5k", "input": {"amount": 5000}, "before": "$50", "after": "$25"},
                          {"case": "$20k", "input": "bad", "before": "$200", "after": "$200."},
                          {"case": "null", "before": "TypeError", "after": "unclear: depends on caller"},
                          {"before": "no case"}, "junk"]},
                {"title": "No rows", "hunks": [h1], "rows": []},
                {"title": "Uncited", "hunks": ["h_x"], "rows": [{"case": "a", "before": "b", "after": "c"}]}],
            "questions": [{"q": "What if it fails?", "answer": "Rolls back.", "hunks": [h1]}, {"q": "", "hunks": [h1]},
                          {"q": "Uncited?", "hunks": []}]}
    out, dropped = ground(data, {h1}, set())
    assert dropped == 2
    [sc] = out["scenarios"]
    assert sc["units"] == [] and sc["id"].startswith("s_") and len(sc["rows"]) == 3
    a, b, c = sc["rows"]
    assert a["changed"] and a["input"] == {"amount": 5000}
    assert not b["changed"] and b["input"] is None          # "$200" vs "$200." is the same outcome
    assert c["unclear"] and c["changed"]
    [q] = out["questions"]
    assert q["id"].startswith("q_") and q["answer"] == "Rolls back."

    old = copy.deepcopy(py_report)
    old["llm"] = attach_evidence({"status": "ok", **out}, old)
    new = copy.deepcopy(py_report)
    for f in new["files"]:
        for hk in f["hunks"]:
            if hk["id"] == h1:
                hk["lines"].append(["+", None, 999, "# changed"])
    carried = carry_over(old, new)
    assert carried["scenarios"][0]["stale"] and carried["questions"][0]["stale"]

    md = render({**py_report, "llm": old["llm"]})
    assert "| $5k | $50 | **$25** |" in md and "What if it fails?" in md
