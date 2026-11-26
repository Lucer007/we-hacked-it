# Equitease data pipeline

Turns official French public data into the startup cards shown in the prototype.

## Run it

```
pip install -r requirements.txt
python build_cards.py        # fetch official data, build cards.json
python summarize.py          # optional: AI summaries (needs ANTHROPIC_API_KEY)
python build_cards.py        # merge the summaries into cards.json
```

`build_cards.py` caches each API answer in `raw/`. Use `--refresh` to download again.

## Files

| File | What it is |
|---|---|
| `startups.json` | The curated list: SIREN number, display name, Equitease sector tags, and what the startup says about itself (pitch, stage, amount raised). |
| `build_cards.py` | Fetches official data and writes `cards.json`. |
| `summarize.py` | Asks Claude for a two- or three-sentence summary of each card, citing fact numbers. Writes `ai_summaries.json`. |
| `ai_summaries.json` | The summaries. The current ones were written in a Claude Code session under the same rules, because no API key was available during the hackathon. |
| `cards.json` | The output the prototype reads. |

## Where the data comes from

One free public API, no key needed: `recherche-entreprises.api.gouv.fr`. It combines:

- **INSEE Sirene**: registered activity (NAF code), founding date, address, headcount band, company category.
- **National Business Register (INPI)**: registered officers, mission-driven company status.
- **Filed annual accounts**: revenue and net income, when the company publishes them.
- **ADEME records**: ADEME support received, greenhouse gas report published.

Each card links to the company's public page on `annuaire-entreprises.data.gouv.fr`.

## Rules the pipeline follows

1. **Numbers never come from the AI.** Plain code copies them from the registers. The AI only writes a summary, and every sentence lists the numbered facts it relies on. A sentence that cites a fact that doesn't exist is dropped.
2. **Every fact is tagged Official or Self-reported.** Official means it comes from a public register. Self-reported means the startup said it (pitch, stage, amount raised) and nobody has checked it.
3. **No investment advice.** The summary prompt forbids saying whether a company is a good investment. This keeps Equitease an information service, not regulated investment advice.
4. **A revenue of 0 counts as "not public".** Small companies can legally keep their income statement confidential, and many startups do. The register then shows 0, which can't be told apart from no sales.
5. **Closed companies are skipped.**

## Limits to know before the demo

- The pitch lines in `startups.json` are short summaries of each company's own website, written by the team. Check them before the demo. In the real product, the startup writes its own.
- Most of these companies have opposed commercial reuse of their register data (the `commercial_use_opposition` flag). In the real product, a startup only appears after it signs up, so it consents to showing its data.
- Revenue can be a few years old: the card shows the year of the latest published accounts.
- "Company category" is assessed by INSEE at group level, so a small subsidiary of a big group shows as a large company.
- The thesis fit uses Equitease's own sector tags plus official age, headcount and category. Edit `THESIS` at the top of `build_cards.py` to change it.
