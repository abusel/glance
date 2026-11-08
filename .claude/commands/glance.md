---
description: Structured review of the current change — schemas, migrations, endpoints, business logic, risks
argument-hint: "[branch] [--base main] [--intent \"what this change was supposed to do\"]"
---

Run this from the repo root and read the whole output:

```
glance --format md --llm none $ARGUMENTS
```

(`--llm none` because you are already the model doing the reasoning; glance supplies the structural facts.)

Then give me a review in this order, keeping it tight:

1. **What changed** — 2–3 sentences in plain English.
2. **High-risk items** — for each high flag, open the cited file/line, confirm or dismiss it with the actual code, and say what to do.
3. **Business logic** — every rule, threshold, calculation or state transition that changed, before → after. Include constants glance listed.
4. **Data & API contract** — migrations vs. model changes (do they match?), breaking request/response changes, endpoints without auth.
5. **Things static analysis can't see** — undefined names, wrong logic, missing edge cases, mismatch with the intent if one was given.

Cite `file:line` for every claim. Don't restate low-severity flags unless they matter. If something needs a fix, offer to make it but don't edit files until I say so.

If I want the visual version, tell me to run `glance` (opens the local review page).
