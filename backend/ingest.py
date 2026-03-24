"""
ingest.py
---------
Reads every JSONL file from backend/data/dataset/ and loads them
into backend/db.sqlite.

Run from the backend folder:
    python ingest.py

or from the project root:
    python backend/ingest.py

The dataset must already be extracted at:
    backend/data/dataset/<folder>/*.jsonl
"""

import json
import sqlite3
import glob
import os
import sys

# Paths
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BACKEND_DIR, "db.sqlite")
DATA_ROOT = os.path.join(BACKEND_DIR, "data", "dataset")


def flatten(record: dict) -> dict:
    """
    Recursively flatten one JSONL record.
    Nested dicts like creationTime: {hours:11, minutes:31}
    become creationTime_hours, creationTime_minutes.
    All values stored as TEXT (None -> NULL).
    """
    out = {}
    for k, v in record.items():
        if isinstance(v, dict):
            for sk, sv in v.items():
                out[f"{k}_{sk}"] = None if sv is None else str(sv)
        else:
            out[k] = None if v is None else str(v)
    return out


TABLES = [
    ("sales_order_headers", "sales_order_headers/*.jsonl", None),
    ("sales_order_items", "sales_order_items/*.jsonl", None),
    ("sales_order_schedule_lines", "sales_order_schedule_lines/*.jsonl", None),
    ("delivery_headers", "outbound_delivery_headers/*.jsonl", None),
    ("delivery_items", "outbound_delivery_items/*.jsonl", None),
    ("billing_headers", "billing_document_headers/*.jsonl", None),
    ("billing_items", "billing_document_items/*.jsonl", None),
    ("billing_cancellations", "billing_document_cancellations/*.jsonl", None),
    ("journal_entries", "journal_entry_items_accounts_receivable/*.jsonl", None),
    ("payments", "payments_accounts_receivable/*.jsonl", None),
    ("business_partners", "business_partners/*.jsonl", None),
    ("business_partner_addresses", "business_partner_addresses/*.jsonl", None),
    ("customer_company", "customer_company_assignments/*.jsonl", None),
    ("customer_sales_area", "customer_sales_area_assignments/*.jsonl", None),
    ("products", "products/*.jsonl", None),
    ("product_descriptions", "product_descriptions/*.jsonl", None),
    ("plants", "plants/*.jsonl", None),
    ("product_plants", "product_plants/*.jsonl", 5000),
]

INDEXES = [
    ("sales_order_headers", "salesOrder"),
    ("sales_order_headers", "soldToParty"),
    ("sales_order_items", "salesOrder"),
    ("sales_order_items", "material"),
    ("delivery_headers", "deliveryDocument"),
    ("delivery_items", "deliveryDocument"),
    ("delivery_items", "referenceSdDocument"),
    ("delivery_items", "plant"),
    ("billing_headers", "billingDocument"),
    ("billing_headers", "accountingDocument"),
    ("billing_headers", "soldToParty"),
    ("billing_items", "billingDocument"),
    ("billing_items", "referenceSdDocument"),
    ("billing_items", "material"),
    ("journal_entries", "accountingDocument"),
    ("journal_entries", "referenceDocument"),
    ("journal_entries", "customer"),
    ("payments", "accountingDocument"),
    ("payments", "customer"),
    ("business_partners", "businessPartner"),
    ("products", "product"),
    ("product_descriptions", "product"),
    ("plants", "plant"),
]


def load_table(conn: sqlite3.Connection, table: str, pattern: str, cap: int | None) -> int:
    full = os.path.join(DATA_ROOT, pattern)
    files = sorted(glob.glob(full))

    if not files:
        print(f"  SKIP  {table}: no files at {full}")
        return 0

    rows: list[dict] = []

    for fpath in files:
        with open(fpath, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rows.append(flatten(json.loads(raw)))
                except json.JSONDecodeError:
                    continue

                if cap and len(rows) >= cap:
                    break

        if cap and len(rows) >= cap:
            break

    if not rows:
        print(f"  SKIP  {table}: 0 rows parsed")
        return 0

    all_keys = list(dict.fromkeys(k for r in rows for k in r.keys()))

    conn.execute(f'DROP TABLE IF EXISTS "{table}"')
    cols_ddl = ", ".join(f'"{c}" TEXT' for c in all_keys)
    conn.execute(f'CREATE TABLE "{table}" ({cols_ddl})')

    placeholders = ", ".join(["?"] * len(all_keys))
    conn.executemany(
        f'INSERT INTO "{table}" VALUES ({placeholders})',
        [[r.get(k) for k in all_keys] for r in rows],
    )

    capped = f" (capped at {cap})" if cap and len(rows) >= cap else ""
    print(f"  ✓  {table}: {len(rows):>5} rows  {len(all_keys)} cols{capped}")
    return len(rows)


def ingest():
    if not os.path.isdir(DATA_ROOT):
        print(f"\nERROR: dataset not found.\nExpected at: {DATA_ROOT}")
        print("Make sure the dataset exists at backend/data/dataset/")
        sys.exit(1)

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    print(f"Dataset : {DATA_ROOT}")
    print(f"Database: {DB_PATH}\n")

    total = 0
    for table, pattern, cap in TABLES:
        total += load_table(conn, table, pattern, cap)

    print("\nCreating indexes ...")
    for tbl, col in INDEXES:
        try:
            conn.execute(
                f'CREATE INDEX IF NOT EXISTS "idx_{tbl}_{col}" '
                f'ON "{tbl}" ("{col}")'
            )
        except Exception as e:
            print(f"  warn  {tbl}.{col}: {e}")

    conn.commit()
    conn.close()
    print(f"\nDone - {total} rows loaded -> {DB_PATH}")


if __name__ == "__main__":
    ingest()