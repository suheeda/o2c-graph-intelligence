# AI Coding Sessions — Usage Log

**Project:** SAP Order-to-Cash Graph Intelligence System
**Role:** Forward Deployed Engineer Assignment
**Duration:** ~3–4 days of focused development

---

## 1. Tools Used

| Tool | Purpose |
|---|---|
| **ChatGPT (GPT-4o)** | System design, architecture decisions, schema reasoning, debugging |
| **Claude (Anthropic)** | Dataset inspection, join verification, code generation, prompt structuring |
| **VS Code** | Implementation, file editing, local testing |
| **Render** | Deployment, environment variable configuration, live debugging |
| **Gemini 1.5 Flash API** | Production LLM for NL→SQL→Answer pipeline |

---

## 2. How AI Was Used

AI tools were not used to blindly generate the project. They were used as a thinking partner at specific decision points:

- **Schema discovery** — I didn't assume column names. I used AI to help write inspection scripts that extracted real fields from the JSONL files before writing any ingestion logic.
- **Join verification** — Before building database.py, I used AI to help structure Python scripts that confirmed every join key against real data (e.g. verifying that `billing_items.referenceSdDocument` actually matched `delivery_headers.deliveryDocument`, not `delivery_items.deliveryDocument`).
- **Prompt engineering** — The Gemini system prompt required multiple rounds of refinement. AI helped me structure the prompt so that Gemini reliably returned raw SQL only, without markdown fences or explanations.
- **Debugging** — When queries failed silently or returned wrong results, I described the failure to AI and used the conversation to narrow down root causes before touching code.
- **Deployment issues** — Render-specific problems (path resolution, static file mounting order in FastAPI) were debugged with AI assistance.

At every stage, the generated output was tested against real data before being kept.

---

## 3. Key Prompt Workflows

### Prompt 1 — Dataset Inspection Before Writing Any Code

**Goal:** Understand the real schema without assumptions.

**Prompt approach:**
> "Write a Python script that reads every JSONL folder in a directory, prints the first record from each folder with all fields and their values, and counts total rows per folder. I need to understand the full schema before writing any SQL."

**Outcome:** This produced the complete field inventory for all 19 folders. It revealed that some fields were nested dicts (e.g. `creationTime: {hours, minutes, seconds}`), which required a custom `flatten()` function in `ingest.py`. Without this step, the ingestion would have failed or stored corrupt data.

---

### Prompt 2 — Verifying Join Keys Against Real Data

**Goal:** Confirm which columns actually link the O2C tables before building the graph or writing SQL.

**Prompt approach:**
> "Write a Python script that loads all records from two JSONL folders and checks how many values in field A of folder 1 appear in field B of folder 2. I want to verify the join completeness for every hop in the O2C chain."

**Outcome:** Revealed that `billing_items.referenceSdDocument` joins to `delivery_headers.deliveryDocument` (not to `outbound_delivery_items.deliveryDocument`), and that `billing_headers.billingDocument = journal_entries.referenceDocument` was the correct link — not the accounting document. These distinctions were critical for the trace query to work correctly. Also surfaced the broken flow counts: 14 undelivered orders, 3 unbilled deliveries, 27 unpaid active billing docs.

---

### Prompt 3 — Structuring the LLM System Prompt

**Goal:** Make Gemini reliably return raw SQL with no markdown, no explanation, and no hallucinated column names.

**Prompt approach:**
> "I'm building a NL→SQL system over a SQLite database. Here is the schema and the verified join paths. Write a system prompt for Gemini that: (1) lists the schema exactly, (2) documents all join relationships, (3) documents real status field values, (4) provides example SQL for the three most complex query patterns, and (5) instructs the model to return OFFTOPIC for non-dataset questions and raw SQL for everything else — no markdown, no backticks, no explanation."

**Outcome:** The resulting system prompt significantly reduced SQL errors. Including example broken-flow queries in the prompt gave Gemini correct JOIN patterns to follow for the most complex queries, instead of generating plausible-but-wrong SQL.

---

### Prompt 4 — Implementing the Self-Correction Loop

**Goal:** Handle cases where generated SQL fails to execute without crashing the user experience.

**Prompt approach:**
> "In my async answer_query function, after a SQL execution failure, I want to send the error message, original question, and failed SQL back to Gemini with an instruction to fix it. Show me how to structure this retry cleanly so it doesn't cause nested exceptions or confusing error messages."

**Outcome:** The self-correction loop now catches execution errors, sends a repair prompt to Gemini, and retries once. In practice this resolved issues where Gemini quoted column names inconsistently or used syntax not supported by SQLite (e.g. `ILIKE`, `REGEXP`).

---

### Prompt 5 — Debugging the Graph Edge Gap

**Goal:** Graph showed billing and payment nodes with no edges connecting them.

**Prompt approach:**
> "In my graph builder, I'm creating payment nodes from `payments.accountingDocument`, and billing nodes from `billing_headers.billingDocument`. The edge should connect them via `billing_headers.accountingDocument`. But no edges are appearing. Here is the relevant code. What's wrong?"

**Outcome:** The bug was that edges were being added before payment nodes were registered in `seen_nodes`, so the edge validity check dropped them silently. The fix was to look up the billing document from `billing_headers` for each payment's `accountingDocument` and resolve the correct node ID before calling `add_edge`. Edges then appeared correctly.

---

### Prompt 6 — FastAPI Static File Mounting Order

**Goal:** After deployment on Render, the root `/` endpoint returned a 404 instead of serving `index.html`.

**Prompt approach:**
> "In FastAPI, I have a `GET /` route that returns a FileResponse, and I also mount a StaticFiles directory at `/`. The static mount is overriding my explicit route. What is the correct order to declare them?"

**Outcome:** The StaticFiles mount must be registered **after** all explicit `@app.get()` routes. FastAPI matches routes in registration order and the wildcard static mount was being registered first during startup. Moving it to the bottom of `main.py` resolved the 404.

---

### Prompt 7 — Guardrail Design

**Goal:** Prevent the LLM from answering general knowledge questions using dataset-sounding language.

**Prompt approach:**
> "I'm building a two-layer guardrail. Layer 1 is a keyword list that rejects obviously off-topic queries before hitting the API. Layer 2 is an LLM instruction. What are the failure modes of each approach, and how should I combine them so neither layer causes false positives on legitimate dataset questions?"

**Outcome:** This conversation clarified that keyword matching should be conservative (only unambiguous off-topic terms) to avoid rejecting legitimate questions like "what is the status of this order." The LLM layer handles edge cases by returning `OFFTOPIC`, which is then checked before SQL execution. A final regex check for `SELECT` ensures nothing non-SQL reaches the database.

---

## 4. Debugging & Iteration

### Dataset Path Mismatch
The earliest blocker was a mismatch between the expected dataset path in `ingest.py` and where the files were actually extracted. Rather than hardcoding paths, I rewrote the path resolution using `os.path.abspath(__file__)` so ingest works correctly regardless of the working directory when invoked.

### Nested Dict Fields in JSONL
Fields like `creationTime`, `actualGoodsMovementTime`, and `creationTime` were stored as nested JSON objects. A naive ingestion would have stored them as raw strings `"{'hours': 11, 'minutes': 31}"`. The `flatten()` function in `ingest.py` expands these into separate columns (`creationTime_hours`, `creationTime_minutes`, `creationTime_seconds`), keeping the schema queryable.

### Boolean Values as Strings
`billingDocumentIsCancelled` is stored as a Python `False` / `True` in the JSONL, which becomes the string `'False'` / `'True'` after flattening. The Gemini prompt explicitly documents this so generated SQL uses `= 'False'` rather than `= 0` or `IS FALSE`, both of which would return zero results.

### Gemini Markdown in SQL Response
Early versions of the Gemini response sometimes wrapped SQL in ` ```sql ``` ` code fences. A regex cleanup function (`_clean_sql`) strips these before passing the string to SQLite, preventing execution errors.

### Graph Layout Performance
With 158+ nodes, the default Cytoscape.js layout was slow and produced overlapping nodes. Tuning `nodeRepulsion`, `idealEdgeLength`, and `gravity` parameters in the CoSE layout brought render time to under 1 second and produced a readable hierarchical structure.

### Render Deployment — Python Version
Render defaulted to Python 3.9, which doesn't support the `X | Y` union type syntax used in type hints (`str | None`). Fixed by specifying `python-3.11` in the Render environment settings, or by replacing union syntax with `Optional[str]` for compatibility.

---

## 5. Iteration Process

```
Read dataset → Inspect schema (AI-assisted) → Verify joins (scripted + AI)
  → Write ingest.py → Test with real data
  → Build database.py (query layer + graph builder)
  → Build llm.py (prompt v1 → test → refine prompt → test again)
  → Build main.py (API endpoints) → Test locally
  → Build frontend (graph + chat UI) → Test end-to-end
  → Deploy to Render → Debug path + env issues
  → Test all three required queries → Verify results against raw data
  → Refine guardrails → Final cleanup
```

Each layer was tested against real data before moving to the next. The LLM prompt went through four revisions — each driven by a specific query failure observed in testing.

---

## 6. Final Outcome

All three required queries work correctly and return grounded, accurate answers:

| Query | Verified Result |
|---|---|
| Highest billed products | FACESERUM 30ML VIT C and SUNSCREEN GEL SPF50 (22 billing docs each) |
| Trace billing doc 90504253 | SO 740556 → DEL 80738076 → BILL 90504253 (cancelled, no payment) |
| Incomplete flows | 14 orders undelivered · 3 deliveries unbilled · 27 active bills unpaid |

The system correctly rejects off-topic questions, self-corrects failed SQL queries, and highlights relevant graph nodes when a query returns identifiable document IDs.

The most valuable part of using AI during this project was not code generation — it was using AI to reason about data structure and join correctness before writing a single line of application code.
