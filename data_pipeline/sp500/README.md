# S&P 500 pipeline and "Ask the company"

## Setup

```
pip install -r requirements.txt
export SEC_USER_AGENT="Equiteas you@example.com"   # the SEC requires a contact email
```

On Windows PowerShell: `$env:SEC_USER_AGENT = "Equiteas you@example.com"`

## 1. Cards for the whole S&P 500

```
python build_sp500_cards.py
```

Builds `cards.json`: 492 of the 503 companies (the others haven't filed an annual report in
the SEC's structured format yet, or tag their revenue differently).

- **Company list:** the public S&P 500 constituents list (ticker, name, GICS sector, SEC ID).
- **Numbers:** the SEC's XBRL "frames", one download per accounting item and year covering
  every US-listed company. About 80 downloads in total, cached in `cache/`.
- **Each fact links to the exact SEC filing** it comes from.
- Revenue, growth, net income and margin, R&D, dividends, cash and long-term debt.
- **Banks and real-estate trusts** don't tag total revenue like other companies. When a
  company's profit is larger than its tagged revenue, the card says revenue is
  "not reported in a comparable form" instead of showing a wrong number.

## 2. Ask the company

```
python ask_company.py NVDA "Who are NVIDIA's biggest customers?"
```

Finds the latest annual report (10-K, or 20-F for foreign companies such as TSM, ASML or
SAP), converts it to text, and asks Claude with **citations turned on**. Every statement in
the answer comes with the exact passage of the report it relies on, and the section it is in
("Item 1A. Risk Factors"). Answers are saved to `answers/<TICKER>.json` for the prototype.

- The whole report goes to Claude (100k to 200k tokens). Prompt caching makes follow-up
  questions on the same company much cheaper.
- The prompt forbids investment advice: no buy, sell or hold, no price predictions.
- Needs `ANTHROPIC_API_KEY`. `--text-only` downloads the report without calling the API.

## 3. The analyst agent

```
python build_db.py                 # builds dealdeck.duckdb (run again after downloading reports)
python analyst.py --test-tools     # checks the tools without calling the API
python analyst.py "Is Nvidia more dependent on a few customers than AMD?"
python analyst.py "Show me profitable healthcare companies with growing revenue and low debt"
```

`dealdeck.duckdb` holds `companies`, `annual` (5 fiscal years of income-statement items),
`balance_sheet`, `filings` and `filing_chunks` (annual reports cut into ~1,300-character
passages, tagged with their section).

`analyst.py` is a tool-use loop on `claude-opus-5-5` with three tools:

| Tool | What it does |
|---|---|
| `query_financials(sql)` | Read-only SQL on DuckDB. The schema is in the tool description. Errors go back to the model, which fixes its query. |
| `search_filing(ticker, query)` | BM25 (words and word pairs) over one company's report. Returns `search_result` blocks with citations on, so the answer cites passages natively. |
| `show_companies(tickers, reason)` | Puts a deck of cards in front of the user. The agent calls it when the user describes what they want to see. |

After the final answer, two deterministic checks run before anything reaches the user:

- **Grounding:** every figure in the answer must appear in a tool result (SQL rows or retrieved
  passages). The model is told to do its arithmetic in SQL so that this holds.
- **No advice:** the answer must not tell the user to buy, sell or hold.

A failed check goes back to the model for one correction round. The result (answer, cited
passages, SQL run, deck, check results, tokens) is appended to `answers/analyst_log.jsonl`.

It is a hand-written loop rather than the SDK's tool runner, because the search tool returns
`search_result` blocks and the checks need to inject a correction turn.

## 4. Swipe cards from annual reports (Lijith's mockup)

```
python build_profile_cards.py                  # extract with Claude, then assemble
python build_profile_cards.py --assemble-only  # no API call
```

Fills the front (tagline, CEO, employees, HQ, mission) and back (founded, founder-led,
segments, key numbers, risks, legal matters) of each card from the company's annual report.

1. **Extract:** Claude reads the business, risk-factor, legal and executive-officer sections
   and returns a structured form (JSON schema). Every value comes with a quote copied from
   the report. Output: `profile_extractions.json`.
2. **Assemble:** plain code looks for every quote, word for word (case, spacing and curly
   quotes normalized), in the report. A value whose quote isn't found is dropped and listed
   in `dropped_unverified`. Changing one digit of a quote is enough to get it rejected.
   Output: `profile_cards.json`, with the quote and report section of every field.

The 12 extractions in `profile_extractions.json` were written by hand in a Claude Code
session, under the same rules, because no API key was available yet. All 140 quotes pass
the check. Fields the reports don't state are left empty: Tesla and TSMC don't give a total
headcount. When the report doesn't give a founding year, it comes from the S&P 500 list
and `founded_source` says so. TSMC reports under IFRS, so it has no SEC key numbers.

## Global coverage

The SEC covers every US-listed company plus the foreign companies listed in the US. For the
rest of the world, the same card format works with other official sources: listed EU
companies publish structured annual reports (filings.xbrl.org), and national registers cover
private companies (see `../build_cards.py` for France).
