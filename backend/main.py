"""
main.py
-------
FastAPI backend for the SAP O2C Graph Intelligence system.

Start from the PROJECT ROOT:
    cd backend
    uvicorn main:app --reload --port 8000

Then open:  http://localhost:8000
"""

import os
import sys

# Make sure Python finds database.py and llm.py (both in same directory)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from database import get_graph_data, get_node_detail, run_query
from llm import answer_query

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="SAP O2C Graph Intelligence", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# frontend/index.html is one level up from backend/
FRONTEND_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")
)


# ── API endpoints ─────────────────────────────────────────────────────────────

@app.get("/api/graph")
def api_graph():
    """Return all graph nodes and edges for Cytoscape.js."""
    try:
        return get_graph_data()
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="Dataset not found")
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to load graph data")


@app.get("/api/node/{node_type}/{node_id}")
def api_node_detail(node_type: str, node_id: str):
    """
    Return the full database row for a clicked node.
    node_type: customer | sales_order | delivery | billing | payment | product | journal_entry
    """
    try:
        detail = get_node_detail(node_type, node_id)
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="Dataset not found")

    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=f"No record found for {node_type} / {node_id}",
        )
    return detail


class ChatRequest(BaseModel):
    message: str


@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    """
    Accept a natural-language question, return:
      { answer, sql, rows, highlight_ids }
    """
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Empty message")
    try:
        return await answer_query(req.message)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to process query")


@app.get("/api/stats")
def api_stats():
    """Dataset counts shown in the header bar."""
    queries = {
        "sales_orders": ('SELECT COUNT(DISTINCT "salesOrder") AS n '
                         'FROM "sales_order_headers"'),
        "deliveries":   ('SELECT COUNT(DISTINCT "deliveryDocument") AS n '
                         'FROM "delivery_headers"'),
        "billing_docs": ('SELECT COUNT(DISTINCT "billingDocument") AS n '
                         'FROM "billing_headers"'),
        "payments":     ('SELECT COUNT(DISTINCT "accountingDocument") AS n '
                         'FROM "payments"'),
        "customers":    ('SELECT COUNT(DISTINCT "businessPartner") AS n '
                         'FROM "business_partners"'),
        "products":     ('SELECT COUNT(DISTINCT "product") AS n '
                         'FROM "products"'),
    }
    result: dict = {}
    for key, sql in queries.items():
        try:
            rows = run_query(sql)
            result[key] = rows[0]["n"] if rows else 0
        except Exception:
            result[key] = 0
    return result


# ── Serve frontend ────────────────────────────────────────────────────────────

@app.get("/")
def serve_index():
    idx = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(idx):
        return FileResponse(idx)
    raise HTTPException(status_code=404, detail="frontend/index.html not found")


# Mount static assets — must be registered after all explicit routes
if os.path.isdir(FRONTEND_DIR):
    app.mount(
        "/",
        StaticFiles(directory=FRONTEND_DIR, html=True),
        name="static",
    )