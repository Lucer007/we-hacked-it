"""Equiteas API: the product backend.

Serves the app page and the product routes: company cards, 5-year financials, portfolio by sector,
orders, wishlist, swipes, and the analyst agent. Account screens (sign-up, email code, ID check)
stay front-end only in the prototype.

Run from the repository root:
  pip install -r backend/requirements.txt
  uvicorn backend.app:app --reload --port 8000
Then open http://127.0.0.1:8000 (the app) or http://127.0.0.1:8000/docs (the API).

Data:
  data_pipeline/sp500/dealdeck.duckdb  product data from SEC filings, in git, opened read-only.
  backend/app_state.duckdb             orders, wishlist, swipes. Local to each developer, not in git.
                                       To share one live database, set EQUITEAS_STATE_DB="md:equiteas"
                                       (MotherDuck, with MOTHERDUCK_TOKEN set).
  ANTHROPIC_API_KEY                    enables POST /api/ask (the analyst agent).
"""

import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import duckdb
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "prototype"))
sys.path.insert(0, str(ROOT / "data_pipeline" / "sp500"))

from build_prototype import DB_PATH, build_data, render_page  # noqa: E402

STATE_DB = os.environ.get("EQUITEAS_STATE_DB", str(ROOT / "backend" / "app_state.duckdb"))
DEMO_USER = "warren"
# Sample holdings of the demo account (sign in with Warren / admin), in euros. TSLA and GOOGL are left
# out so a purchase during the demo adds a new sector to the portfolio map.
DEMO_PORTFOLIO = {"NVDA": 40, "AMD": 20, "TSM": 15, "AAPL": 30, "MSFT": 25, "AMZN": 20, "WMT": 10, "KO": 20, "LLY": 25, "JPM": 15}

DATA = build_data()
CARDS = {card["id"]: card for card in DATA["cards"]}
# The page is rebuilt when the template or a brand image changes, so front-end edits show on the
# next reload without restarting the server (the company data is read once, at start).
PAGE_FILES = [ROOT / "prototype" / "app.template.html", *sorted((ROOT / "prototype" / "assets").glob("*"))]
_page = {"stamp": None, "html": ""}


def current_page() -> str:
    stamp = tuple(f.stat().st_mtime_ns for f in PAGE_FILES)
    if stamp != _page["stamp"]:
        _page["html"], _page["stamp"] = render_page(DATA), stamp
    return _page["html"]


data_db = duckdb.connect(str(DB_PATH), read_only=True)
state_db = duckdb.connect(STATE_DB)
lock = threading.Lock()  # one DuckDB connection per database, shared by the worker threads


def sql(db, query: str, params: list | None = None) -> list[tuple]:
    with lock:
        return db.execute(query, params or []).fetchall()


def init_state():
    with lock:
        state_db.execute("create sequence if not exists order_id")
        state_db.execute("""create table if not exists orders (
            id bigint default nextval('order_id') primary key, user_id varchar not null, ticker varchar not null,
            amount_eur double not null, created_at timestamp not null)""")
        state_db.execute("""create table if not exists wishlist (
            user_id varchar not null, ticker varchar not null, added_at timestamp not null, primary key (user_id, ticker))""")
        state_db.execute("""create table if not exists swipes (
            user_id varchar not null, ticker varchar not null, direction varchar not null, swiped_at timestamp not null)""")
        # The app's sector names (Chips, Tech, Food & drinks...) for the portfolio query.
        state_db.execute("create or replace table sectors (ticker varchar primary key, name varchar, sector varchar)")
        state_db.executemany("insert into sectors values (?, ?, ?)", [(c["id"], c["name"], c["sector"]) for c in CARDS.values()])
        if not state_db.execute("select count(*) from orders where user_id = ?", [DEMO_USER]).fetchone()[0]:
            now = datetime.now(timezone.utc)
            state_db.executemany("insert into orders (user_id, ticker, amount_eur, created_at) values (?, ?, ?, ?)",
                                 [(DEMO_USER, t, a, now) for t, a in DEMO_PORTFOLIO.items()])


init_state()

app = FastAPI(title="Equiteas API", version="0.1",
              description="Product backend of the Equiteas prototype: cards from SEC filings, portfolio, orders, analyst agent.")


def check_ticker(ticker: str) -> str:
    ticker = ticker.upper()
    if ticker not in CARDS:
        raise HTTPException(404, f"Unknown company {ticker}. This prototype covers: {', '.join(sorted(CARDS))}.")
    return ticker


def portfolio(user: str) -> dict:
    rows = sql(state_db, """
        select s.sector, o.ticker, s.name, sum(o.amount_eur) as amount
        from orders o join sectors s using (ticker)
        where o.user_id = ?
        group by all
        order by s.sector, amount desc""", [user])
    sectors: dict[str, dict] = {}
    for sector, ticker, name, amount in rows:
        entry = sectors.setdefault(sector, {"sector": sector, "companies": 0, "amount": 0.0, "holdings": []})
        entry["companies"] += 1
        entry["amount"] += amount
        entry["holdings"].append({"ticker": ticker, "name": name, "amount": amount})
    ordered = sorted(sectors.values(), key=lambda s: (-s["companies"], -s["amount"]))
    return {"user": user, "total": sum(s["amount"] for s in ordered), "sectors": ordered}


# ---------------------------------------------------------------- routes

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def page():
    return current_page()


@app.get("/api/health")
def health():
    return {"ok": True, "companies": len(CARDS), "data_db": DB_PATH.name,
            "state_db": "motherduck" if STATE_DB.startswith("md:") else "local",
            "agent": bool(os.environ.get("ANTHROPIC_API_KEY"))}


@app.get("/api/cards")
def cards():
    return DATA


@app.get("/api/companies/{ticker}/financials")
def financials(ticker: str):
    """Last five fiscal years, straight from the SEC XBRL data in DuckDB (USD)."""
    ticker = check_ticker(ticker)
    rows = sql(data_db, """
        select fiscal_year, fiscal_year_end, revenue, net_income, dividends_paid, source_url
        from annual where ticker = ? and revenue is not null
        order by fiscal_year desc limit 5""", [ticker])
    return {"ticker": ticker, "currency": "USD", "years": [
        {"fiscal_year": y, "fiscal_year_end": str(end), "revenue": rev, "net_income": ni, "dividends_paid": div, "source_url": url}
        for y, end, rev, ni, div, url in reversed(rows)]}


@app.get("/api/companies/{ticker}/prices")
def prices(ticker: str):
    """Share-price history (1 year daily, 5 years weekly), snapshot from data_pipeline/sp500/fetch_prices.py."""
    ticker = check_ticker(ticker)
    series = CARDS[ticker].get("prices")
    if not series:
        raise HTTPException(404, f"No price data for {ticker}. Run data_pipeline/sp500/fetch_prices.py.")
    return {"ticker": ticker, "source": DATA.get("prices_source"), "as_of": DATA.get("prices_as_of"), **series}


@app.get("/api/portfolio")
def get_portfolio(user: str = DEMO_USER):
    return portfolio(user.lower())


class Order(BaseModel):
    user: str = Field(min_length=1, max_length=120)
    ticker: str
    amount: float = Field(gt=0, le=10000, description="Amount in euros")


@app.post("/api/orders")
def place_order(order: Order):
    """Records a demo order (no broker call) and returns the updated portfolio."""
    ticker = check_ticker(order.ticker)
    with lock:
        state_db.execute("insert into orders (user_id, ticker, amount_eur, created_at) values (?, ?, ?, ?)",
                         [order.user.lower(), ticker, order.amount, datetime.now(timezone.utc)])
    return portfolio(order.user.lower())


class Pick(BaseModel):
    user: str = Field(min_length=1, max_length=120)
    ticker: str


@app.get("/api/wishlist")
def get_wishlist(user: str = DEMO_USER):
    rows = sql(state_db, "select ticker from wishlist where user_id = ? order by added_at", [user.lower()])
    return {"user": user.lower(), "tickers": [r[0] for r in rows]}


@app.post("/api/wishlist")
def add_to_wishlist(pick: Pick):
    ticker = check_ticker(pick.ticker)
    with lock:
        state_db.execute("insert or ignore into wishlist values (?, ?, ?)", [pick.user.lower(), ticker, datetime.now(timezone.utc)])
    return get_wishlist(pick.user)


class Swipe(Pick):
    direction: str = Field(pattern="^(like|pass)$")


@app.post("/api/swipes")
def record_swipe(swipe: Swipe):
    ticker = check_ticker(swipe.ticker)
    with lock:
        state_db.execute("insert into swipes values (?, ?, ?, ?)", [swipe.user.lower(), ticker, swipe.direction, datetime.now(timezone.utc)])
    return {"ok": True}


class Question(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    ticker: str | None = None


@app.post("/api/ask")
def ask(q: Question):
    """Runs the analyst agent: SQL on DuckDB + search in the 10-Ks, cited answer, grounding and no-advice checks."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(503, "The analyst agent needs an Anthropic API key: set ANTHROPIC_API_KEY and restart the API.")
    import analyst  # imported here so the API starts without the agent's dependencies
    question = q.question
    if q.ticker:
        ticker = check_ticker(q.ticker)
        question = f"{q.question} (The user is asking about {CARDS[ticker]['name']}, ticker {ticker}.)"
    try:
        return analyst.run_agent(question, verbose=False)
    except SystemExit as exc:  # the agent stops with SystemExit on a refusal or a turn limit
        raise HTTPException(502, f"The analyst agent stopped: {exc}") from None
