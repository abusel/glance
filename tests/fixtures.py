"""Build small demo repos with realistic 'AI-written' feature branches.

    python tests/fixtures.py /tmp/demo   # creates /tmp/demo/fastapi-app and /tmp/demo/node-app
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _write(repo: Path, files: dict[str, str | None]) -> None:
    for rel, content in files.items():
        p = repo / rel
        if content is None:
            p.unlink(missing_ok=True)
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(content).lstrip("\n"))


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "Dev")
    _git(repo, "config", "commit.gpgsign", "false")


def _commit(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


# ---------------------------------------------------------------------------------- FastAPI

PY_BASE = {
    "pyproject.toml": """
        [project]
        name = "lending-api"
        version = "0.1.0"
        dependencies = ["fastapi>=0.115", "sqlalchemy>=2.0", "pydantic>=2.8", "celery>=5.4", "alembic>=1.13"]
    """,
    "app/config.py": """
        import os

        MAINTENANCE_MARGIN = 0.30
        MAX_LEVERAGE = 2.0
        STATEMENT_DAY = 1
        DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/lending")
    """,
    "app/models.py": """
        from datetime import datetime
        from decimal import Decimal

        from sqlalchemy import ForeignKey, Numeric, String
        from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


        class Base(DeclarativeBase):
            pass


        class Account(Base):
            __tablename__ = "accounts"
            id: Mapped[int] = mapped_column(primary_key=True)
            email: Mapped[str] = mapped_column(String(255), unique=True)
            equity: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)


        class Loan(Base):
            __tablename__ = "loans"
            id: Mapped[int] = mapped_column(primary_key=True)
            account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
            principal: Mapped[Decimal] = mapped_column(Numeric(14, 2))
            status: Mapped[str] = mapped_column(String(20), default="open")
            notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
            created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    """,
    "app/schemas.py": """
        from decimal import Decimal

        from pydantic import BaseModel, Field


        class LoanCreate(BaseModel):
            account_id: int
            principal: Decimal = Field(gt=0)


        class LoanOut(BaseModel):
            id: int
            account_id: int
            principal: Decimal
            status: str
            notes: str | None = None
    """,
    "app/services/rates.py": """
        from decimal import Decimal


        def legacy_rate(principal: Decimal) -> Decimal:
            return Decimal("0.065") if principal < 50_000 else Decimal("0.055")


        def current_rate(principal: Decimal) -> Decimal:
            return legacy_rate(principal)
    """,
    "app/api/loans.py": """
        from fastapi import APIRouter, Depends, HTTPException

        from app.auth import get_current_user
        from app.schemas import LoanCreate, LoanOut
        from app.services.rates import current_rate

        router = APIRouter(prefix="/loans", tags=["loans"])


        @router.post("", response_model=LoanOut, status_code=201)
        async def create_loan(body: LoanCreate, user=Depends(get_current_user)) -> LoanOut:
            rate = current_rate(body.principal)
            return await repo.create(body, rate=rate, user=user)


        @router.get("/{loan_id}", response_model=LoanOut)
        async def get_loan(loan_id: int, user=Depends(get_current_user)) -> LoanOut:
            loan = await repo.get(loan_id)
            if loan is None or loan.account_id != user.account_id:
                raise HTTPException(404)
            return loan
    """,
    "app/tasks.py": """
        from celery import Celery

        celery_app = Celery("lending")


        @celery_app.task(bind=True, max_retries=3)
        def send_statement(self, account_id: int):
            statements.render_and_send(account_id)
    """,
    "alembic/versions/0001_init.py": '''
        """init"""
        from alembic import op
        import sqlalchemy as sa

        revision = "0001"
        down_revision = None


        def upgrade():
            op.create_table("accounts", sa.Column("id", sa.Integer, primary_key=True), sa.Column("email", sa.String(255)))
            op.create_table("loans", sa.Column("id", sa.Integer, primary_key=True), sa.Column("notes", sa.String(500)))


        def downgrade():
            op.drop_table("loans")
            op.drop_table("accounts")
    ''',
    "tests/test_loans.py": """
        def test_create_loan(client):
            r = client.post("/loans", json={"account_id": 1, "principal": "1000"})
            assert r.status_code == 201
            assert r.json()["status"] == "open"


        def test_get_loan_forbidden(client):
            r = client.get("/loans/999")
            assert r.status_code == 404
    """,
}

PY_FEATURE = {
    "pyproject.toml": """
        [project]
        name = "lending-api"
        version = "0.1.0"
        dependencies = ["fastapi>=0.115", "sqlalchemy>=2.0", "pydantic>=2.8", "celery>=5.4", "alembic>=1.13", "httpx>=0.27"]
    """,
    "app/config.py": """
        import os

        MAINTENANCE_MARGIN = 0.25
        MAX_LEVERAGE = 2.0
        STATEMENT_DAY = 1
        DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/lending")
        MARGIN_WEBHOOK_URL = os.environ["MARGIN_WEBHOOK_URL"]
    """,
    "app/models.py": """
        from datetime import datetime
        from decimal import Decimal

        from sqlalchemy import ForeignKey, Numeric, String
        from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


        class Base(DeclarativeBase):
            pass


        class Account(Base):
            __tablename__ = "accounts"
            id: Mapped[int] = mapped_column(primary_key=True)
            email: Mapped[str] = mapped_column(String(255), unique=True)
            equity: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)


        class Loan(Base):
            __tablename__ = "loans"
            id: Mapped[int] = mapped_column(primary_key=True)
            account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
            principal: Mapped[Decimal] = mapped_column(Numeric(14, 2))
            status: Mapped[str] = mapped_column(String(20), default="open")
            interest_rate: Mapped[float] = mapped_column(nullable=False)
            margin_call_at: Mapped[datetime | None] = mapped_column(nullable=True)
            created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    """,
    "alembic/versions/0002_margin_calls.py": '''
        """add margin calls and per-loan interest rate"""
        from alembic import op
        import sqlalchemy as sa

        revision = "0002"
        down_revision = "0001"


        def upgrade():
            op.add_column("loans", sa.Column("interest_rate", sa.Float(), nullable=False))
            op.add_column("loans", sa.Column("margin_call_at", sa.DateTime(), nullable=True))
            op.drop_column("loans", "notes")


        def downgrade():
            pass
    ''',
    "app/schemas.py": """
        from datetime import datetime
        from decimal import Decimal

        from pydantic import BaseModel, Field


        class LoanCreate(BaseModel):
            account_id: int
            principal: Decimal = Field(gt=0)
            term_months: int


        class LoanOut(BaseModel):
            id: int
            account_id: int
            principal: Decimal
            status: str
            interest_rate: float
            margin_call_at: datetime | None = None


        class MarginCallOut(BaseModel):
            loan_id: int
            equity_ratio: float
            triggered: bool
    """,
    "app/services/rates.py": """
        from decimal import Decimal


        def current_rate(principal: Decimal, term_months: int = 12) -> Decimal:
            base = Decimal("0.065") if principal < 50_000 else Decimal("0.055")
            if term_months > 24:
                base += Decimal("0.005")
            return base
    """,
    "app/services/margin.py": """
        import httpx

        from app.config import MAINTENANCE_MARGIN, MARGIN_WEBHOOK_URL


        def equity_ratio(equity, loan_amount):
            total = float(equity) + float(loan_amount)
            return float(equity) / total if total else 0.0


        def check_margin(equity, loan_amount, threshold=MAINTENANCE_MARGIN):
            ratio = equity_ratio(equity, loan_amount)
            triggered = ratio < threshold
            if triggered:
                try:
                    httpx.post(MARGIN_WEBHOOK_URL, json={"ratio": ratio}, timeout=5)
                except Exception:
                    pass
            print("margin check", ratio, triggered)
            return ratio, triggered
    """,
    "app/api/loans.py": """
        from fastapi import APIRouter, Depends, HTTPException

        from app.auth import get_current_user
        from app.schemas import LoanCreate, LoanOut, MarginCallOut
        from app.services.margin import check_margin
        from app.services.rates import current_rate, legacy_rate

        router = APIRouter(prefix="/loans", tags=["loans"])


        @router.post("", response_model=LoanOut, status_code=201)
        async def create_loan(body: LoanCreate, user=Depends(get_current_user)) -> LoanOut:
            rate = current_rate(body.principal, body.term_months)
            return await repo.create(body, rate=rate, user=user)


        @router.get("/{loan_id}", response_model=LoanOut)
        async def get_loan(loan_id: int, user=Depends(get_current_user)) -> LoanOut:
            loan = await repo.get(loan_id)
            if loan is None or loan.account_id != user.account_id:
                raise HTTPException(404)
            return loan


        @router.post("/{loan_id}/margin-check", response_model=MarginCallOut)
        async def run_margin_check(loan_id: int) -> MarginCallOut:
            loan = await repo.get(loan_id)
            account = await repo.get_account(loan.account_id)
            ratio, triggered = check_margin(account.equity, loan.principal)
            if triggered:
                await repo.mark_margin_call(loan_id)
            return MarginCallOut(loan_id=loan_id, equity_ratio=ratio, triggered=triggered)


        @router.get("/{loan_id}/quote")
        async def legacy_quote(loan_id: int, user=Depends(get_current_user)):
            loan = await repo.get(loan_id)
            return {"rate": str(legacy_rate(loan.principal))}
    """,
    "app/tasks.py": """
        from celery import Celery

        celery_app = Celery("lending")


        @celery_app.task(bind=True, max_retries=3)
        def send_statement(self, account_id: int, month: str):
            statements.render_and_send(account_id, month)


        @celery_app.task
        def sweep_margin_calls():
            for loan in repo.open_loans():
                check_margin_for(loan)
    """,
    "tests/test_loans.py": """
        import pytest


        def test_create_loan(client):
            r = client.post("/loans", json={"account_id": 1, "principal": "1000", "term_months": 12})
            assert r.status_code == 201


        @pytest.mark.skip(reason="flaky after margin changes")
        def test_get_loan_forbidden(client):
            r = client.get("/loans/999")
            assert r.status_code == 404
    """,
}


# ---------------------------------------------------------------------------------- Node / TS

TS_BASE = {
    "package.json": """
        {
          "name": "shop-api",
          "version": "1.0.0",
          "scripts": { "dev": "tsx watch src/index.ts", "test": "vitest" },
          "dependencies": {
            "@dbos-inc/dbos-sdk": "^2.8.0",
            "@prisma/client": "^5.20.0",
            "express": "^4.19.2",
            "zod": "^3.23.8"
          },
          "devDependencies": { "prisma": "^5.20.0", "typescript": "^5.6.0", "vitest": "^2.1.0" }
        }
    """,
    "prisma/schema.prisma": """
        datasource db {
          provider = "postgresql"
          url      = env("DATABASE_URL")
        }

        enum OrderStatus {
          PENDING
          PAID
          SHIPPED
          CANCELLED
        }

        model User {
          id     String  @id @default(cuid())
          email  String  @unique
          orders Order[]
        }

        model Order {
          id          String      @id @default(cuid())
          userId      String
          user        User        @relation(fields: [userId], references: [id])
          totalCents  Int
          status      OrderStatus @default(PENDING)
          createdAt   DateTime    @default(now())
        }
    """,
    "prisma/migrations/20260901000000_init/migration.sql": """
        CREATE TABLE "User" ("id" TEXT NOT NULL, "email" TEXT NOT NULL, CONSTRAINT "User_pkey" PRIMARY KEY ("id"));
        CREATE TABLE "Order" ("id" TEXT NOT NULL, "userId" TEXT NOT NULL, "totalCents" INTEGER NOT NULL);
    """,
    "src/config.ts": """
        export const REFUND_WINDOW_DAYS = 30;
        export const MAX_ITEMS_PER_ORDER = 50;
        export const PORT = Number(process.env.PORT ?? 3000);
    """,
    "src/schemas.ts": """
        import { z } from "zod";

        export const OrderInput = z.object({
          items: z.array(z.object({ sku: z.string(), qty: z.number().int().positive() })).max(50),
          coupon: z.string().optional(),
        });

        export interface OrderSummary {
          id: string;
          totalCents: number;
          status: "PENDING" | "PAID" | "SHIPPED" | "CANCELLED";
        }
    """,
    "src/routes/orders.ts": """
        import { Router } from "express";
        import { requireAuth } from "../auth";
        import { OrderInput } from "../schemas";
        import { CheckoutWorkflow } from "../workflows/checkout";

        export const router = Router();

        router.post("/orders", requireAuth, async (req, res) => {
          const input = OrderInput.parse(req.body);
          const handle = await DBOS.startWorkflow(CheckoutWorkflow).checkout(req.user.id, input);
          res.status(202).json({ workflowId: handle.workflowID });
        });

        router.get("/orders/:id", requireAuth, async (req, res) => {
          res.json(await prisma.order.findFirstOrThrow({ where: { id: req.params.id, userId: req.user.id } }));
        });

        router.post("/orders/:id/refund", requireAuth, async (req, res) => {
          await refunds.request(req.params.id, req.user.id);
          res.sendStatus(202);
        });
    """,
    "src/workflows/checkout.ts": """
        import { DBOS } from "@dbos-inc/dbos-sdk";

        export class CheckoutWorkflow {
          @DBOS.workflow()
          static async checkout(userId: string, input: OrderInput) {
            const order = await CheckoutWorkflow.reserveInventory(userId, input);
            await CheckoutWorkflow.chargeCard(order.id, order.totalCents);
            await CheckoutWorkflow.sendReceipt(order.id);
            return order.id;
          }

          @DBOS.step()
          static async reserveInventory(userId: string, input: OrderInput) {
            return inventory.reserve(userId, input.items);
          }

          @DBOS.step({ retriesAllowed: true, maxAttempts: 3 })
          static async chargeCard(orderId: string, amountCents: number) {
            return payments.charge(orderId, amountCents);
          }

          @DBOS.step()
          static async sendReceipt(orderId: string) {
            return email.receipt(orderId);
          }
        }
    """,
    "src/routes/orders.test.ts": """
        import { describe, it, expect } from "vitest";

        describe("orders", () => {
          it("creates an order", async () => {
            const res = await request(app).post("/orders").send({ items: [{ sku: "a", qty: 1 }] });
            expect(res.status).toBe(202);
          });
          it("refunds within window", async () => {
            const res = await request(app).post("/orders/1/refund");
            expect(res.status).toBe(202);
          });
        });
    """,
}

TS_FEATURE = {
    "package.json": """
        {
          "name": "shop-api",
          "version": "1.0.0",
          "scripts": { "dev": "tsx watch src/index.ts", "test": "vitest" },
          "dependencies": {
            "@dbos-inc/dbos-sdk": "^2.8.0",
            "@prisma/client": "^5.20.0",
            "express": "^4.19.2",
            "stripe": "^17.2.0",
            "zod": "^3.23.8"
          },
          "devDependencies": { "prisma": "^5.20.0", "typescript": "^5.6.0", "vitest": "^2.1.0" }
        }
    """,
    "prisma/schema.prisma": """
        datasource db {
          provider = "postgresql"
          url      = env("DATABASE_URL")
        }

        enum OrderStatus {
          PENDING
          PAID
          SHIPPED
          REFUNDED
        }

        model User {
          id     String  @id @default(cuid())
          email  String  @unique
          orders Order[]
        }

        model Order {
          id          String      @id @default(cuid())
          userId      String
          user        User        @relation(fields: [userId], references: [id])
          totalCents  Int
          currency    String      @default("usd")
          status      OrderStatus @default(PENDING)
          refundedAt  DateTime?
          createdAt   DateTime    @default(now())
        }
    """,
    "src/config.ts": """
        export const REFUND_WINDOW_DAYS = 60;
        export const MAX_ITEMS_PER_ORDER = 50;
        export const PORT = Number(process.env.PORT ?? 3000);
        export const STRIPE_KEY = process.env.STRIPE_SECRET_KEY;
        const FALLBACK_STRIPE_KEY = "sk_live_51Hx9QmZt4RkP2wLn8VbYc3Ad";
    """,
    "src/schemas.ts": """
        import { z } from "zod";

        export const OrderInput = z.object({
          items: z.array(z.object({ sku: z.string(), qty: z.number().int().positive() })).max(50),
          currency: z.enum(["usd", "eur"]),
        });

        export const RefundInput = z.object({
          amount: z.string(),
          reason: z.string().max(500).optional(),
        });

        export interface OrderSummary {
          id: string;
          totalCents: number;
          currency: string;
          status: "PENDING" | "PAID" | "SHIPPED" | "REFUNDED";
        }
    """,
    "src/routes/orders.ts": """
        import { Router } from "express";
        import { requireAuth } from "../auth";
        import { OrderInput, RefundInput } from "../schemas";
        import { CheckoutWorkflow } from "../workflows/checkout";
        import { REFUND_WINDOW_DAYS } from "../config";

        export const router = Router();

        router.post("/orders", requireAuth, async (req, res) => {
          const input = OrderInput.parse(req.body);
          const handle = await DBOS.startWorkflow(CheckoutWorkflow).checkout(req.user.id, input);
          res.status(202).json({ workflowId: handle.workflowID });
        });

        router.get("/orders/:id", requireAuth, async (req, res) => {
          res.json(await prisma.order.findFirstOrThrow({ where: { id: req.params.id, userId: req.user.id } }));
        });

        router.post("/orders/:id/refunds", requireAuth, async (req, res) => {
          const body = RefundInput.parse(req.body);
          const amount = parseFloat(body.amount);
          const order = await prisma.order.findUniqueOrThrow({ where: { id: req.params.id } });
          const ageDays = (Date.now() - order.createdAt.getTime()) / 86_400_000;
          if (ageDays > REFUND_WINDOW_DAYS) return res.status(422).json({ error: "refund window closed" });
          try {
            await stripe.refunds.create({ payment_intent: order.paymentIntentId, amount: Math.round(amount * 100) });
          } catch (e) {}
          await prisma.order.update({ where: { id: order.id }, data: { status: "REFUNDED", refundedAt: new Date() } });
          res.sendStatus(202);
        });

        router.delete("/orders/:id", async (req, res) => {
          await prisma.order.delete({ where: { id: req.params.id } });
          res.sendStatus(204);
        });
    """,
    "src/workflows/checkout.ts": """
        import { DBOS } from "@dbos-inc/dbos-sdk";

        export class CheckoutWorkflow {
          @DBOS.workflow()
          static async checkout(userId: string, input: OrderInput, currency: string) {
            const startedAt = Date.now();
            const order = await CheckoutWorkflow.reserveInventory(userId, input);
            await CheckoutWorkflow.sendReceipt(order.id);
            await CheckoutWorkflow.chargeCard(order.id, order.totalCents, currency);
            DBOS.logger.info(`checkout took ${Date.now() - startedAt}ms`);
            return order.id;
          }

          @DBOS.step()
          static async reserveInventory(userId: string, input: OrderInput) {
            return inventory.reserve(userId, input.items);
          }

          @DBOS.step({ retriesAllowed: true, maxAttempts: 5 })
          static async chargeCard(orderId: string, amountCents: number, currency: string) {
            return payments.charge(orderId, amountCents, currency);
          }

          @DBOS.step()
          static async sendReceipt(orderId: string) {
            return email.receipt(orderId);
          }
        }
    """,
    "src/routes/orders.test.ts": """
        import { describe, it, expect } from "vitest";

        describe("orders", () => {
          it("creates an order", async () => {
            const res = await request(app).post("/orders").send({ items: [{ sku: "a", qty: 1 }], currency: "usd" });
            expect(res.status).toBe(202);
          });
        });
    """,
}


def make_python_repo(root: Path) -> Path:
    repo = root / "fastapi-app"
    _init(repo)
    _write(repo, PY_BASE)
    _commit(repo, "init lending api")
    _git(repo, "checkout", "-q", "-b", "ai/margin-calls")
    _write(repo, PY_FEATURE)
    _commit(repo, "Add margin calls, per-loan interest rate and term-based pricing")
    return repo


def make_ts_repo(root: Path) -> Path:
    repo = root / "node-app"
    _init(repo)
    _write(repo, TS_BASE)
    _commit(repo, "init shop api")
    _git(repo, "checkout", "-q", "-b", "ai/refunds")
    _write(repo, TS_FEATURE)
    _commit(repo, "Add refunds endpoint, currency support and order deletion")
    return repo


if __name__ == "__main__":
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "demo")
    root.mkdir(parents=True, exist_ok=True)
    print(make_python_repo(root))
    print(make_ts_repo(root))
