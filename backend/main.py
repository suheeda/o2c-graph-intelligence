"""
main.py
-------
FastAPI backend for the SAP O2C Graph Intelligence system.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from database import get_graph_data, get_node_detail, run_query
from llm import answer_query

app = FastAPI(title="SAP O2C Graph Intelligence", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "frontend"))


@app.get("/api/graph")
def api_graph():
    try:
        return get_graph_data()
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load graph data: {e}")


@app.get("/api/node/{node_type}/{node_id}")
def api_node_detail(node_type: str, node_id: str):
    try:
        detail = get_node_detail(node_type, node_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))

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
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Empty message")
    try:
        return await answer_query(req.message)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to process query: {e}")


@app.get("/api/stats")
def api_stats():
    queries = {
        "sales_orders": ('SELECT COUNT(DISTINCT "salesOrder") AS n FROM "sales_order_headers"'),
        "deliveries":   ('SELECT COUNT(DISTINCT "deliveryDocument") AS n FROM "delivery_headers"'),
        "billing_docs": ('SELECT COUNT(DISTINCT "billingDocument") AS n FROM "billing_headers"'),
        "payments":     ('SELECT COUNT(DISTINCT "accountingDocument") AS n FROM "payments"'),
        "customers":    ('SELECT COUNT(DISTINCT "businessPartner") AS n FROM "business_partners"'),
        "products":     ('SELECT COUNT(DISTINCT "product") AS n FROM "products"'),
    }

    result = {}
    for key, sql in queries.items():
        try:
            rows = run_query(sql)
            result[key] = rows[0]["n"] if rows else 0
        except Exception:
            result[key] = 0
    return result


@app.get("/")
def serve_index():
    idx = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(idx):
        return FileResponse(idx)
    raise HTTPException(status_code=404, detail="frontend/index.html not found")


if os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")