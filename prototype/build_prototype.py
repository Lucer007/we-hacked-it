"""Build the standalone prototype page (index.html) from the verified swipe cards.

Reads data_pipeline/sp500/profile_cards.json, adds the ready-made questions for the "Ask"
screen (answers built only from verified quotes), checks the extra quotes of the demo
question against the reports, and writes index.html from app.template.html.

Usage:
  python build_prototype.py
"""

import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPELINE = HERE.parent / "data_pipeline" / "sp500"
sys.path.insert(0, str(PIPELINE))

import duckdb  # noqa: E402

from build_profile_cards import DB_PATH, locate, report  # noqa: E402

# Friendly sector names for the preference question.
FRIENDLY_SECTORS = {
    "NVDA": "Chips", "AMD": "Chips", "TSM": "Chips", "AAPL": "Tech", "MSFT": "Tech", "GOOGL": "Internet",
    "AMZN": "Retail", "WMT": "Retail", "TSLA": "Cars & energy", "KO": "Food & drinks", "JPM": "Finance",
    "LLY": "Health",
}

# Names as people say them (the S&P list has "Alphabet Inc. (Class A)", "Lilly (Eli)"...).
DISPLAY_NAMES = {
    "NVDA": "Nvidia", "AMD": "AMD", "TSM": "TSMC", "AAPL": "Apple", "MSFT": "Microsoft", "GOOGL": "Alphabet (Google)",
    "AMZN": "Amazon", "WMT": "Walmart", "TSLA": "Tesla", "KO": "Coca-Cola", "JPM": "JPMorgan Chase", "LLY": "Eli Lilly",
}

# The demo's multi-company question, answered from two reports.
COMPARISON = {
    "question": "Is Nvidia more dependent on a few customers than AMD?",
    "tickers": ["NVDA", "AMD"],
    "answer": [
        {"text": "Yes, by a wide margin. In fiscal 2026, one direct customer made up 22% of Nvidia's revenue and another made up 14%.",
         "quote": ("NVDA", "For fiscal year 2026, sales to one direct customer represented 22% of total revenue and sales to another direct customer represented 14% of total revenue")},
        {"text": "AMD says no single customer reached 10% of its revenue in 2025 or 2024.",
         "quote": ("AMD", "No customer accounted for at least 10% of the Company's consolidated net revenue in fiscal years 2025 and 2024.")},
    ],
}


def quote_entry(source: dict, n: int, label: str) -> dict:
    return {"n": n, "text": source["quote"], "section": source["section"], "url": source["url"], "report": label}


def build_questions(card: dict, report_label: str) -> list[dict]:
    name, front, back, sources = card["name"], card["front"], card["back"], card["sources"]
    questions = []

    def answer(question, parts):
        quotes, out = [], []
        for text, field, index in parts:
            refs = []
            if field and len(sources.get(field, [])) > index:
                quotes.append(quote_entry(sources[field][index], len(quotes) + 1, report_label))
                refs = [len(quotes)]
            out.append({"text": text, "refs": refs})
        questions.append({"question": question, "answer": out, "quotes": quotes})

    if front["what_they_do"]:
        parts = [(front["what_they_do"], "what_they_do", 0)]
        if back["segments"]:
            parts.append((f"It reports its results in {len(back['segments'])} segment"
                          f"{'s' if len(back['segments']) > 1 else ''}: {', '.join(back['segments'])}.", "segments", 0))
        answer(f"What does {name} do?", parts)
    if back["risks"]:
        answer(f"What could go wrong for {name}?",
               [(risk, "risks", i) for i, risk in enumerate(back["risks"])])
    if front["ceo"]:
        parts = [(f"{front['ceo']} runs the company.", "ceo", 0)]
        if front["employees"]:
            parts.append((f"It has about {front['employees']} employees.", "employees", 0))
        answer(f"Who runs {name}?", parts)
    if back["legal_matters"]:
        answer(f"Is {name} facing legal issues?",
               [(matter, "legal_matters", i) for i, matter in enumerate(back["legal_matters"])])
    return questions


def comparison_question(db) -> dict:
    quotes, parts = [], []
    for item in COMPARISON["answer"]:
        ticker, text = item["quote"]
        filing = report(db, ticker)
        section = locate(text, filing)
        if not section:
            raise SystemExit(f"Comparison quote not found in the {ticker} report: {text}")
        quotes.append({"n": len(quotes) + 1, "text": text, "section": section, "url": filing["url"],
                       "report": f"{ticker} {filing['form']}, period ending {filing['period']}"})
        parts.append({"text": item["text"], "refs": [len(quotes)]})
    return {"question": COMPARISON["question"], "answer": parts, "quotes": quotes}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    cards = json.loads((PIPELINE / "profile_cards.json").read_text(encoding="utf-8"))["cards"]
    db = duckdb.connect(str(DB_PATH), read_only=True)
    comparison = comparison_question(db)

    out = []
    for card in cards:
        card["name"] = DISPLAY_NAMES.get(card["ticker"], card["name"])
        report_label = f"{card['report']['form']}, period ending {card['report']['period']}"
        questions = build_questions(card, report_label)
        if card["ticker"] in COMPARISON["tickers"]:
            questions.insert(0, comparison)
        out.append({
            "id": card["ticker"],
            "ticker": card["ticker"],
            "name": card["name"],
            "sector": FRIENDLY_SECTORS.get(card["ticker"], card["front"]["sector"] or "Other"),
            "industry": card["front"]["sector"],
            "front": card["front"],
            "back": card["back"],
            "reveal": card["reveal"],
            "sources": card["sources"],
            "report": card["report"],
            "report_label": report_label,
            "questions": questions,
        })

    data = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "cards": out}
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    template = (HERE / "app.template.html").read_text(encoding="utf-8")
    # Brand images (cut from the pitch deck) are inlined: the published page cannot load other files.
    for placeholder, name, mime in (("__LOGO__", "logo.png", "image/png"), ("__HERO__", "hero.jpg", "image/jpeg")):
        encoded = base64.b64encode((HERE / "assets" / name).read_bytes()).decode("ascii")
        template = template.replace(placeholder, f"data:{mime};base64,{encoded}")
    (HERE / "index.html").write_text(template.replace("__DATA__", payload), encoding="utf-8")
    print(f"Wrote index.html: {len(out)} companies, {sum(len(c['questions']) for c in out)} ready-made questions, "
          f"{len(payload) // 1024} KB of data")
    return 0


if __name__ == "__main__":
    sys.exit(main())
