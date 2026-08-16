# MoStar Paper Trader — exact dashboard

This package implements the dashboard shown in the latest UI image.

## Run

```bash
cd mostar_paper_ui_exact

python paper_dashboard.py \
  --session paper_sessions/demo \
  --initial-balance 10000 \
  --host 127.0.0.1 \
  --port 8787
```

Open:

```text
http://127.0.0.1:8787
```

## Files

- `paper_dashboard.py` — backend server and JSON API
- `futures_paper_engine.py` — working paper futures ledger
- `web/index.html` — exact dashboard structure
- `web/styles.css` — exact visual styling
- `web/app.js` — live data rendering, charts, and paper close controls

## API

```text
GET  /api/status
GET  /api/trades?limit=100
GET  /api/marks?limit=300
GET  /health

POST /api/positions/close
{"trade_id":"..."}

POST /api/session/close-all
{}
```

## Safety

This is paper trading only. It uses public Binance mark prices. It has no exchange credentials and cannot place real orders.
