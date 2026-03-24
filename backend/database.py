"""
database.py
-----------
SQLite access layer + graph-data builder.

VERIFIED JOIN MAP (confirmed against real dataset):
────────────────────────────────────────────────────
sales_order_headers.salesOrder
    = sales_order_items.salesOrder
    = delivery_items.referenceSdDocument
    = sales_order_schedule_lines.salesOrder

delivery_items.deliveryDocument
    = delivery_headers.deliveryDocument
    = billing_items.referenceSdDocument

billing_items.billingDocument
    = billing_headers.billingDocument

billing_headers.accountingDocument
    = journal_entries.accountingDocument
    = payments.accountingDocument

billing_headers.billingDocument
    = journal_entries.referenceDocument

business_partners.businessPartner
    = sales_order_headers.soldToParty
    = billing_headers.soldToParty
    = payments.customer
    = journal_entries.customer

sales_order_items.material  =  products.product  =  product_descriptions.product
billing_items.material      =  products.product  =  product_descriptions.product
delivery_items.plant        =  plants.plant
"""

import os
import sqlite3

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BACKEND_DIR, "db.sqlite")


# ── Connection ────────────────────────────────────────────────────────────────
def get_conn() -> sqlite3.Connection:
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(
            f"Database not found: {DB_PATH}\n"
            "Run: python backend/ingest.py"
        )
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── Schema string for LLM ────────────────────────────────────────────────────
def get_schema_for_llm() -> str:
    """
    Returns every table + column in O2C flow order.
    Used verbatim inside the LLM system prompt.
    """
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


# ── Generic query runner ──────────────────────────────────────────────────────
def run_query(sql: str, params=()) -> list[dict]:
    """Execute any SELECT and return a list of dicts. Raises on error."""
    conn = get_conn()
    try:
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# ── Graph data ────────────────────────────────────────────────────────────────
def get_graph_data() -> dict:
    """
    Build the node + edge graph for the Cytoscape.js frontend.

    Node types:
      customer, sales_order, delivery, billing, payment, product, journal_entry

    Edge labels:
      placed, delivered_by, billed_as, settled_by, contains, posted_to
    """
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

        # ------------------------------------------------------------------
        # 1. Customers
        # ------------------------------------------------------------------
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
                {
                    "businessPartner": bp,
                    "name": name,
                },
            )

        # ------------------------------------------------------------------
        # 2. Sales Orders
        # ------------------------------------------------------------------
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

        # ------------------------------------------------------------------
        # 3. Deliveries
        # ------------------------------------------------------------------
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

        # ------------------------------------------------------------------
        # 4. Billing Documents
        # ------------------------------------------------------------------
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

        # ------------------------------------------------------------------
        # 5. Payments
        # ------------------------------------------------------------------
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

        # ------------------------------------------------------------------
        # 6. Products
        # ------------------------------------------------------------------
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

        # ------------------------------------------------------------------
        # 7. Journal Entries
        # ------------------------------------------------------------------
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

        # ------------------------------------------------------------------
        # Final edge cleanup
        # ------------------------------------------------------------------
        valid_ids = {n["id"] for n in nodes}
        clean_edges = [
            edge
            for edge in edges
            if edge["source"] in valid_ids and edge["target"] in valid_ids
        ]

        return {
            "nodes": nodes,
            "edges": clean_edges,
        }

    finally:
        conn.close()


# ── Node detail ───────────────────────────────────────────────────────────────
def get_node_detail(node_type: str, node_id: str) -> dict | None:
    """Return the full DB row for a clicked graph node."""
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