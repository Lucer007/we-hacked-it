"""Build the swipe cards of Lijith's mockup (front and back) from each company's annual report.

Two steps:
  1. extract   Claude reads the relevant sections of the annual report (business, risk factors,
               legal proceedings, executive officers) and fills a structured form. Every field
               comes with a quote copied from the report. Output: profile_extractions.json.
  2. assemble  Plain code checks every quote against the report text. A field whose quote is
               not found word for word is dropped. The verified fields are combined with the
               SEC numbers from cards.json into profile_cards.json, the file the app reads.

Step 2 never calls the API, so it can be re-run at will.

Usage:
  python build_profile_cards.py                    # extract missing companies, then assemble
  python build_profile_cards.py --assemble-only    # no API call
  python build_profile_cards.py --tickers NVDA AMD --refresh
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import duckdb

import sec

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "dealdeck.duckdb"
EXTRACTIONS_PATH = HERE / "profile_extractions.json"
OUTPUT_PATH = HERE / "profile_cards.json"
MODEL = "claude-opus-5-5"

# Report sections sent to the model, per form, with a character budget each.
SECTIONS = {
    "10-K": [("business", r"^item\s*1\.\s", 60000), ("risks", r"^item\s*1a", 40000),
             ("legal", r"^item\s*3\.", 8000)],
    "20-F": [("business", r"^item\s*4\.", 60000), ("risks", r"^item\s*3\.", 40000),
             ("people", r"^item\s*6\.", 15000), ("legal", r"^item\s*8\.", 8000)],
}
OFFICERS_RE = re.compile(r"executive officers|chief executive officer", re.I)

SYSTEM_PROMPT = """You fill in the profile card of a listed company for a swipe app. Most users \
are new to investing. Use only the annual report excerpts you are given, never outside knowledge.

Every field has a value and a quote. The quote must be copied word for word from the excerpts \
(at most 300 characters) and must support the value: software checks that it appears in the \
report and drops the field otherwise. If the excerpts do not contain the information, leave both \
value and quote empty. Write values in plain English.

Fields:
- tagline: what the company is, in 8 words or fewer (e.g. "Chips and software for AI computing").
- what_they_do: one plain sentence on how the company makes money.
- mission: the company's own mission or purpose statement, copied as written; empty if none.
- ceo: full name of the chief executive officer.
- employees: number of employees as written in the report (e.g. "42,000").
- founded_year: year the company was founded or incorporated.
- founder_led: "yes" if the current CEO founded or co-founded the company, "no" if the report \
shows otherwise, "unknown" if it does not say.
- segments: the business segments the company reports, up to 5.
- risks: the 3 risks that matter most for this company specifically, each in 15 words or fewer.
- legal_matters: up to 2 significant legal proceedings or investigations, each in 20 words or \
fewer. Empty list if none.

No investment advice: never say whether the company is a good investment."""

FIELD = {
    "type": "object",
    "properties": {"value": {"type": "string"}, "quote": {"type": "string"}},
    "required": ["value", "quote"],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {
        "tagline": FIELD, "what_they_do": FIELD, "mission": FIELD, "ceo": FIELD,
        "employees": FIELD, "founded_year": FIELD,
        "founder_led": {
            "type": "object",
            "properties": {"value": {"type": "string", "enum": ["yes", "no", "unknown"]}, "quote": {"type": "string"}},
            "required": ["value", "quote"],
            "additionalProperties": False,
        },
        "segments": {"type": "array", "items": FIELD},
        "risks": {"type": "array", "items": FIELD},
        "legal_matters": {"type": "array", "items": FIELD},
    },
    "required": ["tagline", "what_they_do", "mission", "ceo", "employees", "founded_year",
                 "founder_led", "segments", "risks", "legal_matters"],
    "additionalProperties": False,
}
SINGLE_FIELDS = ["tagline", "what_they_do", "mission", "ceo", "employees", "founded_year", "founder_led"]
LIST_FIELDS = ["segments", "risks", "legal_matters"]


# ---------------------------------------------------------------- report access

def report(db, ticker: str) -> dict:
    row = db.execute("SELECT accession, form, period_end, url FROM filings WHERE ticker = ?", [ticker]).fetchone()
    if not row:
        raise LookupError(f"No annual report downloaded for {ticker}. Run: python ask_company.py {ticker} --text-only")
    accession, form, period, url = row
    chunks = db.execute("SELECT section, text FROM filing_chunks WHERE ticker = ? ORDER BY "
                        "CAST(split_part(chunk_id, '-', 2) AS INTEGER)", [ticker]).fetchall()
    return {"accession": accession, "form": form, "period": str(period), "url": url, "chunks": chunks}


def excerpts(filing: dict) -> str:
    """The sections the model needs, each cut to its budget, plus passages about executives."""
    parts = []
    for label, pattern, budget in SECTIONS.get(filing["form"], SECTIONS["10-K"]):
        text = " ".join(t for s, t in filing["chunks"] if re.match(pattern, s, re.I))[:budget]
        if text:
            parts.append(f"=== {label.upper()} ===\n{text}")
    officers = " ".join(t for s, t in filing["chunks"] if OFFICERS_RE.search(t))[:12000]
    parts.append(f"=== EXECUTIVE OFFICERS ===\n{officers}")
    return "\n\n".join(parts)


def normalize(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = text.replace("–", "-").replace("—", "-").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip().lower()


def locate(quote: str, filing: dict) -> str | None:
    """Section that contains the quote word for word, or None if the report doesn't contain it."""
    needle = normalize(quote).rstrip(".")
    if len(needle) < 12:
        return None
    for section, text in filing["chunks"]:
        if needle in normalize(text):
            return section
    # A quote may straddle two passages: check the joined text of each section.
    sections = {}
    for section, text in filing["chunks"]:
        sections.setdefault(section, []).append(text)
    for section, texts in sections.items():
        if needle in normalize(" ".join(texts)):
            return section
    return None


# ---------------------------------------------------------------- step 1: extract

def extract(client: anthropic.Anthropic, name: str, filing: dict) -> dict:
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # if the model declines, the API retries on a fallback model
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content":
                   f"Company: {name}\nAnnual report: {filing['form']}, period ending {filing['period']}\n\n"
                   f"{excerpts(filing)}"}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("the model declined")
    text = next(b.text for b in response.content if b.type == "text")
    return {"accession": filing["accession"], "extracted_by": response.model,
            "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "fields": json.loads(text)}


# ---------------------------------------------------------------- step 2: assemble

def assemble(ticker: str, extraction: dict, filing: dict, sec_card: dict | None, company: dict) -> dict:
    fields, sources, dropped = extraction["fields"], {}, []

    def checked(name: str, item: dict):
        value, quote = (item.get("value") or "").strip(), (item.get("quote") or "").strip()
        if not value or value == "unknown":
            return None
        section = locate(quote, filing)
        if not section:
            dropped.append(f"{name}: {value}")
            return None
        sources.setdefault(name, []).append({"quote": quote, "section": section, "url": filing["url"]})
        return value

    single = {name: checked(name, fields.get(name) or {}) for name in SINGLE_FIELDS}
    lists = {name: [v for v in (checked(name, item) for item in fields.get(name) or []) if v] for name in LIST_FIELDS}
    stats = {s["label"].split(" ")[0]: s["value"] for s in (sec_card or {}).get("stats", [])}
    # No SEC card (e.g. foreign companies reporting under IFRS): dividends are unknown, not "No".
    dividend = None if not sec_card else ("Yes" if stats.get("Dividends") not in (None, "None") else "No")

    return {
        "ticker": ticker,
        "name": company["name"],
        "front": {
            "name": company["name"],
            "tagline": single["tagline"],
            "sector": company["gics_sub_industry"],
            "ceo": single["ceo"],
            "employees": single["employees"],
            "headquarters": company["headquarters"],
            "mission": single["mission"],
            "what_they_do": single["what_they_do"],
            "photo": None,
        },
        "back": {
            "profile": {
                "founded": single["founded_year"] or company["founded"],
                "founded_source": "annual report" if single["founded_year"] else "S&P 500 constituents list",
                "founder_led": {"yes": "Yes", "no": "No"}.get(single["founder_led"] or "", "Not stated"),
                "size": "Large company (S&P 500)",
            },
            "segments": lists["segments"],
            "key_numbers": (sec_card or {}).get("stats", []),
            "key_numbers_period": (sec_card or {}).get("fiscal_year"),
            "risks": lists["risks"],
            "legal_matters": lists["legal_matters"],
        },
        "reveal": {
            "ticker": ticker,
            "exchange": company.get("exchange"),
            "dividend": dividend,
            "market_cap": None,  # needs a price feed
        },
        "sources": sources,
        "dropped_unverified": dropped,
        "report": {"form": filing["form"], "period": filing["period"], "url": filing["url"]},
        "extracted_by": extraction.get("extracted_by"),
    }


def company_info(db, ticker: str, cik_hint: int | None = None) -> dict:
    row = db.execute("SELECT name, gics_sub_industry, headquarters, founded, cik FROM companies WHERE ticker = ?",
                     [ticker]).fetchone()
    info = {"name": ticker, "gics_sub_industry": None, "headquarters": None, "founded": None, "exchange": None}
    if row:
        info.update(name=row[0], gics_sub_industry=row[1], headquarters=row[2], founded=row[3])
        cik_hint = row[4]
    # Exchange and, for foreign companies, the name come from the SEC submissions file.
    for path in ([sec.CACHE_DIR / "submissions" / f"{cik_hint}.json"] if cik_hint else []) + \
                sorted((sec.CACHE_DIR / "submissions").glob("*.json")):
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if ticker in (data.get("tickers") or []):
            info["exchange"] = (data.get("exchanges") or [None])[0]
            if not row:
                info["name"] = data.get("name", ticker).title()
                info["gics_sub_industry"] = data.get("sicDescription")
                address = (data.get("addresses") or {}).get("business") or {}
                info["headquarters"] = ", ".join(filter(None, [(address.get("city") or "").title(),
                                                               address.get("stateOrCountryDescription")]))
            break
    return info


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tickers", nargs="*", help="default: every company with a downloaded report")
    parser.add_argument("--refresh", action="store_true", help="extract again even if already done")
    parser.add_argument("--assemble-only", action="store_true", help="skip the API, only check and assemble")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    db = duckdb.connect(str(DB_PATH), read_only=True)
    tickers = args.tickers or [r[0] for r in db.execute("SELECT ticker FROM filings ORDER BY ticker").fetchall()]
    extractions = json.loads(EXTRACTIONS_PATH.read_text(encoding="utf-8")) if EXTRACTIONS_PATH.exists() else {}
    sec_cards = {c["ticker"]: c for c in json.loads((HERE / "cards.json").read_text(encoding="utf-8"))["cards"]}

    if not args.assemble_only:
        client = anthropic.Anthropic()
        for ticker in tickers:
            filing = report(db, ticker)
            if not args.refresh and extractions.get(ticker, {}).get("accession") == filing["accession"]:
                continue
            print(f"Extracting {ticker} from its {filing['form']}...")
            try:
                extractions[ticker] = extract(client, company_info(db, ticker)["name"], filing)
            except TypeError as error:
                if "authentication" not in str(error):
                    raise
                print("No Anthropic API key found: assembling the existing extractions only.", file=sys.stderr)
                break
            except (anthropic.APIError, RuntimeError) as error:
                print(f"  {ticker} failed: {error}", file=sys.stderr)
                continue
            EXTRACTIONS_PATH.write_text(json.dumps(extractions, ensure_ascii=False, indent=2), encoding="utf-8")

    cards = []
    for ticker in tickers:
        if ticker not in extractions:
            print(f"{ticker:<6} no extraction yet")
            continue
        filing = report(db, ticker)
        if extractions[ticker]["accession"] != filing["accession"]:
            print(f"{ticker:<6} extraction is for an older report: run again with --refresh")
            continue
        card = assemble(ticker, extractions[ticker], filing, sec_cards.get(ticker), company_info(db, ticker))
        cards.append(card)
        kept = sum(len(v) for v in card["sources"].values())
        print(f"{ticker:<6} {kept} verified quotes, {len(card['dropped_unverified'])} dropped"
              + (f": {card['dropped_unverified']}" if card["dropped_unverified"] else ""))

    OUTPUT_PATH.write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                       "cards": cards}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH.name}: {len(cards)} cards")
    return 0


if __name__ == "__main__":
    sys.exit(main())
