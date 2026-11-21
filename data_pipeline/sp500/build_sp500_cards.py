"""Build Dealdeck cards for every S&P 500 company from SEC filings.

What it does:
  1. Downloads the public list of S&P 500 companies (ticker, name, GICS sector, SEC CIK).
  2. Downloads the SEC "frames": one file per accounting item and year, holding that item for
     every US-listed company, as reported in their annual reports (10-K) in XBRL format.
  3. For each company, picks its latest fiscal year and turns the numbers into numbered facts,
     each with a link to the exact SEC filing it comes from.
  4. Merges AI summaries from ai_summaries.json, if present (see ../summarize.py).
  5. Writes cards.json.

Rule: numbers never come from the AI. They are copied from SEC filings here, in plain code.

Usage:
  python build_sp500_cards.py            # reuses downloads in cache/
  python build_sp500_cards.py --refresh  # downloads everything again
"""

import argparse
import csv
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import sec

HERE = Path(__file__).resolve().parent
CONSTITUENTS_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
FRAME_URL = "https://data.sec.gov/api/xbrl/frames/us-gaap/{concept}/USD/{period}.json"
FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn_plain}/{accn}-index.htm"

# Accounting items, each with the XBRL tags companies use for it, in order of preference.
DURATION_ITEMS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "RevenuesNetOfInterestExpense",
                "SalesRevenueNet"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "rnd": ["ResearchAndDevelopmentExpense", "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"],
    "dividends": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
}
INSTANT_ITEMS = {
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "long_term_debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
}
# Latest first. CY2026 holds the fiscal years that already ended in 2026 (Apple, Microsoft...);
# the last year is only used for revenue growth.
YEARS = [2026, 2025, 2024, 2023]
QUARTER_ENDS = ["CY2026Q3I", "CY2026Q2I", "CY2026Q1I", "CY2025Q4I", "CY2025Q3I", "CY2025Q2I", "CY2025Q1I",
                "CY2024Q4I", "CY2024Q3I", "CY2024Q2I", "CY2024Q1I"]

# Default filters for the demo. In the product, the user picks these (or describes them in
# a sentence and the AI turns that into filters). They are filters, never recommendations.
DEFAULT_FILTERS_LABEL = "Profitable · Growing · Pays a dividend · Low debt"


def load_frames(refresh: bool) -> dict:
    """frames[(concept, period)] = {cik: entry}"""
    frames = {}
    jobs = []
    for concepts in DURATION_ITEMS.values():
        for concept in concepts:
            years = YEARS if concepts is DURATION_ITEMS["revenue"] else YEARS[:3]
            jobs += [(concept, f"CY{year}") for year in years]
    for concepts in INSTANT_ITEMS.values():
        jobs += [(concept, period) for concept in concepts for period in QUARTER_ENDS]
    for i, (concept, period) in enumerate(jobs, 1):
        print(f"\rSEC frames {i}/{len(jobs)}", end="", flush=True)
        try:
            data = sec.fetch_json(FRAME_URL.format(concept=concept, period=period),
                                  f"frames/{concept}_{period}.json", refresh)
        except FileNotFoundError:
            data = {"data": []}
        frames[(concept, period)] = {entry["cik"]: entry for entry in data["data"]}
    print()
    return frames


def usd(value: float) -> str:
    sign = "−" if value < 0 else ""
    value = abs(value)
    for size, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "k")):
        if value >= size:
            return f"{sign}${value / size:.1f}{suffix}"
    return f"{sign}${value:.0f}"


def filing_url(cik: int, accn: str) -> str:
    return FILING_URL.format(cik=cik, accn_plain=accn.replace("-", ""), accn=accn)


def fiscal_label(entry: dict) -> str:
    end = datetime.strptime(entry["end"], "%Y-%m-%d")
    return f"FY ending {end:%b %Y}"


def latest_duration(frames, cik, concepts, year):
    for concept in concepts:
        entry = frames.get((concept, f"CY{year}"), {}).get(cik)
        if entry:
            return concept, entry
    return None, None


def instant_at(frames, cik, concepts, end_date):
    """Balance-sheet value at the fiscal year end, or at most 100 days before it."""
    end = datetime.strptime(end_date, "%Y-%m-%d")
    best = None
    for concept in concepts:
        for period in QUARTER_ENDS:
            entry = frames.get((concept, period), {}).get(cik)
            if not entry or entry["end"] > end_date:
                continue
            if (end - datetime.strptime(entry["end"], "%Y-%m-%d")).days > 100:
                continue
            if best is None or entry["end"] > best["end"]:
                best = entry
        if best and best["end"] == end_date:
            break
    return best


def build_card(company: dict, frames: dict) -> dict | None:
    cik = int(company["CIK"])

    # Latest fiscal year with a revenue figure, and the year before for growth. Companies
    # often tag a part of their revenue too, so keep the largest tagged figure.
    revenue = revenue_prev = None
    year = None
    for candidate in YEARS[:3]:  # 2024 is the fallback for late filers
        options = [(concept, frames.get((concept, f"CY{candidate}"), {}).get(cik))
                   for concept in DURATION_ITEMS["revenue"]]
        options = [(concept, entry) for concept, entry in options if entry]
        if options:
            concept, revenue = max(options, key=lambda option: option[1]["val"])
            year = candidate
            revenue_prev = frames.get((concept, f"CY{candidate - 1}"), {}).get(cik)
            break
    if not revenue:
        return None

    _, net_income = latest_duration(frames, cik, DURATION_ITEMS["net_income"], year)
    # Banks and real-estate trusts don't tag total revenue the way other companies do: what
    # is tagged is only one part of it. A profit larger than revenue gives that away.
    revenue_comparable = not (net_income and net_income["val"] > revenue["val"]) and revenue["val"] > 0
    _, rnd = latest_duration(frames, cik, DURATION_ITEMS["rnd"], year)
    _, dividends = latest_duration(frames, cik, DURATION_ITEMS["dividends"], year)
    cash = instant_at(frames, cik, INSTANT_ITEMS["cash"], revenue["end"])
    debt = instant_at(frames, cik, INSTANT_ITEMS["long_term_debt"], revenue["end"])
    equity = instant_at(frames, cik, INSTANT_ITEMS["equity"], revenue["end"])

    fy = fiscal_label(revenue)
    facts = []

    def add(fact, source, entry=None):
        facts.append({"n": len(facts) + 1, "fact": fact, "source": source,
                      "source_url": filing_url(cik, entry["accn"]) if entry else None, "official": True})

    add(f"Sector: {company['GICS Sector']}, {company['GICS Sub-Industry']}",
        "S&P Dow Jones Indices GICS classification, via the public S&P 500 constituents list")
    add(f"In the S&P 500 since {company['Date added']}", "Public S&P 500 constituents list")
    growth = None
    if revenue_comparable:
        add(f"Revenue ({fy}): {usd(revenue['val'])}", "Annual report (10-K), SEC XBRL data", revenue)
        if revenue_prev and revenue_prev["val"]:
            growth = (revenue["val"] - revenue_prev["val"]) / abs(revenue_prev["val"]) * 100
            add(f"Revenue growth over one year: {growth:+.1f}% (from {usd(revenue_prev['val'])})",
                "Computed from two annual reports (10-K), SEC XBRL data", revenue)
    else:
        add(f"Revenue ({fy}): not reported in a comparable form (common for banks and real-estate trusts)",
            "Annual report (10-K), SEC XBRL data", revenue)
    margin = None
    if net_income:
        margin = net_income["val"] / revenue["val"] * 100 if revenue_comparable else None
        margin_text = f", a net margin of {margin:.1f}%" if margin is not None else ""
        add(f"Net income ({fy}): {usd(net_income['val'])}{margin_text}", "Annual report (10-K), SEC XBRL data", net_income)
    if rnd:
        share_text = f", {rnd['val'] / revenue['val'] * 100:.1f}% of revenue" if revenue_comparable else ""
        add(f"Research and development spending ({fy}): {usd(rnd['val'])}{share_text}",
            "Annual report (10-K), SEC XBRL data", rnd)
    if dividends and dividends["val"] > 0:
        add(f"Dividends paid to shareholders ({fy}): {usd(dividends['val'])}", "Annual report (10-K), SEC XBRL data", dividends)
    else:
        add(f"Dividends paid ({fy}): none reported", "Annual report (10-K), SEC XBRL data")
    if cash:
        add(f"Cash and cash equivalents at {cash['end']}: {usd(cash['val'])}", "Annual report (10-K), SEC XBRL data", cash)
    debt_ratio = None
    if debt:
        ratio_text = ""
        if equity and equity["val"] > 0:
            debt_ratio = debt["val"] / equity["val"]
            ratio_text = f", {debt_ratio:.2f} times shareholders' equity"
        add(f"Long-term debt at {debt['end']}: {usd(debt['val'])}{ratio_text}", "Annual report (10-K), SEC XBRL data", debt)

    pays_dividend = bool(dividends and dividends["val"] > 0)
    stats = [
        {"label": f"Revenue {fy.replace('FY ending ', '')}",
         "value": usd(revenue["val"]) if revenue_comparable else "n/a", "official": True},
        {"label": "Revenue growth", "value": f"{growth:+.1f}%" if growth is not None else "n/a", "official": True},
        {"label": "Net margin", "value": f"{margin:.1f}%" if margin is not None else "n/a", "official": True},
        {"label": "Dividends paid", "value": usd(dividends["val"]) if pays_dividend else "None", "official": True},
    ]
    low_debt = debt is None or (debt_ratio is not None and debt_ratio < 1)
    fit = [
        {"label": "Profitable" if net_income and net_income["val"] > 0 else "Not profitable",
         "ok": bool(net_income and net_income["val"] > 0)},
        {"label": "Growing" if growth is not None and growth > 0 else "Not growing",
         "ok": growth is not None and growth > 0},
        {"label": "Pays a dividend" if pays_dividend else "No dividend", "ok": pays_dividend},
        {"label": "Low debt" if low_debt else "High debt", "ok": low_debt},
    ]

    return {
        "id": company["Symbol"],
        "ticker": company["Symbol"],
        "cik": cik,
        "name": company["Security"],
        "sector": company["GICS Sub-Industry"],
        "gics_sector": company["GICS Sector"],
        "city": company["Headquarters Location"],
        "founded": company.get("Founded"),
        "fiscal_year": fy,
        "edgar_url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=10-K",
        "stats": stats,
        "fit": fit,
        "fit_score": f"{sum(f['ok'] for f in fit)}/{len(fit)}",
        "facts": facts,
        "summary": None,
    }


def merge_summaries(cards: list[dict]) -> int:
    path = HERE / "ai_summaries.json"
    if not path.exists():
        return 0
    summaries = json.loads(path.read_text(encoding="utf-8"))
    merged = 0
    for card in cards:
        item = summaries.get(card["id"])
        if not item:
            continue
        if item.get("fiscal_year") and item["fiscal_year"] != card["fiscal_year"]:
            print(f"Stale AI summary for {card['id']} ({item['fiscal_year']}, card is {card['fiscal_year']}): skipped")
            continue
        valid = {f["n"] for f in card["facts"]}
        sentences = [s for s in item.get("sentences", []) if s.get("refs") and set(s["refs"]) <= valid]
        if sentences:
            card["summary"] = sentences
            card["summary_generated_by"] = item.get("generated_by")
            merged += 1
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--refresh", action="store_true", help="download everything again")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    constituents = sec.fetch(CONSTITUENTS_URL, "sp500_constituents.csv", args.refresh).decode("utf-8")
    companies = list(csv.DictReader(io.StringIO(constituents)))
    frames = load_frames(args.refresh)

    cards, missing = [], []
    for company in companies:
        card = build_card(company, frames)
        (cards.append(card) if card else missing.append(company["Symbol"]))

    merged = merge_summaries(cards)
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "SEC EDGAR XBRL frames (10-K annual reports); S&P 500 constituents list",
        "filters": DEFAULT_FILTERS_LABEL,
        "cards": cards,
    }
    (HERE / "cards.json").write_text(json.dumps(output, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Wrote cards.json: {len(cards)} of {len(companies)} companies, {merged} with an AI summary.")
    if missing:
        print(f"No annual revenue found in SEC frames for {len(missing)}: {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
