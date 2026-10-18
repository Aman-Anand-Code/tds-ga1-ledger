from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = Path(os.getenv("LEDGER_DATA_FILE", str(BASE_DIR / "orders.ndjson")))
BUSINESS_TZ = ZoneInfo("Asia/Kolkata")
USD_PER_UNIT = {"USD": Decimal("1"), "EUR": Decimal("1.05"), "INR": Decimal("0.01165")}
MONEY_CENT = Decimal("0.01")

REGIONS = ["Central", "North", "South", "East", "West"]
PRODUCTS = ["Air Fryer", "Blender", "Grinder", "Juicer", "Kettle", "Mixer", "Rice Cooker", "Toaster"]
MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7,
    "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

app = FastAPI(title="Acme Appliances Ledger Agent", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=BUSINESS_TZ)
        return dt
    except (ValueError, TypeError):
        return None


def load_orders() -> list[dict[str, Any]]:
    if not DATA_FILE.exists():
        raise RuntimeError(f"Ledger data file not found: {DATA_FILE}")
    newest: dict[str, dict[str, Any]] = {}
    with DATA_FILE.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Invalid JSON on line {line_no}: {exc}") from exc
            order_id = str(row.get("id", ""))
            if not order_id:
                continue
            old = newest.get(order_id)
            new_dt = parse_dt(row.get("updated_at"))
            old_dt = parse_dt(old.get("updated_at")) if old else None
            if old is None or (new_dt and (old_dt is None or new_dt > old_dt)):
                newest[order_id] = row
    return list(newest.values())


ORDERS = load_orders()


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


def money_usd(row: dict[str, Any]) -> Decimal:
    amount = Decimal(str(row.get("amount", 0)))
    currency = str(row.get("currency", "USD")).upper()
    return amount * USD_PER_UNIT.get(currency, Decimal("1"))


def round_money(value: Decimal) -> float:
    return float(value.quantize(MONEY_CENT, rounding=ROUND_HALF_UP))


def get_filters(question: str) -> dict[str, Any]:
    q = norm(question)
    filters: dict[str, Any] = {"region": None, "product": None, "customer": None, "order_id": None, "month": None, "year": None, "start_month": None, "end_month": None}

    # Match a specific customer/order identifier first.
    cm = re.search(r"\bc\s*[-#]?\s*(\d{4})\b", q)
    if cm:
        filters["customer"] = "C" + cm.group(1)
    om = re.search(r"\bord\s*[-#]?\s*(\d{4,})\b", q, re.I)
    if om:
        filters["order_id"] = "ORD-" + om.group(1)

    for region in REGIONS:
        if re.search(rf"\b{re.escape(region.lower())}\b", q):
            # Avoid interpreting the word 'east' in unrelated text? Usually fine.
            filters["region"] = region
            break

    for product in sorted(PRODUCTS, key=len, reverse=True):
        base = product.lower()
        # Accept API spelling as well as common plural/spacing variants.
        variants = {base, base + "s", base.replace(" ", ""), base.replace(" ", "") + "s"}
        if base == "air fryer":
            variants |= {"airfryers"}
        if base == "rice cooker":
            variants |= {"ricecookers"}
        if any(re.search(rf"\b{re.escape(v)}\b", q) for v in variants):
            filters["product"] = product
            break

    ym = re.search(r"\b(20\d{2})\b", q)
    if ym:
        filters["year"] = int(ym.group(1))

    # Quarter filters (for example, Q1 2026 or first quarter of 2026).
    qm = re.search(r"\bq([1-4])(?:\s*(20\d{2}))?\b", q)
    if not qm:
        ordinal_quarters = {"first": 1, "second": 2, "third": 3, "fourth": 4}
        for word, qnum in ordinal_quarters.items():
            if re.search(rf"\b{word}\s+quarter\b", q):
                qm = (qnum, str(filters["year"]) if filters["year"] else None)
                break
    if qm:
        qnum = int(qm.group(1)) if hasattr(qm, "group") else int(qm[0])
        filters["start_month"] = (qnum - 1) * 3 + 1
        filters["end_month"] = filters["start_month"] + 2
        filters["month"] = None
        qyear = qm.group(2) if hasattr(qm, "group") else qm[1]
        if qyear:
            filters["year"] = int(qyear)
    else:
        month_hits = []
        for name, num in MONTHS.items():
            if re.search(rf"\b{re.escape(name)}\b", q):
                month_hits.append(num)
        # If two months are named in a range (e.g., between March and May), include both endpoints.
        if len(set(month_hits)) >= 2 and re.search(r"\b(between|from|through|to|until)\b", q):
            filters["start_month"] = min(month_hits)
            filters["end_month"] = max(month_hits)
        elif month_hits:
            filters["month"] = month_hits[0]

    return filters


def row_local_datetime(row: dict[str, Any], field: str = "created_at") -> datetime | None:
    dt = parse_dt(row.get(field))
    return dt.astimezone(BUSINESS_TZ) if dt else None


def choose_rows(question: str, filters: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    q = norm(question)
    intent_status = "all"
    if re.search(r"\b(refund|refunded|refunds|refund amount|refund value)\b", q):
        intent_status = "refunded"
    elif re.search(r"\b(void|voided|cancelled|canceled)\b", q):
        intent_status = "void"
    elif re.search(r"\b(revenue|sales|paid|earn|earns|earned|earning|earnings|turnover|income|generate|generated|made|bring in|brought in|sold|spend|spent|spending|purchase|purchases|purchased)\b", q):
        intent_status = "paid"

    rows = []
    for row in ORDERS:
        if intent_status != "all" and str(row.get("status", "")).lower() != intent_status:
            continue
        if filters["region"] and row.get("region") != filters["region"]:
            continue
        if filters["product"] and row.get("product") != filters["product"]:
            continue
        if filters["customer"] and row.get("customer") != filters["customer"]:
            continue
        if filters["order_id"] and row.get("id") != filters["order_id"]:
            continue
        dt = row_local_datetime(row, "created_at")
        if filters["month"] is not None and (dt is None or dt.month != filters["month"]):
            continue
        if filters["year"] is not None and (dt is None or dt.year != filters["year"]):
            continue
        if filters["start_month"] is not None and (dt is None or not (filters["start_month"] <= dt.month <= filters["end_month"])):
            continue
        rows.append(row)
    return rows, intent_status


def is_count_query(q: str) -> bool:
    return bool(re.search(r"\b(how many|number of|count|count of|quantity of orders|number of orders)\b", q))


def is_quantity_query(q: str) -> bool:
    return bool(re.search(r"\b(units|items|pieces|quantity sold|how much was sold|total quantity|total units)\b", q))


def aggregate(rows: list[dict[str, Any]], q: str, intent_status: str) -> Any:
    # Resolve group-by questions that ask for the best/worst entity.
    group_dim = None
    if re.search(r"\b(product|products|item|items)\b", q):
        group_dim = "product"
    elif re.search(r"\b(customer|customers|client|clients|buyer|buyers)\b", q):
        group_dim = "customer"
    elif re.search(r"\b(region|regions)\b", q):
        group_dim = "region"

    winner_words = bool(re.search(r"\b(which|what|who).{0,80}\b(highest|most|largest|top|biggest|greatest|lowest|least|smallest)\b|\b(highest|most|largest|top|biggest|greatest|lowest|least|smallest).{0,80}\b(which|what|who)\b", q))
    winner_words = winner_words or bool(re.search(r"\b(top|best|worst)\s+(selling\s+)?(product|customer|region)\b", q))
    if group_dim and winner_words:
        if is_quantity_query(q) or re.search(r"\b(units sold|most units|highest quantity|quantity sold|total quantity)\b", q):
            metric = "qty"
        elif re.search(r"\b(number|count|how many|most orders|number of orders|most purchases|number of purchases)\b", q):
            metric = "count"
        elif re.search(r"\b(refund|refunded|refunds)\b", q) and not re.search(r"\b(amount|value|total|sum|dollar|usd|money|revenue)\b", q):
            metric = "count"
        else:
            metric = "money"
        sums: dict[str, Decimal] = defaultdict(Decimal)
        counts: dict[str, int] = defaultdict(int)
        for row in rows:
            key = str(row.get(group_dim, ""))
            if metric == "qty":
                sums[key] += Decimal(str(row.get("qty", 0)))
            elif metric == "count":
                sums[key] += Decimal("1")
            else:
                sums[key] += money_usd(row)
            counts[key] += 1
        if not sums:
            return None
        reverse = not bool(re.search(r"\b(lowest|least|smallest|worst)\b", q))
        ordered = sorted(sums.items(), key=lambda kv: ((-kv[1] if reverse else kv[1]), kv[0]))
        best_key, best_value = ordered[0]
        return best_key

    if is_quantity_query(q):
        return int(sum(int(r.get("qty", 0)) for r in rows))

    if is_count_query(q):
        if re.search(r"\b(unique|distinct)\s+customers\b|\bhow many customers\b|\bnumber of customers\b", q):
            return len({r.get("customer") for r in rows})
        if re.search(r"\bproducts\b", q) and re.search(r"\b(unique|distinct|different|how many)\b", q):
            return len({r.get("product") for r in rows})
        return len(rows)

    if re.search(r"\b(average|avg|mean)\b", q):
        if not rows:
            return None
        total = sum((money_usd(r) for r in rows), Decimal("0"))
        return round_money(total / len(rows))

    # Specific order lookups can ask for its customer, product, status, region or amount.
    if re.search(r"\b(status|state)\b", q) and len(rows) == 1:
        return rows[0].get("status")
    if re.search(r"\bcustomer\b", q) and filters_have_order_id(q) and len(rows) == 1:
        return rows[0].get("customer")
    if re.search(r"\bproduct\b", q) and filters_have_order_id(q) and len(rows) == 1:
        return rows[0].get("product")
    if re.search(r"\b(region)\b", q) and filters_have_order_id(q) and len(rows) == 1:
        return rows[0].get("region")

    # Max/min individual order amount.
    if re.search(r"\b(maximum|max|min|minimum|largest|biggest|smallest|highest|lowest)\s+(order|transaction|sale|refund)", q) and rows:
        rev = not bool(re.search(r"\b(minimum|min|smallest|lowest)\b", q))
        r = sorted(rows, key=lambda x: money_usd(x), reverse=rev)[0]
        if re.search(r"\bwhich order|order id|which transaction\b", q):
            return r.get("id")
        return round_money(money_usd(r))

    # Monetary aggregation defaults: revenue = paid only; refunds = refunded only; generic amount uses current rows.
    if re.search(r"\b(total|sum|overall|combined|amount|value|revenue|sales|refund|refunds|turnover|earn|earns|earned|earning|earnings|income|generated|generate|made|brought in)\b", q):
        return round_money(sum((money_usd(r) for r in rows), Decimal("0")))

    # If question asks for the customer/product/region on a single matching result, return that field.
    if len(rows) == 1:
        r = rows[0]
        if "customer" in q:
            return r.get("customer")
        if "product" in q:
            return r.get("product")
        if "region" in q:
            return r.get("region")
        if "status" in q:
            return r.get("status")
        if re.search(r"\b(amount|value|price|revenue)\b", q):
            return round_money(money_usd(r))

    # Safe fallback: total value of paid rows for revenue/sales, otherwise row count.
    if intent_status == "paid" or re.search(r"\b(revenue|sales|paid)\b", q):
        return round_money(sum((money_usd(r) for r in rows), Decimal("0")))
    return len(rows)


def filters_have_order_id(q: str) -> bool:
    return bool(re.search(r"\bord\s*[-#]?\s*\d{4,}\b", q, re.I))


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


@app.get("/")
def root() -> dict[str, Any]:
    return {"service": "Acme Appliances order ledger", "status": "ok", "records": len(ORDERS), "endpoint": "/answer"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/answer")
def answer(req: AskRequest) -> dict[str, Any]:
    q = req.question.strip()
    if not q:
        raise HTTPException(status_code=400, detail="question must not be empty")
    filters = get_filters(q)
    rows, status = choose_rows(q, filters)
    result = aggregate(rows, norm(q), status)
    return {"answer": result}
