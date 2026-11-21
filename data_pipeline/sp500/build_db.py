"""Build dealdeck.duckdb, the database the analyst agent queries.

Tables:
  companies       one row per S&P 500 company (ticker, name, GICS sector, HQ...)
  annual          one row per company and fiscal year, 5 years of income-statement items
  balance_sheet   one row per company and balance-sheet date (cash, debt, equity, assets)
  filings         the annual reports downloaded by ask_company.py --text-only
  filing_chunks   those reports cut into passages by section, for search_filing

All numbers come from the SEC's XBRL frames (as filed in 10-K annual reports).

Usage:
  python build_db.py            # reuses downloads in cache/
  python build_db.py --refresh  # downloads the SEC frames again
"""

import argparse
import csv
import io
import json
import re
import sys
from pathlib import Path

import duckdb

import sec

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "dealdeck.duckdb"
CONSTITUENTS_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
FRAME_URL = "https://data.sec.gov/api/xbrl/frames/us-gaap/{concept}/USD/{period}.json"
FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn_plain}/{accn}-index.htm"

# CY2026 holds the fiscal years that already ended in 2026 (e.g. Apple in September, Microsoft in June).
YEARS = [2021, 2022, 2023, 2024, 2025, 2026]
QUARTER_ENDS = [f"CY{y}Q{q}I" for y in (2024, 2025, 2026) for q in (1, 2, 3, 4)][:11]

# Income-statement items (one value per fiscal year), with the XBRL tags used for each.
DURATION_ITEMS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "RevenuesNetOfInterestExpense"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "rnd": ["ResearchAndDevelopmentExpense", "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"],
    "dividends_paid": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
}
# Balance-sheet items (one value per date).
INSTANT_ITEMS = {
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "long_term_debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "total_assets": ["Assets"],
}

CHUNK_CHARS = 1500
# "Item 1." and its title can be split over a few lines ("ITEM 1.\n\nBUSINESS").
SECTION_RE = re.compile(r"^(Item\s+\d+[A-C]?\.?)[ \t]*(?:\n[ \t]*){0,3}([^\n]{3,80})", re.I | re.M)


def frame(concept: str, period: str, refresh: bool) -> list[dict]:
    try:
        return sec.fetch_json(FRAME_URL.format(concept=concept, period=period),
                              f"frames/{concept}_{period}.json", refresh)["data"]
    except FileNotFoundError:
        return []


def filing_url(cik: int, accn: str) -> str:
    return FILING_URL.format(cik=cik, accn_plain=accn.replace("-", ""), accn=accn)


def build_annual(ciks: dict, refresh: bool) -> list[tuple]:
    """rows of (ticker, fiscal_year, fiscal_year_end, item, value, accession, source_url)"""
    best = {}  # (cik, fiscal_year, item) -> entry; the largest value across an item's tags
    jobs = [(item, concept, year) for item, concepts in DURATION_ITEMS.items()
            for concept in concepts for year in YEARS]
    for i, (item, concept, year) in enumerate(jobs, 1):
        print(f"\rIncome-statement frames {i}/{len(jobs)}", end="", flush=True)
        for entry in frame(concept, f"CY{year}", refresh):
            if entry["cik"] not in ciks:
                continue
            key = (entry["cik"], int(entry["end"][:4]), item)
            if key not in best or abs(entry["val"]) > abs(best[key]["val"]):
                best[key] = entry
    print()
    return [(ciks[cik], fy, entry["end"], item, entry["val"], entry["accn"], filing_url(cik, entry["accn"]))
            for (cik, fy, item), entry in best.items()]


def build_balance_sheet(ciks: dict, refresh: bool) -> list[tuple]:
    best = {}  # (cik, date, item) -> entry
    jobs = [(item, concept, period) for item, concepts in INSTANT_ITEMS.items()
            for concept in concepts for period in QUARTER_ENDS]
    for i, (item, concept, period) in enumerate(jobs, 1):
        print(f"\rBalance-sheet frames {i}/{len(jobs)}", end="", flush=True)
        for entry in frame(concept, period, refresh):
            if entry["cik"] not in ciks:
                continue
            key = (entry["cik"], entry["end"], item)
            if key not in best or abs(entry["val"]) > abs(best[key]["val"]):
                best[key] = entry
    print()
    return [(ciks[cik], date, item, entry["val"], entry["accn"], filing_url(cik, entry["accn"]))
            for (cik, date, item), entry in best.items()]


def chunk_report(text: str) -> list[tuple[str, str]]:
    """Cut a report into (section, passage) pairs of about CHUNK_CHARS characters."""
    text = text.replace(" ", " ").replace(" ", " ")  # thin spaces in some headings
    headings = [(m.start(), f"{m.group(1).rstrip('.')}. {m.group(2).strip()}") for m in SECTION_RE.finditer(text)]
    bounds = [(0, "Cover and introduction")] + headings + [(len(text), None)]
    chunks = []
    for (start, section), (end, _) in zip(bounds, bounds[1:]):
        body = text[start:end]
        if len(body) < 300:  # table-of-contents entries
            continue
        # The extracted text rarely has blank lines between paragraphs, so build passages from
        # lines, and cut lines that are too long at sentence ends.
        pieces = []
        for line in body.splitlines():
            line = line.strip()
            if len(line) <= CHUNK_CHARS:
                pieces.append(line)
            else:
                pieces += re.split(r"(?<=[.!?;])\s+", line)
        current = ""
        for piece in filter(None, pieces):
            if current and len(current) + len(piece) > CHUNK_CHARS:
                chunks.append((section, current))
                current = ""
            current = f"{current} {piece}".strip()
        if len(current) > 80:
            chunks.append((section, current))
    return chunks


def build_filings(companies: dict) -> tuple[list[tuple], list[tuple]]:
    filings, chunks = [], []
    by_ticker = {row["Symbol"]: row for row in companies.values()}
    for path in sorted((sec.CACHE_DIR / "filings").glob("*.txt")):
        ticker, accession = path.stem.rsplit("_", 1)
        submissions_path = None
        meta = {"form": "annual report", "period": None, "filed": None}
        company = by_ticker.get(ticker)
        if company:
            submissions_path = sec.CACHE_DIR / "submissions" / f"{int(company['CIK'])}.json"
        else:  # foreign filers are not in the S&P 500 list
            for candidate in (sec.CACHE_DIR / "submissions").glob("*.json"):
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if accession in data["filings"]["recent"]["accessionNumber"]:
                    submissions_path = candidate
                    break
        if submissions_path and submissions_path.exists():
            recent = json.loads(submissions_path.read_text(encoding="utf-8"))["filings"]["recent"]
            i = recent["accessionNumber"].index(accession)
            meta = {"form": recent["form"][i], "period": recent["reportDate"][i], "filed": recent["filingDate"][i]}
            cik = int(submissions_path.stem)
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{recent['primaryDocument'][i]}"
        else:
            url = None
        filings.append((ticker, accession, meta["form"], meta["period"], meta["filed"], url))
        for n, (section, passage) in enumerate(chunk_report(path.read_text(encoding="utf-8"))):
            chunks.append((f"{ticker}-{n}", ticker, accession, section, passage))
    return filings, chunks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--refresh", action="store_true", help="download the SEC frames again")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    rows = list(csv.DictReader(io.StringIO(
        sec.fetch(CONSTITUENTS_URL, "sp500_constituents.csv", args.refresh).decode("utf-8"))))
    companies = {int(row["CIK"]): row for row in rows}
    ciks = {cik: row["Symbol"] for cik, row in companies.items()}

    annual_rows = build_annual(ciks, args.refresh)
    balance_rows = build_balance_sheet(ciks, args.refresh)
    filings, chunks = build_filings(companies)

    DB_PATH.unlink(missing_ok=True)
    db = duckdb.connect(str(DB_PATH))
    db.execute("""CREATE TABLE companies (ticker VARCHAR PRIMARY KEY, name VARCHAR, gics_sector VARCHAR,
                  gics_sub_industry VARCHAR, headquarters VARCHAR, in_sp500_since DATE, founded VARCHAR, cik INTEGER)""")
    db.executemany("INSERT INTO companies VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   [(r["Symbol"], r["Security"], r["GICS Sector"], r["GICS Sub-Industry"], r["Headquarters Location"],
                     r["Date added"] or None, r["Founded"], int(r["CIK"])) for r in rows])

    db.execute("""CREATE TABLE annual_long (ticker VARCHAR, fiscal_year INTEGER, fiscal_year_end DATE, item VARCHAR,
                  value DOUBLE, accession VARCHAR, source_url VARCHAR)""")
    db.executemany("INSERT INTO annual_long VALUES (?, ?, ?, ?, ?, ?, ?)", annual_rows)
    item_columns = ", ".join(f"max(value) FILTER (WHERE item = '{item}') AS {item}" for item in DURATION_ITEMS)
    db.execute(f"""CREATE TABLE annual AS
        SELECT ticker, fiscal_year, max(fiscal_year_end) AS fiscal_year_end, {item_columns},
               any_value(source_url) FILTER (WHERE item = 'revenue') AS source_url
        FROM annual_long GROUP BY ticker, fiscal_year""")
    # Banks and real-estate trusts tag only part of their revenue: profit above revenue gives it away.
    db.execute("""ALTER TABLE annual ADD COLUMN revenue_comparable BOOLEAN;
                  UPDATE annual SET revenue_comparable = revenue > 0 AND (net_income IS NULL OR net_income <= revenue)""")

    db.execute("""CREATE TABLE balance_long (ticker VARCHAR, period_end DATE, item VARCHAR, value DOUBLE,
                  accession VARCHAR, source_url VARCHAR)""")
    db.executemany("INSERT INTO balance_long VALUES (?, ?, ?, ?, ?, ?)", balance_rows)
    item_columns = ", ".join(f"max(value) FILTER (WHERE item = '{item}') AS {item}" for item in INSTANT_ITEMS)
    db.execute(f"""CREATE TABLE balance_sheet AS
        SELECT ticker, period_end, {item_columns}, any_value(source_url) AS source_url
        FROM balance_long GROUP BY ticker, period_end""")
    db.execute("DROP TABLE annual_long; DROP TABLE balance_long")

    db.execute("""CREATE TABLE filings (ticker VARCHAR, accession VARCHAR, form VARCHAR, period_end DATE,
                  filed DATE, url VARCHAR)""")
    if filings:
        db.executemany("INSERT INTO filings VALUES (?, ?, ?, ?, ?, ?)", filings)
    db.execute("CREATE TABLE filing_chunks (chunk_id VARCHAR, ticker VARCHAR, accession VARCHAR, section VARCHAR, text VARCHAR)")
    if chunks:
        db.executemany("INSERT INTO filing_chunks VALUES (?, ?, ?, ?, ?)", chunks)

    for table in ("companies", "annual", "balance_sheet", "filings", "filing_chunks"):
        print(f"{table:<14} {db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]:>7} rows")
    db.close()
    print(f"Wrote {DB_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
