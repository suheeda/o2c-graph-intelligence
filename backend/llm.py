"""
llm.py
------
Natural-language -> SQL -> natural-language-answer pipeline.
Uses Google Gemini via REST.

For quick local testing, paste your Gemini API key below.
Before GitHub/submission, move it back to an environment variable.
"""

import re
import json
import httpx

from database import get_schema_for_llm, run_query

# For local testing only
GEMINI_API_KEY = ""

MODEL_NAME = "gemini-3-flash-preview"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{MODEL_NAME}:generateContent"
)

REJECTION_MSG = (
    "This system is designed to answer only SAP Order-to-Cash (O2C) related "
    "queries such as sales orders, deliveries, billing documents, payments, "
    "customers, products, and journal entries."
)

_OFF_TOPIC_KEYWORDS = [
    "recipe", "cook", "bake", "weather", "forecast",
    "movie", "film", "actor", "celebrity",
    "sport", "football", "cricket", "basketball", "ipl", "fifa",
    "poem", "poetry", "song", "lyric", "music",
    "story", "novel", "write an essay",
    "joke", "funny",
    "capital of", "president of", "prime minister",
    "who is", "what is the meaning of",
    "bitcoin", "crypto", "ethereum", "stock price", "share price",
    "translate", "language", "geography",
    "history of", "tell me about world",
]


def _is_off_topic(query: str) -> bool:
    q = query.lower()
    return any(kw in q for kw in _OFF_TOPIC_KEYWORDS)


def _build_system_prompt() -> str:
    schema = get_schema_for_llm()
    return f"""You are a data analyst for an SAP Order-to-Cash (O2C) system.
You have access to a SQLite database with these exact tables and columns:

{schema}

VERIFIED JOIN RELATIONSHIPS — use these exactly:

1. Sales Order -> Line Items
   sales_order_headers.salesOrder = sales_order_items.salesOrder

2. Sales Order -> Delivery
   delivery_items.referenceSdDocument = sales_order_headers.salesOrder
   delivery_items.deliveryDocument    = delivery_headers.deliveryDocument

3. Delivery -> Billing
   billing_items.referenceSdDocument = delivery_items.deliveryDocument
   billing_items.billingDocument     = billing_headers.billingDocument

4. Billing -> Journal Entry
   journal_entries.accountingDocument = billing_headers.accountingDocument
   journal_entries.referenceDocument  = billing_headers.billingDocument

5. Billing -> Payment
   payments.accountingDocument = billing_headers.accountingDocument

6. Customer links
   sales_order_headers.soldToParty = business_partners.businessPartner
   billing_headers.soldToParty     = business_partners.businessPartner
   payments.customer               = business_partners.businessPartner
   journal_entries.customer        = business_partners.businessPartner

7. Product links
   sales_order_items.material = products.product
   billing_items.material     = products.product
   product_descriptions.product = products.product

8. Plant link
   delivery_items.plant = plants.plant

RULES:
1. If the question is NOT about this O2C dataset, respond ONLY with:
   OFFTOPIC
2. Otherwise write ONE valid SQLite SELECT statement.
3. Return ONLY raw SQL — no explanation, no markdown, no code fences.
4. Use LIMIT 50 by default; omit LIMIT only for COUNT queries.
5. Quote column names that clash with SQL keywords.
6. For trace queries, JOIN all tables along the full O2C chain.
7. Do NOT invent filters like language unless the user explicitly asks for them.
"""


async def _gemini(system_prompt: str | None, user_text: str, temperature: float = 0.1) -> str:
    if not GEMINI_API_KEY or GEMINI_API_KEY == "PASTE_YOUR_REAL_GEMINI_KEY_HERE":
        raise ValueError("GEMINI_API_KEY is missing or still placeholder text.")

    payload = {
        "contents": [{"parts": [{"text": user_text}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": 1024,
        },
    }

    if system_prompt:
        payload["system_instruction"] = {"parts": [{"text": system_prompt}]}

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            GEMINI_URL,
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def _clean_sql(raw: str) -> str:
    raw = re.sub(r"^```(?:sql)?\s*", "", raw.strip(), flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw.strip())
    return raw.strip()


def _is_safe_select_sql(sql: str) -> bool:
    s = sql.strip().lower()
    if not s:
        return False

    if not (s.startswith("select") or s.startswith("with")):
        return False

    blocked = [
        " insert ", " update ", " delete ", " drop ", " alter ",
        " create ", " truncate ", " pragma ", " attach ", " detach ",
        " replace ",
    ]
    padded = f" {s} "
    return not any(word in padded for word in blocked)


async def answer_query(question: str) -> dict:
    question = question.strip()
    q = question.lower().strip()

    # Stable fixed intents
    if any(x in q for x in [
        "top billed products",
        "highest billed products",
        "products with highest billing",
        "most billed products",
    ]):
        sql = """
        SELECT bi.material,
               pd.productDescription,
               SUM(CAST(bi.netAmount AS REAL)) AS totalBilledAmount
        FROM billing_items bi
        LEFT JOIN product_descriptions pd
          ON bi.material = pd.product
        GROUP BY bi.material, pd.productDescription
        ORDER BY totalBilledAmount DESC
        LIMIT 50
        """
        rows = run_query(sql)
        return {
            "answer": (
                f"The top billed product is {rows[0]['productDescription'] or rows[0]['material']} "
                f"with a billed amount of {rows[0]['totalBilledAmount']}."
                if rows else
                "No billed product results were found in the dataset."
            ),
            "sql": sql.strip(),
            "rows": rows[:50],
            "highlight_ids": _extract_highlight_ids(rows),
        }

    if any(x in q for x in [
        "orders not yet delivered",
        "orders without delivery",
        "undelivered orders",
        "incomplete flows",
        "broken flows",
    ]):
        sql = """
        SELECT soh.salesOrder
        FROM sales_order_headers soh
        LEFT JOIN delivery_items di
          ON di.referenceSdDocument = soh.salesOrder
        WHERE di.deliveryDocument IS NULL
        LIMIT 50
        """
        rows = run_query(sql)
        return {
            "answer": f"Found {len(rows)} sales orders that do not yet have a delivery.",
            "sql": sql.strip(),
            "rows": rows[:50],
            "highlight_ids": _extract_highlight_ids(rows),
        }

    if any(x in q for x in [
        "deliveries not billed",
        "unbilled deliveries",
    ]):
        sql = """
        SELECT DISTINCT dh.deliveryDocument
        FROM delivery_headers dh
        LEFT JOIN billing_items bi
          ON bi.referenceSdDocument = dh.deliveryDocument
        WHERE bi.billingDocument IS NULL
        LIMIT 50
        """
        rows = run_query(sql)
        return {
            "answer": f"Found {len(rows)} deliveries that have not been billed yet.",
            "sql": sql.strip(),
            "rows": rows[:50],
            "highlight_ids": _extract_highlight_ids(rows),
        }

    if any(x in q for x in [
        "unpaid billing documents",
        "billing docs without payment",
        "invoices without payment",
        "unpaid billing docs",
    ]):
        sql = """
        SELECT bh.billingDocument,
               bh.accountingDocument,
               bh.totalNetAmount
        FROM billing_headers bh
        LEFT JOIN payments p
          ON p.accountingDocument = bh.accountingDocument
        WHERE p.accountingDocument IS NULL
          AND bh.billingDocumentIsCancelled = 'False'
        LIMIT 50
        """
        rows = run_query(sql)
        return {
            "answer": f"Found {len(rows)} active billing documents without payment.",
            "sql": sql.strip(),
            "rows": rows[:50],
            "highlight_ids": _extract_highlight_ids(rows),
        }

    if any(x in q for x in [
        "cancelled billing documents",
        "canceled billing documents",
        "cancelled invoices",
        "canceled invoices",
    ]):
        sql = """
        SELECT billingDocument,
               accountingDocument,
               totalNetAmount,
               billingDocumentIsCancelled
        FROM billing_headers
        WHERE billingDocumentIsCancelled = 'True'
        LIMIT 50
        """
        rows = run_query(sql)
        return {
            "answer": f"Found {len(rows)} cancelled billing documents.",
            "sql": sql.strip(),
            "rows": rows[:50],
            "highlight_ids": _extract_highlight_ids(rows),
        }

    if any(x in q for x in [
        "revenue by product",
        "sales by product",
        "product revenue",
    ]):
        sql = """
        SELECT bi.material,
               pd.productDescription,
               SUM(CAST(bi.netAmount AS REAL)) AS revenue
        FROM billing_items bi
        LEFT JOIN product_descriptions pd
          ON bi.material = pd.product
        GROUP BY bi.material, pd.productDescription
        ORDER BY revenue DESC
        LIMIT 50
        """
        rows = run_query(sql)
        return {
            "answer": f"Found revenue details for {len(rows)} products.",
            "sql": sql.strip(),
            "rows": rows[:50],
            "highlight_ids": _extract_highlight_ids(rows),
        }

    if any(x in q for x in [
        "top customers by order value",
        "customers by order value",
        "highest value customers",
        "top customers",
    ]):
        sql = """
        SELECT soldToParty,
               SUM(CAST(totalNetAmount AS REAL)) AS totalOrderValue
        FROM sales_order_headers
        GROUP BY soldToParty
        ORDER BY totalOrderValue DESC
        LIMIT 50
        """
        rows = run_query(sql)
        return {
            "answer": f"Found top {len(rows)} customers by order value.",
            "sql": sql.strip(),
            "rows": rows[:50],
            "highlight_ids": _extract_highlight_ids(rows),
        }

    if any(x in q for x in [
        "trace billing doc",
        "trace billing document",
        "trace invoice",
        "billing flow",
        "show billing flow",
    ]):
        bill_match = re.search(r"\b\d{4,}\b", question)
        if bill_match:
            billing_doc = bill_match.group(0)
            sql = f"""
            SELECT bh.billingDocument,
                   bh.accountingDocument,
                   bh.soldToParty,
                   bi.referenceSdDocument AS deliveryDocument,
                   p.accountingDocument AS paymentDocument,
                   je.accountingDocument AS journalEntryDocument
            FROM billing_headers bh
            LEFT JOIN billing_items bi
              ON bi.billingDocument = bh.billingDocument
            LEFT JOIN payments p
              ON p.accountingDocument = bh.accountingDocument
            LEFT JOIN journal_entries je
              ON je.accountingDocument = bh.accountingDocument
            WHERE bh.billingDocument = '{billing_doc}'
            LIMIT 50
            """
            rows = run_query(sql)
            return {
                "answer": (
                    f"Found {len(rows)} flow record(s) for billing document {billing_doc}."
                    if rows else
                    f"No flow records were found for billing document {billing_doc}."
                ),
                "sql": sql.strip(),
                "rows": rows[:50],
                "highlight_ids": _extract_highlight_ids(rows),
            }

    if any(x in q for x in [
        "trace sales order",
        "trace so",
        "trace order flow",
        "show order flow",
        "full order flow",
        "end to end flow",
    ]):
        so_match = re.search(r"\b\d{4,}\b", question)
        if so_match:
            sales_order = so_match.group(0)
            sql = f"""
            SELECT soh.salesOrder,
                   dh.deliveryDocument,
                   bh.billingDocument,
                   bh.accountingDocument,
                   je.accountingDocument AS journalEntryDocument
            FROM sales_order_headers soh
            LEFT JOIN delivery_items di
              ON di.referenceSdDocument = soh.salesOrder
            LEFT JOIN delivery_headers dh
              ON dh.deliveryDocument = di.deliveryDocument
            LEFT JOIN billing_items bi
              ON bi.referenceSdDocument = dh.deliveryDocument
            LEFT JOIN billing_headers bh
              ON bh.billingDocument = bi.billingDocument
            LEFT JOIN journal_entries je
              ON je.accountingDocument = bh.accountingDocument
            WHERE soh.salesOrder = '{sales_order}'
            LIMIT 50
            """
            rows = run_query(sql)
            return {
                "answer": (
                    f"Found {len(rows)} flow record(s) for sales order {sales_order}."
                    if rows else
                    f"No flow records were found for sales order {sales_order}."
                ),
                "sql": sql.strip(),
                "rows": rows[:50],
                "highlight_ids": _extract_highlight_ids(rows),
            }

    if _is_off_topic(question):
        return {
            "answer": REJECTION_MSG,
            "sql": None,
            "rows": None,
            "highlight_ids": [],
        }

    try:
        raw_sql = await _gemini(_build_system_prompt(), question, temperature=0.1)
    except Exception as exc:
        return {
            "answer": f"Could not reach the AI service. Error: {exc}",
            "sql": None,
            "rows": None,
            "highlight_ids": [],
        }

    sql = _clean_sql(raw_sql)

    if sql.strip().upper() == "OFFTOPIC":
        return {
            "answer": REJECTION_MSG,
            "sql": None,
            "rows": None,
            "highlight_ids": [],
        }

    if not re.search(r"\bSELECT\b", sql, re.IGNORECASE):
        return {
            "answer": REJECTION_MSG,
            "sql": None,
            "rows": None,
            "highlight_ids": [],
        }

    if not _is_safe_select_sql(sql):
        return {
            "answer": "The generated query was not a safe SELECT statement.",
            "sql": sql,
            "rows": None,
            "highlight_ids": [],
        }

    try:
        rows = run_query(sql)
    except Exception as err:
        fix_prompt = (
            f"The following SQLite query failed.\n"
            f"Error: {err}\n"
            f"Original question: {question}\n"
            f"Failed SQL:\n{sql}\n\n"
            "Return ONLY the corrected SQL, nothing else."
        )
        try:
            raw2 = await _gemini(_build_system_prompt(), fix_prompt, temperature=0.1)
            sql = _clean_sql(raw2)

            if not _is_safe_select_sql(sql):
                return {
                    "answer": "The corrected query was not a safe SELECT statement.",
                    "sql": sql,
                    "rows": None,
                    "highlight_ids": [],
                }

            rows = run_query(sql)
        except Exception as err2:
            return {
                "answer": (
                    "I generated a SQL query but it failed to execute.\n"
                    f"Error: {err2}\n\nSQL attempted:\n{sql}"
                ),
                "sql": sql,
                "rows": None,
                "highlight_ids": [],
            }

    if not rows:
        return {
            "answer": (
                "The query returned no results. "
                "There may be no matching records for your question in the dataset."
            ),
            "sql": sql,
            "rows": [],
            "highlight_ids": [],
        }

    preview = rows[:10]
    answer_prompt = (
        f"Question: {question}\n\n"
        f"SQL used:\n{sql}\n\n"
        f"The query returned {len(rows)} row(s). First {len(preview)}:\n"
        f"{json.dumps(preview, indent=2, default=str)}\n\n"
        "Write a clear, concise answer using actual values from the data. "
        "Do not mention SQL. Keep it to 2–5 sentences."
    )

    try:
        answer_text = await _gemini(None, answer_prompt, temperature=0.3)
    except Exception:
        answer_text = f"Found {len(rows)} result(s).\n" + "\n".join(str(r) for r in preview)

    return {
        "answer": answer_text,
        "sql": sql,
        "rows": rows[:50],
        "highlight_ids": _extract_highlight_ids(rows),
    }


def _extract_highlight_ids(rows: list[dict]) -> list[str]:
    ids = []

    for row in rows[:20]:
        for col, val in row.items():
            if not val:
                continue
            cl = col.lower()
            if cl in ("salesorder", "sales_order"):
                ids.append(f"SO_{val}")
            elif cl in ("deliverydocument", "delivery_document"):
                ids.append(f"DEL_{val}")
            elif cl in ("billingdocument", "billing_document"):
                ids.append(f"BILL_{val}")
            elif cl in ("accountingdocument", "accounting_document", "paymentdocument"):
                ids.append(f"PAY_{val}")
            elif cl in ("journalentrydocument",):
                ids.append(f"JE_{val}")
            elif cl in ("businesspartner", "soldtoparty", "customer"):
                ids.append(f"BP_{val}")
            elif cl in ("material", "product"):
                ids.append(f"PRD_{val}")

    seen = set()
    result = []
    for x in ids:
        if x not in seen:
            seen.add(x)
            result.append(x)
    return result