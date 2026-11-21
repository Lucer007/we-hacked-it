# Equiteas API (FastAPI)

The product backend. It serves the app page and these routes (interactive docs at `/docs`):

| Route | What it does |
|---|---|
| `GET /api/health` | Status, databases in use, whether the analyst agent is on |
| `GET /api/cards` | The 12 verified company cards and their ready-made questions |
| `GET /api/companies/{ticker}/financials` | Last 5 fiscal years from DuckDB (SEC XBRL, USD) |
| `GET /api/portfolio?user=` | Holdings grouped by sector, computed in SQL |
| `POST /api/orders` | Records a demo order (no broker call), returns the portfolio |
| `GET/POST /api/wishlist` | The user's wishlist |
| `POST /api/swipes` | Logs a like or a pass (training data for the recommender) |
| `POST /api/ask` | Runs the analyst agent; needs `ANTHROPIC_API_KEY` |

Account screens (sign-up, email code, ID check) are front-end only in the prototype.

## Run it (PowerShell, from the repository root)

```powershell
pip install -r backend/requirements.txt
python -m uvicorn backend.app:app --reload --port 8000
```

Open http://127.0.0.1:8000. The header shows **Live API** when the page talks to the backend.
The same page published on claude.ai has no backend and runs on its embedded data.

Optional: `$env:ANTHROPIC_API_KEY = "..."` before starting turns on free questions in "Ask".

## Data and collaboration

- `data_pipeline/sp500/dealdeck.duckdb` (product data, public SEC filings) is in git, opened
  read-only. After `git pull` everyone has the same companies, financials and report passages.
  Rebuild it with `python data_pipeline/sp500/build_db.py` and commit the new file.
- `backend/app_state.duckdb` (orders, wishlist, swipes) is local to each developer and not in git.
  It is created on first start with the demo account's portfolio (sign in with Warren / admin).
- To share one live state between developers, point the API at MotherDuck (hosted DuckDB):
  `$env:MOTHERDUCK_TOKEN = "..."; $env:EQUITEAS_STATE_DB = "md:equiteas"`. No code change.
