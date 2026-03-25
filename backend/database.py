"""
database.py
-----------
SQLite access layer + graph-data builder.
"""

import os
import sqlite3

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BACKEND_DIR, "db.sqlite")
DATASET_DIR = os.path.join(BACKEND_DIR, "data", "dataset")


def ensure_db():
    """
    Auto-create db.sqlite from the dataset if it doesn't exist yet.
    Works on Render and locally.
    """
    if os.path.exists(DB_PATH):
        return

    if not os.path.isdir(DATASET_DIR):
        raise FileNotFoundError(
            f"Dataset not found: {DATASET_DIR}"
        )

    from ingest import ingest
    ingest()

    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(
            f"Database not created: {DB_PATH}"
        )


def get_conn() -> sqlite3.Connection:
    ensure_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_schema_for_llm() -> str:
    conn = get_conn()
    try:
        existing = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        ordered = [
            "sales_order_headers",
            "sales_order_items",
            "sales_order_schedule_lines",
            "delivery_headers",
            "delivery_items",
            "billing_headers",
            "billing_items",
            "billing_cancellations",
            "journal_entries",
            "payments",
            "business_partners",
            "business_partner_addresses",
            "customer_company",
            "customer_sales_area",
            "products",
            "product_descriptions",
            "plants",
            "product_plants",
        ]

        lines = []
        for table_name in ordered:
            if table_name not in existing:
                continue

            cols = [
                c[1]
                for c in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
            ]
            lines.append(f"  {table_name}({', '.join(cols)})")

        return "\n".join(lines)
    finally:
        conn.close()


def run_query(sql: str, params=()) -> list[dict]:
    conn = get_conn()
    try:
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_graph_data() -> dict:
    conn = get_conn()

    try:
        nodes: list[dict] = []
        edges: list[dict] = []
        seen_nodes: set[str] = set()
        seen_edges: set[tuple[str, str, str]] = set()

        def add_node(node_id: str, label: str, node_type: str, data: dict):
            if not node_id or node_id in seen_nodes:
                return
            seen_nodes.add(node_id)
            nodes.append({
                "id": node_id,
                "label": label,
                "type": node_type,
                "data": data,
            })

        def add_edge(source: str, target: str, label: str):
            if not source or not target:
                return
            key = (source, target, label)
            if key in seen_edges:
                return
            seen_edges.add(key)
            edges.append({
                "source": source,
                "target": target,
                "label": label,
            })

        for row in conn.execute("""
            SELECT businessPartner, businessPartnerFullName
            FROM business_partners
            LIMIT 60
        """).fetchall():
            bp = row["businessPartner"]
            name = row["businessPartnerFullName"] or bp
            add_node(
                f"BP_{bp}",
                name,
                "customer",
                {"businessPartner": bp, "name": name},
            )

        for row in conn.execute("""
            SELECT salesOrder,
                   soldToParty,
                   totalNetAmount,
                   transactionCurrency,
                   overallDeliveryStatus,
                   creationDate
            FROM sales_order_headers
            LIMIT 40
        """).fetchall():
            so = row["salesOrder"]
            party = row["soldToParty"]

            add_node(
                f"SO_{so}",
                f"SO {so}",
                "sales_order",
                {
                    "salesOrder": so,
                    "soldToParty": party,
                    "totalNetAmount": row["totalNetAmount"],
                    "transactionCurrency": row["transactionCurrency"],
                    "overallDeliveryStatus": row["overallDeliveryStatus"],
                    "creationDate": (row["creationDate"] or "")[:10],
                },
            )

            add_edge(f"BP_{party}", f"SO_{so}", "placed")

        for row in conn.execute("""
            SELECT DISTINCT
                   di.deliveryDocument,
                   di.referenceSdDocument,
                   dh.overallGoodsMovementStatus,
                   dh.shippingPoint
            FROM delivery_items di
            JOIN delivery_headers dh
              ON dh.deliveryDocument = di.deliveryDocument
            LIMIT 40
        """).fetchall():
            delivery_id = row["deliveryDocument"]
            sales_order_ref = row["referenceSdDocument"]

            add_node(
                f"DEL_{delivery_id}",
                f"DEL {delivery_id}",
                "delivery",
                {
                    "deliveryDocument": delivery_id,
                    "referenceSdDocument": sales_order_ref,
                    "overallGoodsMovementStatus": row["overallGoodsMovementStatus"],
                    "shippingPoint": row["shippingPoint"],
                },
            )

            add_edge(f"SO_{sales_order_ref}", f"DEL_{delivery_id}", "delivered_by")

        for row in conn.execute("""
            SELECT DISTINCT
                   bh.billingDocument,
                   bi.referenceSdDocument,
                   bh.totalNetAmount,
                   bh.billingDocumentIsCancelled,
                   bh.accountingDocument,
                   bh.billingDocumentType,
                   bh.soldToParty
            FROM billing_headers bh
            JOIN billing_items bi
              ON bi.billingDocument = bh.billingDocument
            LIMIT 40
        """).fetchall():
            billing_id = row["billingDocument"]
            delivery_ref = row["referenceSdDocument"]

            add_node(
                f"BILL_{billing_id}",
                f"BILL {billing_id}",
                "billing",
                {
                    "billingDocument": billing_id,
                    "referenceSdDocument": delivery_ref,
                    "totalNetAmount": row["totalNetAmount"],
                    "billingDocumentIsCancelled": row["billingDocumentIsCancelled"],
                    "accountingDocument": row["accountingDocument"],
                    "billingDocumentType": row["billingDocumentType"],
                    "soldToParty": row["soldToParty"],
                },
            )

            add_edge(f"DEL_{delivery_ref}", f"BILL_{billing_id}", "billed_as")

        billing_by_acc = {}
        for row in conn.execute("""
            SELECT billingDocument, accountingDocument
            FROM billing_headers
            WHERE accountingDocument IS NOT NULL
        """).fetchall():
            acc = row["accountingDocument"]
            bill = row["billingDocument"]
            if acc and acc not in billing_by_acc:
                billing_by_acc[acc] = bill

        for row in conn.execute("""
            SELECT accountingDocument,
                   customer,
                   amountInTransactionCurrency,
                   transactionCurrency,
                   clearingDate
            FROM payments
            LIMIT 30
        """).fetchall():
            acc = row["accountingDocument"]
            cust = row["customer"]

            add_node(
                f"PAY_{acc}",
                f"PAY {acc}",
                "payment",
                {
                    "accountingDocument": acc,
                    "customer": cust,
                    "amountInTransactionCurrency": row["amountInTransactionCurrency"],
                    "transactionCurrency": row["transactionCurrency"],
                    "clearingDate": (row["clearingDate"] or "")[:10],
                },
            )

            billing_doc = billing_by_acc.get(acc)
            if billing_doc:
                add_edge(f"BILL_{billing_doc}", f"PAY_{acc}", "settled_by")

        for row in conn.execute("""
            SELECT DISTINCT
                   soi.material,
                   pd.productDescription,
                   soi.salesOrder
            FROM sales_order_items soi
            LEFT JOIN product_descriptions pd
              ON pd.product = soi.material
            WHERE soi.material IS NOT NULL
            LIMIT 35
        """).fetchall():
            product_id = row["material"]
            product_name = row["productDescription"] or product_id
            sales_order = row["salesOrder"]

            add_node(
                f"PRD_{product_id}",
                product_name,
                "product",
                {
                    "product": product_id,
                    "productDescription": product_name,
                },
            )

            add_edge(f"SO_{sales_order}", f"PRD_{product_id}", "contains")

        for row in conn.execute("""
            SELECT DISTINCT
                   je.accountingDocument,
                   je.referenceDocument,
                   je.customer
            FROM journal_entries je
            WHERE je.accountingDocument IS NOT NULL
            LIMIT 25
        """).fetchall():
            je_id = row["accountingDocument"]
            billing_ref = row["referenceDocument"]

            add_node(
                f"JE_{je_id}",
                f"JE {je_id}",
                "journal_entry",
                {
                    "accountingDocument": je_id,
                    "referenceDocument": billing_ref,
                    "customer": row["customer"],
                },
            )

            if billing_ref:
                add_edge(f"BILL_{billing_ref}", f"JE_{je_id}", "posted_to")

        valid_ids = {n["id"] for n in nodes}
        clean_edges = [
            edge for edge in edges
            if edge["source"] in valid_ids and edge["target"] in valid_ids
        ]

        return {"nodes": nodes, "edges": clean_edges}

    finally:
        conn.close()


def get_node_detail(node_type: str, node_id: str) -> dict | None:
    mapping = {
        "customer": ("business_partners", "businessPartner"),
        "sales_order": ("sales_order_headers", "salesOrder"),
        "delivery": ("delivery_headers", "deliveryDocument"),
        "billing": ("billing_headers", "billingDocument"),
        "payment": ("payments", "accountingDocument"),
        "product": ("products", "product"),
        "journal_entry": ("journal_entries", "accountingDocument"),
    }

    if node_type not in mapping:
        return None

    table_name, pk = mapping[node_type]
    rows = run_query(
        f'SELECT * FROM "{table_name}" WHERE "{pk}" = ? LIMIT 1',
        (node_id,),
    )
    return rows[0] if rows else None