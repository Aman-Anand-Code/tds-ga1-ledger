# TDS GA1 Question 15 — Ledger Agent

FastAPI service implementing `POST /answer` with request shape `{"question":"..."}` and response shape `{"answer": ...}`.

## Included data

`orders.ndjson` is the personalized export from the exam API (one JSON order per line). At startup, the app keeps the row with the latest `updated_at` for every order ID. Business date filters are evaluated in `Asia/Kolkata`. Currency conversion uses the exam-provided USD-per-unit rates: USD=1, EUR=1.05, INR=0.01165.

## Local run

```bash
python -m pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Test:

```bash
curl -X POST http://127.0.0.1:8000/answer -H 'Content-Type: application/json' -d '{"question":"What was the total revenue in USD from the North region in March 2026?"}'
```

## Render

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Set the repository's root directory to the folder containing `main.py` and `orders.ndjson`.

The service uses a deterministic query parser for common ledger questions (revenue/refunds, region, product, customer, month/year, counts, and top entities). Test the kinds of questions expected in the grader before submitting.
