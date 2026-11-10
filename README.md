# glance

Local code review for AI-written changes. Instead of reading every line, you get the change broken down
into what a reviewer actually needs: **schema and migration changes, new/changed models, endpoints, tasks,
business-logic changes, risks, and a risk-ranked reading order**, with every item one click from the exact
diff hunk that justifies it.

Works on **Python (FastAPI / Pydantic / SQLAlchemy / SQLModel / Alembic / Celery / Pydantic AI)** and
**TypeScript / Node (Express / Fastify / Hono / NestJS / Next.js / tRPC / Zod / Prisma / Drizzle / TypeORM /
Mongoose / DBOS / BullMQ / Knex)**.

```
pipx install -e .          # or: pip install -e .
cd your-repo
glance                     # working tree (incl. uncommitted) vs. merge-base with main → opens the review page
```

## How it works

1. **Deterministic layer (instant, no LLM).** Parses the before and after version of every changed file
   (Python `ast`, tree-sitter for TS/JS, plus Prisma schema, SQL migrations and package manifests), extracts
   entities, and diffs them structurally: field-level before → after for tables and models, op lists for
   migrations, signature changes for functions, auth dependencies for routes. Then `git grep` finds callers
   of changed / removed functions (blast radius) and ~50 risk rules run over the result.
2. **Model pass (optional, runs in the background).** Claude gets the structural facts plus the diff with
   hunk ids and returns: a summary, intent vs. actual (if you pass `--intent`), business-logic changes as
   before → after, concerns, and a walkthrough. **Every item must cite hunk ids that exist in the diff;
   anything uncited is dropped.** Uses the `claude` CLI if it's on your PATH (runs in the repo with read-only
   tools, so it sees your `CLAUDE.md` and can look around), otherwise `ANTHROPIC_API_KEY`.

The page shows static results immediately and fills in the model pass when it lands.

## Understanding the change, not just reading it

With the model pass, the page reads top to bottom as: the story → decisions → map → behavior → comprehension
→ step by step. Three meters in the top bar track **Read**, **Decisions** and **Understood**; when all three
are full it says **Ready to merge**.

- **Decisions**: the judgement calls in the change (chose / instead of / tradeoff / stakes). Agree, question or
  disagree; questions and disagreements stay open until you resolve them.
- **Map**: new and changed routes, jobs, functions, schemas and tables in columns (entry → logic → shapes →
  storage), with field-level diffs. Every line is a real reference in the code. Click a box to trace what it
  touches; pick a flow to walk it step by step with the data's shape at each step.
- **What behaves differently**: before/after tables of concrete inputs, boundary cases included. When a table
  maps to one function, **▶ Run before & after** calls it for real on the base commit and on your code (with
  the project's `.venv` Python, or node / `tsx`), flags rows where Claude's prediction is wrong, lets you edit
  inputs or add cases, and copies the cases as a test file. It runs your code locally, so anything Claude
  didn't mark pure asks first. The base commit is extracted to `~/.cache/glance/snapshots`, never into the repo.
- **What this relies on**: assumptions the change makes (never null, runs once, rows already have X) and what
  enforces each: a DB constraint, a guard, a type or a test. Claude must quote the enforcing code; glance finds
  that quote in the file and shows it as proof, or marks the claim "couldn't verify". For anything unenforced,
  accept the risk or ask the agent to enforce it (counts toward Decisions).
- **Can you answer these?**: questions you should be able to answer before merging, answers hidden.
- **Copy feedback**: open questions, disagreements, flagged rows and ask-the-agent questions as one prompt.

## Usage

```
glance                                  # working tree vs main
glance feature/margin-calls             # a branch vs main
glance --base develop HEAD~3            # explicit range
glance --intent "Add margin calls when equity < 25%"
glance --intent-file plan.json          # compare against a task/plan (e.g. a Ranger plan)
glance --repo ../worktrees/bug-123      # review a worktree
glance --llm none                       # static only
glance --format md                      # markdown to stdout (PR description, Claude Code)
glance --format html                    # self-contained .glance/report.html to share
glance --format json --stdout           # raw report
glance serve                            # reopen the last report
```

In the page: check items off as you review them (progress persists per branch), filter risks by severity,
expand any item to its hunks, press **r** or **↻ Refresh** after you or an agent edits the code.

Reports are written to `.glance/` (auto-added to `.git/info/exclude`, your `.gitignore` is untouched).

### In Claude Code

Copy `.claude/commands/glance.md` into your repo (or `~/.claude/commands/`) and run `/glance`. Claude runs
`glance --format md --llm none`, then verifies the high-risk flags against the code and walks you through
business-logic and contract changes.

## What it extracts

| | Python | TypeScript / Node |
|---|---|---|
| Tables | SQLAlchemy, SQLModel | Prisma, Drizzle, TypeORM, Mongoose, `CREATE TABLE` |
| Migrations | Alembic (ops, downgrade) | Prisma / Drizzle SQL, Knex, TypeORM |
| Models | Pydantic, dataclasses, TypedDict, BaseSettings | Zod, interfaces, object types, enums, unions |
| Endpoints | FastAPI (+ router prefix, `Depends`/`Security`) | Express, Fastify, Hono, Koa, NestJS (+ guards), Next.js route handlers & server actions, tRPC |
| Tasks | Celery | DBOS workflows / steps / transactions / queues, BullMQ |
| Logic | `UPPER_CASE` constants, prompts, Pydantic AI agents + tools | constants, prompts, agent configs |
| Other | env vars (incl. settings fields), every function signature, dependencies | env vars, every function signature, dependencies, npm scripts |

## Risk rules (highlights)

- **Data:** destructive migration (drop/truncate/delete), NOT NULL column without default, column type
  change, renames, irreversible migration, two Alembic revisions with one parent, **models changed with no
  migration**, migration touching a table whose model didn't change, enum value removed.
- **API:** endpoint removed / path changed, **auth removed**, **new endpoint without auth when its siblings
  have it**, request field removed, new required request field.
- **Async work:** Celery task signature change (queued messages break), DBOS workflow signature/step-order
  change (recovery breaks), `Date.now()` / `Math.random()` / `fetch` inside a DBOS workflow.
- **Code:** removed function still referenced, signature change with call sites outside the diff, swallowed
  exceptions / empty catch, money as float, SQL built by interpolation, unsafe raw queries, hard-coded
  secrets, TLS/CORS disabled, auth "bypass" flags, unscoped deletes, `eval`/`shell=True`, type suppressions,
  debug leftovers, dead new functions, very long new functions.
- **Tests:** test file deleted, cases removed, assertions weakened, `skip`/`.only`, logic changed with no
  test changes.
- **Business:** threshold/limit/rate constants changed, prompts changed, new required env vars, new deps,
  major version bumps, changes in auth/payments/ledger paths.

Rules live in `glance/risk.py`; each one is a small function that yields flags with a file, line and hunk ids.

## Layout

```
glance/
  git.py              change set: files, before/after text, hunks, untracked files
  extractors/         python.py, typescript.py, prisma.py, sql.py, manifest.py
  analyze.py          entity diffing, moves, callers, ranking, report
  risk.py             deterministic rules
  llm.py              grounded model pass (claude CLI or Anthropic API)
  render_md.py        markdown output
  server.py, ui/      local review page (stdlib server, single-file UI)
tests/
  fixtures.py         builds two demo repos with realistic AI-written feature branches
  test_glance.py
```

Try it on the demos: `python tests/fixtures.py /tmp/demo && cd /tmp/demo/node-app && glance`.

## Ideas for next

- Ranger integration: open glance on each worktree from Ranger's review screen and pass the approved plan as
  `--intent-file`.
- Cross-file route prefixes (`app.include_router(router, prefix=...)`, `app.use("/api", router)`).
- Per-repo rule config (`.glance.toml`): severities, ignored paths, which constants count as business logic.
- "Ask about this hunk" from the page (send a question plus hunk ids to `claude -p`).
