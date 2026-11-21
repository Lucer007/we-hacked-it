# Handoff: Equiteas (Sciences Po AI Hackathon)

State on Saturday 21 November 2026. Read this first, then the READMEs it points to.

## Deadline and deliverables

- **Submission: Saturday 16:00, no extensions** (aim for 15:30): prototype link, short demo video
  (1–2 min), slide deck. **Everything in English.** Chat with Théo in French.
- Finalists announced 17:45; final 18:00: 5-minute pitch + 5-minute Q&A.
- Jury weights: problem 15% (evidence: interviews, data) · solution fit + business model, unit
  economics, acquisition 35% · demo works end to end 25% · UX and polish 25%.
- Deck must cover: problem, solution, market size, impact, business model, go-to-market.

## The product

**Equiteas**, a "Tinder for investing" for people new to investing. Users answer a few
preference questions, then swipe company cards that show facts (what the company does, CEO,
employees, mission, risks, legal issues), never the ticker or price. A right swipe reveals the
stock; a fractional buy goes through a licensed broker partner. Global scope, starting with
the S&P 500.

- Reference design (Lijith, 19 screens): https://claude.ai/artifact/2XD3kUZUGGeXc9qQgH56v2
- **Working prototype** (Claude, standalone page): https://claude.ai/artifact/JMzfRxZtjLEbJWduCoJNoJ
  **Still private: Théo must share it (Share menu) before submitting it as the prototype link.**
- Older mockup, superseded: https://claude.ai/artifact/4JfFexnpsnjYmKrG1Hoait

## What exists (committed and pushed to `main`)

| Path | What it is | Status |
|---|---|---|
| `prototype/` | `app.template.html` → `build_prototype.py` → `index.html` (the published page). Edit the template, never `index.html`. | Built, published (version 2) and tested end to end in Chrome on 21 Nov: questions, swipe, card back and sources, pass/undo, like, reveal, buy, wishlist, Ask (incl. the NVDA vs AMD question), Tune your feed, end of deck. No console errors. Not yet tried on a real phone |
| `data_pipeline/sp500/sec.py` | SEC client (cache, rate limit, User-Agent) | Works |
| `data_pipeline/sp500/build_sp500_cards.py` | Financial cards for the S&P 500 from SEC XBRL frames → `cards.json` | 492/503 companies; spot-checked against known figures |
| `data_pipeline/sp500/build_db.py` | `dealdeck.duckdb`: companies, 5 years of annual data, balance sheets, 10-K passages | Works (12 reports chunked) |
| `data_pipeline/sp500/ask_company.py` | Downloads a 10-K/20-F as text; Q&A with document citations | Download works; **Q&A never run (no API key)** |
| `data_pipeline/sp500/analyst.py` | Analyst agent: manual tool loop on claude-opus-5-5 with `query_financials` (SQL), `search_filing` (BM25, `search_result` blocks with citations), `show_companies`; then deterministic grounding and no-advice checks | Tools and checks tested offline (`--test-tools`); **agent loop never run** |
| `data_pipeline/sp500/build_profile_cards.py` | Card front/back from the 10-K: LLM extraction with structured output + quotes, then code verifies each quote word for word | Assemble step works; **extraction step never run**; `profile_extractions.json` was written by hand for 12 companies; 140 quotes, all verified |
| `data_pipeline/summarize.py`, `*/ai_summaries.json` | Short card summaries citing fact numbers | Summaries hand-written for 15 French + 10 US cards |
| `data_pipeline/build_cards.py`, `startups.json` | First pipeline (French startups, INSEE/INPI) | Superseded by the S&P 500 scope; keep as the "private companies" example |

The 12 demo companies (10-K downloaded): NVDA, AMD, TSM, AAPL, MSFT, GOOGL, AMZN, WMT, TSLA,
KO, JPM, LLY. Details: `data_pipeline/sp500/README.md`, `prototype/README.md`.

## Decisions on 21 Nov (morning)

- **Name: Equiteas** (was Dealdeck). Renamed in the app and the docs; file names such as
  `dealdeck.duckdb` are unchanged.
- **Buy wording:** the reveal button says just "Buy" and the order type is "Market order"; no
  mention of fractional shares (too complex for the pitch). The amount is still picked in euros.
- **Design = the pitch deck's:** deep green-black, mint accent `#5EE5A8`, Poppins for headings and
  controls, Century Gothic (Questrial fallback) for text, glass panels. The sprout logo and the
  ribbon on the welcome screen are cut from the deck cover (`prototype/assets/`, inlined by
  `build_prototype.py`). Welcome headline = deck tagline.
- **No backend:** there is no FastAPI and no live DB connection. The page is static; the data is
  read from `profile_cards.json` and DuckDB (`build_prototype.py` opens it read-only to re-check the
  demo quotes) at build time and embedded in `index.html`. Free
  questions in "Ask" show a message saying the analyst agent answers them in the live app.
- Bugs fixed in the template: the liked card came back on the Discover tab; re-rendering the
  same screen jumped to the top (wishlist button, buy checkbox). Locally, `hidden` elements
  showed (grey overlay) and accents broke because the template had no `[hidden]` rule and no
  charset; the published wrapper already covered both.
- Local preview: `cd prototype; python -m http.server 8791 --bind 127.0.0.1` then open
  http://127.0.0.1:8791/index.html (port 8765 is taken by an old server on Théo's machine).

## Constraints and gotchas

- **No Anthropic API key** (the hackathon gives Claude Pro only). Every AI output so far was
  written in a Claude Code session under the same rules as the scripts. Ask the Anthropic team
  on site for API credits; then run `analyst.py` on the demo questions and
  `build_profile_cards.py` (without `--assemble-only`), and fix what breaks at runtime.
- **SEC www.sec.gov needs a contact email in the User-Agent.** Théo sets it himself in his
  PowerShell terminal: `$env:SEC_USER_AGENT = "Equiteas <his email>"`. A privacy hook blocks
  Claude from putting his email in commands. data.sec.gov (the XBRL API) works without it.
- **No share-price source:** Stooq is behind an anti-bot check; don't work around it. The
  reveal screen says the price comes from the broker partner in the live app.
- Théo's terminal is Windows PowerShell; Claude's Bash tool is Git Bash. The `!` prefix only
  works in the Claude Code input box.
- Python packages installed: anthropic 2.12, duckdb 2.0.1, beautifulsoup4, lxml, requests.
- `.gitignore` excludes SEC caches, `dealdeck.duckdb` and raw register data (personal data).
- The reports contradict outdated knowledge: Apple's CEO is John Ternus, Coca-Cola's is
  Henrique Braun (from 31 March 2026), Walmart's is John Furner. Always take facts from the
  reports, never from memory.

## Compliance framing (for the pitch and the Q&A)

- Information and discovery, never advice: no "buy", "recommended for you"; wording is "shown
  because of your answers". The agent prompt forbids advice and a regex check enforces it.
- Experience and risk answers set tone and explanations, never the ranking (otherwise it looks
  like a suitability test, i.e. regulated investment advice).
- Buying = partnering with a licensed broker API (e.g. Upvest, DriveWealth, Alpaca), which
  handles KYC and execution. Business model: commission and/or a commission-free subscription,
  or a white-label B2B2C deal with brokers and neobanks.

## Pitch talking points (AI, for a technical jury)

- Numbers never come from the AI: code copies them from SEC filings (XBRL).
- The AI reads 150-page reports and fills the cards; code checks every quote word for word
  (change one digit and the fact is dropped). 140 facts verified on 12 companies.
- One agent where it is justified (multi-hop questions): SQL + filing search with native
  citations, then grounding and no-advice checks with a correction round.
- Demo question: "Is Nvidia more dependent on a few customers than AMD?" → Nvidia: one direct
  customer 22% and another 14% of FY2026 revenue; AMD: no customer at 10% or more (FY2025, 2024).
- Vision: monitoring agent on new filings, ESG and news, global coverage (EU filings, national
  registers), swipe recommender with active learning for the "Tune your feed" questions.

## Next steps

1. Théo: share the prototype link; try the flow once on phone and laptop.
2. Demo video script (1–2 min) following the prototype flow, then record it.
3. Slide deck (6 required sections), with numbers: market size, pricing, first customers.
4. Evidence: 5 quick interviews on site (how people pick companies today, would they pay).
5. If an API key arrives: run the agent and the extraction for real, record that in the video.
6. Commit and push to github.com/theofarouk/we-hacked-it before submitting.
