# SAP Order-to-Cash — Graph Intelligence System

A graph-based data explorer with a natural-language query interface
over a real SAP O2C dataset (SQLite backend, Gemini LLM, Cytoscape.js graph).

---

## Final Project Structure

```
o2c-graph-intelligence/
├── backend/
│   ├── data/
│   │   └── sap-o2c-data/           ← real dataset lives HERE (inside backend/)
│   │       ├── sales_order_headers/
│   │       ├── sales_order_items/
│   │       ├── outbound_delivery_headers/
│   │       ├── outbound_delivery_items/
│   │       ├── billing_document_headers/
│   │       ├── billing_document_items/
│   │       ├── billing_document_cancellations/
│   │       ├── journal_entry_items_accounts_receivable/
│   │       ├── payments_accounts_receivable/
│   │       ├── business_partners/
│   │       ├── business_partner_addresses/
│   │       ├── customer_company_assignments/
│   │       ├── customer_sales_area_assignments/
│   │       ├── products/
│   │       ├── product_descriptions/
│   │       ├── plants/
│   │       ├── product_plants/
│   │       └── sales_order_schedule_lines/
│   ├── ingest.py       reads JSONL → creates db.sqlite
│   ├── database.py     SQLite access layer + graph builder
│   ├── llm.py          Gemini NL→SQL→answer + guardrails
│   ├── main.py         FastAPI app
│   ├── requirements.txt
│   └── db.sqlite       auto-created by ingest.py (do not commit)
├── frontend/
│   └── index.html      single-file UI (Cytoscape.js + chat)
└── README.md
```

---

## Step-by-Step Setup

### 1 — Place the dataset

Extract `sap-order-to-cash-dataset.zip` so the folders land at:
```
backend/data/sap-o2c-data/sales_order_headers/part-*.jsonl
backend/data/sap-o2c-data/billing_document_headers/part-*.jsonl
... etc.
```

### 2 — Install Python dependencies

```bash
pip install -r backend/requirements.txt
```

Requires Python 3.11 or later.

### 3 — Get a free Gemini API key

Go to https://ai.google.dev → "Get API key" (no credit card needed).

### 4 — Run ingestion  (from project root)

```bash
python backend/ingest.py
```

This creates `backend/db.sqlite` with all tables and indexes.
Expected output:
```
✓  sales_order_headers:   100 rows  24 cols
✓  sales_order_items:     167 rows  13 cols
...
✅  Done — 4670 rows loaded
```

### 5 — Set the API key

```bash
# Mac / Linux
export GEMINI_API_KEY=your_key_here

# Windows CMD
set GEMINI_API_KEY=your_key_here

# Windows PowerShell
$env:GEMINI_API_KEY="your_key_here"
```

⚠️ Add your Gemini API key inside backend/llm.py:

### 6 — Start the backend

```bash
cd o2c-graph-intelligence
cd backend
uvicorn main:app --reload --port 8000

```

### 7 — Open the UI

```
http://localhost:8000
```

The FastAPI server serves `frontend/index.html` directly — no separate
web server needed.

---

## Dataset Schema (verified from real JSONL files)

### Tables and primary join columns

| SQLite table | Source folder | Key column |
|---|---|---|
| sales_order_headers | sales_order_headers/ | salesOrder |
| sales_order_items | sales_order_items/ | salesOrder, material |
| delivery_headers | outbound_delivery_headers/ | deliveryDocument |
| delivery_items | outbound_delivery_items/ | deliveryDocument, referenceSdDocument |
| billing_headers | billing_document_headers/ | billingDocument, accountingDocument |
| billing_items | billing_document_items/ | billingDocument, referenceSdDocument, material |
| billing_cancellations | billing_document_cancellations/ | billingDocument |
| journal_entries | journal_entry_items_accounts_receivable/ | accountingDocument, referenceDocument |
| payments | payments_accounts_receivable/ | accountingDocument, customer |
| business_partners | business_partners/ | businessPartner |
| products | products/ | product |
| product_descriptions | product_descriptions/ | product |
| plants | plants/ | plant |

### Verified O2C join chain

```
sales_order_headers.salesOrder
  ──► sales_order_items.salesOrder              (100/100 matched)
  ──► delivery_items.referenceSdDocument        (86/100 — 14 orders undelivered)
        ──► delivery_headers.deliveryDocument   (86/86)
        ──► billing_items.referenceSdDocument   (83/86 — 3 deliveries unbilled)
              ──► billing_headers.billingDocument
                    ──► billing_headers.accountingDocument
                          ──► journal_entries.accountingDocument   (123/163)
                          ──► payments.accountingDocument          (120/163)
```

### Broken-flow counts (real dataset)

| Condition | Count |
|---|---|
| Sales orders without delivery | 14 of 100 |
| Deliveries without billing doc | 3 of 86 |
| Active billing docs unpaid | 27 of 83 active |

### Status field values

| Field | Values |
|---|---|
| sales_order_headers.overallDeliveryStatus | `'C'` complete · `'A'` pending |
| delivery_headers.overallGoodsMovementStatus | `'A'` not started · `'C'` complete |
| billing_headers.billingDocumentIsCancelled | `'True'` / `'False'` |
| billing_headers.billingDocumentType | `'F2'` invoice · `'S1'` cancellation |

---

## LLM Architecture

### Two-stage pipeline

1. **NL → SQL** (Gemini, temperature 0.1)
   - System prompt contains full schema, all verified join paths,
     status decodings, and three ready-made broken-flow SQL patterns.
   - Model returns only raw SQL.
   - One automatic self-correction attempt on execution failure.

2. **SQL rows → Answer** (Gemini, temperature 0.3)
   - Up to 10 rows sent back to Gemini with the original question.
   - Returns a concise 2–5 sentence natural-language answer.

### Guardrails (two layers)

1. **Keyword filter** — instant, no API call. Rejects queries containing:
   `recipe`, `weather`, `movie`, `cricket`, `poem`, `bitcoin`, etc.

2. **LLM-level** — system prompt instructs Gemini to return `OFFTOPIC`
   for non-dataset questions. Any response without `SELECT` is also rejected.

**Rejection message:**
> "This system only answers questions about the SAP Order-to-Cash dataset."

---

## Example Queries

| Query | What it returns |
|---|---|
| Top billed products | GROUP BY material, COUNT billingDocument DESC |
| Trace billing doc 90504253 | Full SO→DEL→BILL→JE→PAY chain |
| Trace SO 740556 | Same chain starting from a sales order |
| Orders not yet delivered | LEFT JOIN delivery_items, WHERE NULL |
| Deliveries not billed | LEFT JOIN billing_items, WHERE NULL |
| Unpaid billing docs | LEFT JOIN payments, WHERE NULL, not cancelled |
| Top customers by order value | GROUP BY soldToParty, SUM totalNetAmount |
| Cancelled billing documents | WHERE billingDocumentIsCancelled = 'True' |

---

## Environment Variables

| Variable | Required | Where to get it |
|---|---|---|
| `GEMINI_API_KEY` | Yes | https://ai.google.dev (free) |
