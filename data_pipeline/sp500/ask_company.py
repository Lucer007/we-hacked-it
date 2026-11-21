"""Ask a question about a company and get an answer quoted from its latest annual report.

Works for any company that files with the SEC: the S&P 500, other US-listed companies, and
foreign companies listed in the US (they file a 20-F, e.g. TSM, ASML, NVO, SAP, TM).

What it does:
  1. Finds the company's latest annual report (10-K, 20-F or 40-F) through the SEC's free API.
  2. Downloads it and converts it to plain text (cached in cache/filings/).
  3. Sends the whole report to Claude with citations turned on, so every statement in the
     answer comes with the exact passage of the report it relies on.
  4. Prints the answer and saves it to answers/<TICKER>.json for the prototype.

Needs an Anthropic API key in ANTHROPIC_API_KEY for step 3.

Usage:
  python ask_company.py NVDA "Who are NVIDIA's biggest customers?"
  python ask_company.py AAPL --text-only     # only download the report (no API key needed)
"""

import argparse
import csv
import io
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from bs4 import BeautifulSoup

import sec

HERE = Path(__file__).resolve().parent
MODEL = "claude-opus-5-5"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
DOCUMENT_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn_plain}/{document}"
ANNUAL_FORMS = ("10-K", "20-F", "40-F")

SYSTEM_PROMPT = """You answer questions about a company for people who are curious about it, \
most of them new to investing. Use only the company's annual report, given as a document.

Answer in two to four short sentences of plain English, then stop. Support every factual \
statement with a citation from the report. Explain any financial term you use in a few words. \
If the report does not answer the question, say so plainly instead of guessing.

Do not give investment advice: do not say whether to buy, sell or hold, whether the company is \
a good investment, or what its share price will do."""


def find_company(ticker: str) -> dict:
    wanted = ticker.upper()
    constituents = sec.CACHE_DIR / "sp500_constituents.csv"  # written by build_sp500_cards.py
    if constituents.exists():
        for row in csv.DictReader(io.StringIO(constituents.read_text(encoding="utf-8"))):
            if row["Symbol"].upper() == wanted:
                return {"cik": int(row["CIK"]), "name": row["Security"], "ticker": row["Symbol"]}
    tickers = sec.fetch_json(TICKERS_URL, "company_tickers.json")  # every SEC filer, foreign ones too
    for item in tickers.values():
        if item["ticker"].upper() == wanted.replace(".", "-"):
            return {"cik": int(item["cik_str"]), "name": item["title"], "ticker": item["ticker"]}
    raise SystemExit(f"Ticker {ticker} not found in the SEC's list of companies.")


def latest_annual_report(company: dict) -> dict:
    cik = company["cik"]
    submissions = sec.fetch_json(SUBMISSIONS_URL.format(cik=cik), f"submissions/{cik}.json")
    recent = submissions["filings"]["recent"]
    for form, accn, document, filed, period in zip(recent["form"], recent["accessionNumber"],
                                                   recent["primaryDocument"], recent["filingDate"],
                                                   recent["reportDate"]):
        if form in ANNUAL_FORMS:
            return {
                "form": form, "accession": accn, "filed": filed, "period": period,
                "url": DOCUMENT_URL.format(cik=cik, accn_plain=accn.replace("-", ""), document=document),
            }
    raise SystemExit(f"No annual report found for {company['name']}.")


def report_text(company: dict, report: dict) -> str:
    """The annual report as plain text, without the hidden XBRL data block."""
    text_path = sec.CACHE_DIR / "filings" / f"{company['ticker']}_{report['accession']}.txt"
    if text_path.exists():
        return text_path.read_text(encoding="utf-8")
    html = sec.fetch(report["url"], f"filings/{company['ticker']}_{report['accession']}.htm")
    soup = BeautifulSoup(html, "lxml")
    for hidden in soup.find_all(style=re.compile(r"display:\s*none", re.I)):
        hidden.decompose()
    for tag in soup(["script", "style", "ix:header"]):
        tag.decompose()
    text = soup.get_text("\n").replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    text_path.write_text(text, encoding="utf-8")
    return text


def section_at(text: str, position: int) -> str | None:
    """Name of the report section ("Item 1A. Risk Factors") a passage belongs to."""
    heading = None
    for match in re.finditer(r"^(Item\s+\d+[A-C]?\.?)[ \t]*(?:\n[ \t]*){0,3}([^\n]{3,80})", text, re.I | re.M):
        if match.start() > position:
            break
        heading = f"{match.group(1).rstrip('.')}. {match.group(2).strip()}"
    return heading


def ask(company: dict, report: dict, text: str, question: str) -> dict:
    client = anthropic.Anthropic()
    title = f"{company['name']} annual report ({report['form']}, period ending {report['period']})"
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # if the model declines, the API retries on a fallback model
        output_config={"effort": "medium"},
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {"type": "text", "media_type": "text/plain", "data": text},
                    "title": title,
                    "citations": {"enabled": True},
                    "cache_control": {"type": "ephemeral"},  # follow-up questions reuse the report
                },
                {"type": "text", "text": question},
            ],
        }],
    )
    if response.stop_reason == "refusal":
        raise SystemExit("The model declined to answer this question.")

    parts, quotes = [], []
    for block in response.content:
        if block.type != "text":
            continue
        refs = []
        for citation in block.citations or []:
            cited = (citation.cited_text or "").strip()
            existing = next((q for q in quotes if q["text"] == cited), None)
            if existing is None:
                existing = {"n": len(quotes) + 1, "text": cited,
                            "section": section_at(text, getattr(citation, "start_char_index", 0) or 0)}
                quotes.append(existing)
            refs.append(existing["n"])
        parts.append({"text": block.text, "refs": sorted(set(refs))})

    return {
        "question": question,
        "answer": parts,
        "quotes": quotes,
        "filing": {"form": report["form"], "period": report["period"], "filed": report["filed"], "url": report["url"]},
        "generated_by": response.model,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def save(company: dict, result: dict) -> Path:
    path = HERE / "answers" / f"{company['ticker']}.json"
    path.parent.mkdir(exist_ok=True)
    history = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    history = [item for item in history if item["question"] != result["question"]] + [result]
    path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ticker")
    parser.add_argument("question", nargs="?")
    parser.add_argument("--text-only", action="store_true", help="only download the report as text")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    company = find_company(args.ticker)
    report = latest_annual_report(company)
    text = report_text(company, report)
    print(f"{company['name']}: {report['form']} for the period ending {report['period']} "
          f"({len(text):,} characters)\n{report['url']}")
    if args.text_only or not args.question:
        return 0

    try:
        result = ask(company, report, text, args.question)
    except TypeError as error:
        if "authentication" not in str(error):
            raise
        print("No Anthropic API key found. Set ANTHROPIC_API_KEY and run again.", file=sys.stderr)
        return 1
    except anthropic.AuthenticationError:
        print("The Anthropic API key was rejected.", file=sys.stderr)
        return 1

    print(f"\nQ: {result['question']}\n")
    print(" ".join(p["text"] + "".join(f" [{n}]" for n in p["refs"]) for p in result["answer"]))
    for quote in result["quotes"]:
        print(f"\n[{quote['n']}] {quote['section'] or 'Annual report'}\n    \"{quote['text']}\"")
    print(f"\nSaved to {save(company, result)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
