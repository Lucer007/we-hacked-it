"""Dealdeck analyst agent: answers questions about companies with cited evidence.

Architecture:
  - Claude (claude-opus-5-5) runs a tool loop with three tools:
      query_financials(sql)          read-only SQL on dealdeck.duckdb (SEC numbers, 5 years)
      search_filing(ticker, query)   BM25 search in annual reports, returned as search_result
                                     blocks with citations on, so Claude cites passages natively
      show_companies(tickers, reason) puts a deck of cards in front of the user
  - After Claude's final answer, two deterministic checks run:
      grounding: every figure in the answer must appear in a tool result (SQL rows or cited
                 passages). Claude is told to compute derived numbers in SQL for that reason.
      no advice: the answer must not tell the user to buy, sell or hold.
    If a check fails, the failure goes back to Claude for one correction round.

Usage:
  python analyst.py "Is Nvidia more dependent on a few customers than AMD?"
  python analyst.py "Show me profitable healthcare companies with growing revenue and low debt"
  python analyst.py --test-tools      # exercise the tools without calling the API
"""

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import duckdb

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "dealdeck.duckdb"
LOG_PATH = HERE / "answers" / "analyst_log.jsonl"
MODEL = "claude-opus-5-5"
MAX_TURNS = 12
MAX_ROWS = 50

SYSTEM_PROMPT = """You are the analyst inside Dealdeck, an app where people discover listed \
companies by swiping cards. Most users are new to investing.

How to work:
- Get every number from query_financials and every statement about what a company does, its \
customers, segments, risks or strategy from search_filing. Do not use outside knowledge about \
these companies: it may be out of date and it cannot be checked.
- Compute every derived number (growth, margins, ratios, shares, rankings) in SQL, so it appears \
in a tool result. Do not do arithmetic yourself.
- Search again with other words if a search misses. If the tools cannot answer, say so.
- When the user describes the kind of companies they want to see, find them with \
query_financials and call show_companies.

How to answer:
- Two to five short sentences of plain English. Explain any financial term in a few words.
- Give the fiscal year of every figure.
- No investment advice: never say whether to buy, sell or hold, whether a company is a good \
investment, or what its share price will do. If asked, say you can't advise and give the \
relevant facts instead."""

TOOLS = [
    {
        "name": "query_financials",
        "description": (
            "Run one read-only SQL query (DuckDB dialect) on Dealdeck's database of S&P 500 companies, "
            "built from SEC annual reports. Tables:\n"
            "- companies(ticker, name, gics_sector, gics_sub_industry, headquarters, in_sp500_since, founded, cik)\n"
            "- annual(ticker, fiscal_year, fiscal_year_end, revenue, gross_profit, operating_income, net_income, "
            "rnd, dividends_paid, source_url, revenue_comparable): one row per company and fiscal year, "
            "2021 to 2026, in US dollars. fiscal_year is the calendar year in which the fiscal year ends. "
            "rnd is research and development spending. When revenue_comparable is false (banks, real-estate "
            "trusts), revenue is only partial: do not use it.\n"
            "- balance_sheet(ticker, period_end, cash, long_term_debt, equity, total_assets, source_url): "
            "US dollars at each date (fiscal year ends and quarter ends, 2024 to 2026).\n"
            "- filings(ticker, accession, form, period_end, filed, url): annual reports that search_filing can search.\n"
            "Tickers use dots, e.g. BRK.B. Compute growth, margins and ratios in the query. "
            f"At most {MAX_ROWS} rows are returned."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "A single SELECT or WITH query."}},
            "required": ["sql"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_filing",
        "description": (
            "Search the latest annual report (10-K, or 20-F for foreign companies) of one company and get "
            "the most relevant passages, which you can cite. Use it for what the numbers don't say: what the "
            "company does, customers, segments, geography, suppliers, competition, risks, strategy. Only the "
            "companies in the filings table have a report. Use specific words that the report would contain."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "query": {"type": "string", "description": "Keywords, e.g. 'customer concentration percent of revenue'"},
            },
            "required": ["ticker", "query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "show_companies",
        "description": (
            "Show the user a deck of company cards to swipe. Use it when the user describes the kind of "
            "companies they want to see. Give up to 12 tickers, best match first, and one sentence on how "
            "they match the user's description."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "tickers": {"type": "array", "items": {"type": "string"}},
                "reason": {"type": "string"},
            },
            "required": ["tickers", "reason"],
            "additionalProperties": False,
        },
    },
]


# ---------------------------------------------------------------- tools

class FilingIndex:
    """BM25 over the passages of each company's annual report."""

    TOKEN_RE = re.compile(r"[a-z0-9]+")
    STOPWORDS = set("the a an and or of to in for on by with as at is are was were be been our we us its it "
                    "this that these those from which such may can will not any all other".split())

    def __init__(self, db):
        self.passages = defaultdict(list)
        for chunk_id, ticker, section, text, form, period, url in db.execute(
                """SELECT c.chunk_id, c.ticker, c.section, c.text, f.form, f.period_end, f.url
                   FROM filing_chunks c JOIN filings f USING (ticker, accession)""").fetchall():
            self.passages[ticker].append({"id": chunk_id, "section": section, "text": text,
                                          "form": form, "period": str(period), "url": url})
        self.stats = {}

    def tokens(self, text: str) -> list[str]:
        """Words plus pairs of consecutive words, so 'direct customer' outranks either word alone."""
        words = self.TOKEN_RE.findall(text.lower().replace("%", " percent "))
        words = [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words]
        unigrams = [w for w in words if w not in self.STOPWORDS]
        bigrams = [f"{a}_{b}" for a, b in zip(words, words[1:]) if a not in self.STOPWORDS and b not in self.STOPWORDS]
        return unigrams + bigrams

    def _stats(self, ticker: str):
        if ticker not in self.stats:
            docs = [Counter(self.tokens(p["text"])) for p in self.passages[ticker]]
            df = Counter(term for doc in docs for term in doc)
            lengths = [sum(doc.values()) for doc in docs]
            self.stats[ticker] = (docs, df, lengths, sum(lengths) / max(len(lengths), 1))
        return self.stats[ticker]

    def search(self, ticker: str, query: str, k: int = 5) -> list[dict]:
        if ticker not in self.passages:
            return []
        docs, df, lengths, avg_length = self._stats(ticker)
        n = len(docs)
        terms = set(self.tokens(query))
        scores = []
        for doc, length in zip(docs, lengths):
            score = 0.0
            for term in terms:
                tf = doc.get(term, 0)
                if tf:
                    idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                    score += idf * tf * 2.5 / (tf + 1.5 * (0.25 + 0.75 * length / avg_length))
            scores.append(score)
        results, seen = [], set()
        for i in sorted(range(n), key=lambda i: scores[i], reverse=True):
            if scores[i] <= 0 or len(results) == k:
                break
            fingerprint = self.passages[ticker][i]["text"][:200]
            if fingerprint not in seen:  # reports repeat some paragraphs in the notes
                seen.add(fingerprint)
                results.append(self.passages[ticker][i])
        return results


class Tools:
    def __init__(self):
        if not DB_PATH.exists():
            raise SystemExit("dealdeck.duckdb not found. Run: python build_db.py")
        self.db = duckdb.connect(str(DB_PATH), read_only=True)
        self.index = FilingIndex(self.db)
        self.evidence = []   # text of every tool result, for the grounding check
        self.queries = []    # SQL run, for the answer's "data used" section
        self.deck = None

    def run(self, name: str, args: dict):
        """Returns (content for the tool_result, is_error)."""
        if name == "query_financials":
            return self.query_financials(args["sql"])
        if name == "search_filing":
            return self.search_filing(args["ticker"], args["query"])
        if name == "show_companies":
            self.deck = {"tickers": [t.upper() for t in args["tickers"]][:12], "reason": args["reason"]}
            return "The deck is shown to the user.", False
        return f"Unknown tool {name}", True

    def query_financials(self, sql: str):
        statement = sql.strip().rstrip(";")
        if not re.match(r"(?is)^\s*(select|with)\b", statement) or ";" in statement:
            return "Only one SELECT or WITH query is allowed.", True
        try:
            cursor = self.db.execute(statement)
            columns = [c[0] for c in cursor.description]
            rows = cursor.fetchmany(MAX_ROWS + 1)
        except duckdb.Error as error:
            return f"SQL error: {error}", True
        truncated = len(rows) > MAX_ROWS
        lines = [" | ".join(columns)] + [" | ".join("" if v is None else str(v) for v in row) for row in rows[:MAX_ROWS]]
        if truncated:
            lines.append(f"(more than {MAX_ROWS} rows; only the first {MAX_ROWS} are shown)")
        result = "\n".join(lines) if rows else "No rows."
        self.queries.append({"sql": statement, "rows": min(len(rows), MAX_ROWS)})
        self.evidence.append(result)
        return result, False

    def search_filing(self, ticker: str, query: str):
        ticker = ticker.upper()
        passages = self.index.search(ticker, query)
        if not passages:
            available = ", ".join(sorted(self.index.passages)) or "none"
            return f"No passage found for {ticker}. Reports available for: {available}.", True
        self.evidence += [p["text"] for p in passages]
        return [{
            "type": "search_result",
            "source": f"{p['url']}#{p['id']}",
            "title": f"{ticker} {p['form']} (period ending {p['period']}), {p['section']}",
            "content": [{"type": "text", "text": p["text"]}],
            "citations": {"enabled": True},
        } for p in passages], False


# ---------------------------------------------------------------- checks

NUMBER_RE = re.compile(
    r"(?<![\w.])([-−]?)\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*"
    r"(%|percent\b|trillion\b|billion\b|million\b|thousand\b|bn\b|[TBMK]\b)?(?!-[KQ])", re.I)
SCALES = {"t": 1e12, "trillion": 1e12, "b": 1e9, "bn": 1e9, "billion": 1e9, "m": 1e6, "million": 1e6,
          "k": 1e3, "thousand": 1e3}
ADVICE_RE = re.compile(
    r"\b(you should (buy|sell|hold|invest)|i (would )?recommend (buying|selling|holding|investing)|"
    r"(is|looks like) a (good|great|bad|strong) (investment|buy)|(strong )?buy rating|"
    r"(share|stock) price (will|should) (rise|fall|go up|go down))\b", re.I)


def parse_numbers(text: str) -> list[tuple[str, float, str]]:
    found = []
    for match in NUMBER_RE.finditer(text):
        sign, whole, decimals, unit = match.groups()
        value = float(whole.replace(",", "") + (decimals or "")) * (-1 if sign else 1)
        unit = (unit or "").lower()
        if unit in ("%", "percent"):
            found.append(("pct", value, match.group(0).strip()))
        elif unit in SCALES:
            found.append(("amount", value * SCALES[unit], match.group(0).strip()))
        else:
            found.append(("plain", value, match.group(0).strip()))
    return found


def ungrounded_figures(answer: str, evidence: list[str]) -> list[str]:
    amounts, percents = [], []
    for kind, value, _ in parse_numbers("\n".join(evidence)):
        (percents if kind == "pct" else amounts).append(value)
        if kind == "plain":
            percents.append(value * 100)  # SQL returns ratios like 0.556 for 55.6%
            percents.append(value)
            amounts += [value * scale for scale in (1e3, 1e6, 1e9, 1e12)]  # SQL rounded to billions etc.
    missing = []
    for kind, value, raw in parse_numbers(answer):
        if kind == "plain" and value == int(value) and (1900 <= value <= 2100 or abs(value) < 10):
            continue  # years and small counts
        if kind == "pct":
            ok = any(abs(value - p) <= 0.6 for p in percents)
        else:
            ok = any(abs(value - a) <= 0.015 * max(abs(a), 1) for a in amounts)
        if not ok:
            missing.append(raw)
    return missing


# ---------------------------------------------------------------- agent loop

def run_agent(question: str, verbose: bool = True) -> dict:
    client = anthropic.Anthropic()
    tools = Tools()
    messages = [{"role": "user", "content": question}]
    corrections = 0
    checks = {"grounding": "passed", "advice": "passed"}
    usage = Counter()

    for _ in range(MAX_TURNS):
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",                 # if the model declines, the API retries on a fallback model
            output_config={"effort": "medium"},
            cache_control={"type": "ephemeral"},  # the system prompt, tools and history are re-sent each turn
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )
        usage.update({"input": response.usage.input_tokens, "output": response.usage.output_tokens,
                      "cache_read": response.usage.cache_read_input_tokens or 0})
        messages.append({"role": "assistant", "content": response.content})  # unchanged, thinking blocks included

        if response.stop_reason == "refusal":
            raise SystemExit("The model declined to answer.")
        if response.stop_reason == "pause_turn":
            continue

        tool_uses = [block for block in response.content if block.type == "tool_use"]
        if tool_uses:
            results = []
            for tool_use in tool_uses:
                if verbose:
                    print(f"  → {tool_use.name}({json.dumps(tool_use.input, ensure_ascii=False)[:160]})")
                content, is_error = tools.run(tool_use.name, tool_use.input)
                results.append({"type": "tool_result", "tool_use_id": tool_use.id,
                                "content": content, "is_error": is_error})
            messages.append({"role": "user", "content": results})
            continue

        # Final answer: run the deterministic checks, allow one correction round.
        answer_text = "".join(block.text for block in response.content if block.type == "text")
        missing = ungrounded_figures(answer_text, tools.evidence)
        advice = ADVICE_RE.search(answer_text)
        if (missing or advice) and corrections < 1:
            corrections += 1
            problems = []
            if missing:
                problems.append("these figures do not appear in any tool result: " + ", ".join(missing) +
                                ". Get them with the tools (compute derived numbers in SQL) or remove them")
            if advice:
                problems.append(f"the answer contains investment advice (\"{advice.group(0)}\"). Remove it")
            if verbose:
                print(f"  ✗ check failed: {'; '.join(problems)}")
            messages.append({"role": "user", "content": "Automatic check: " + "; ".join(problems) + ". Then answer again."})
            continue
        checks["grounding"] = "passed" if not missing else "failed: " + ", ".join(missing)
        checks["advice"] = "passed" if not advice else "failed"
        return build_result(question, response, tools, checks, corrections, usage)

    raise SystemExit("The agent did not finish within the turn limit.")


def build_result(question, response, tools, checks, corrections, usage) -> dict:
    parts, quotes = [], []
    for block in response.content:
        if block.type != "text":
            continue
        refs = []
        for citation in block.citations or []:
            cited = (getattr(citation, "cited_text", "") or "").strip()
            quote = next((q for q in quotes if q["text"] == cited), None)
            if quote is None:
                quote = {"n": len(quotes) + 1, "text": cited, "title": getattr(citation, "title", None),
                         "source": getattr(citation, "source", None)}
                quotes.append(quote)
            refs.append(quote["n"])
        parts.append({"text": block.text, "refs": sorted(set(refs))})
    return {
        "question": question,
        "answer": parts,
        "quotes": quotes,
        "queries": tools.queries,
        "deck": tools.deck,
        "checks": checks,
        "corrections": corrections,
        "model": response.model,
        "usage": dict(usage),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def print_result(result: dict):
    print("\n" + "".join(p["text"] + "".join(f" [{n}]" for n in p["refs"]) for p in result["answer"]).strip())
    for quote in result["quotes"]:
        print(f"\n[{quote['n']}] {quote['title']}\n    \"{quote['text'][:400]}\"")
    if result["deck"]:
        print(f"\nDeck: {', '.join(result['deck']['tickers'])}\n  {result['deck']['reason']}")
    if result["queries"]:
        print(f"\nSQL queries run: {len(result['queries'])}")
    print(f"Checks: {result['checks']}  corrections: {result['corrections']}  tokens: {result['usage']}")


def test_tools():
    tools = Tools()
    print("query_financials:")
    print(tools.query_financials(
        """SELECT ticker, fiscal_year, round(revenue/1e9, 1) AS revenue_b,
                  round(100.0 * net_income / revenue, 1) AS net_margin_pct
           FROM annual WHERE ticker IN ('NVDA', 'AMD') AND fiscal_year >= 2024 ORDER BY ticker, fiscal_year""")[0])
    print("\nsearch_filing NVDA 'customer concentration percent of total revenue':")
    content, _ = tools.search_filing("NVDA", "direct customer percent of total revenue")
    for block in content[:2]:
        print(f"  {block['title']}\n    {block['content'][0]['text'][:300]}...")
    answer = "Nvidia's revenue reached $215.9B in fiscal 2026, and one direct customer made up 22% of it, versus $3.1B claimed."
    print(f"\ngrounding check on a test answer: unsupported figures = {ungrounded_figures(answer, tools.evidence)}")
    print(f"advice check: {bool(ADVICE_RE.search('You should buy Nvidia now.'))} (expected True)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("question", nargs="?")
    parser.add_argument("--test-tools", action="store_true", help="exercise the tools without the API")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    if args.test_tools:
        test_tools()
        return 0
    if not args.question:
        parser.print_help()
        return 1
    try:
        result = run_agent(args.question)
    except TypeError as error:
        if "authentication" not in str(error):
            raise
        print("No Anthropic API key found. Set ANTHROPIC_API_KEY and run again.", file=sys.stderr)
        return 1
    except anthropic.AuthenticationError:
        print("The Anthropic API key was rejected.", file=sys.stderr)
        return 1
    print_result(result)
    LOG_PATH.parent.mkdir(exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as log:
        log.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
