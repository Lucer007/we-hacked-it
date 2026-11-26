# Equiteas prototype

`index.html` is a single, self-contained page: the swipe app from Lijith's mockup running on
12 real companies (NVDA, AMD, TSM, AAPL, MSFT, GOOGL, AMZN, WMT, TSLA, KO, JPM, LLY).

Screens follow the wireframe v2 (https://claude.ai/artifact/24yQRUjVSvunEx8p1i6BbY):

1. Welcome → Create account (Apple / Google / email, front-end only) → Verify email (any 6 digits).
   "I already have an account" signs in the demo user (Warren / admin) and goes straight to the feed: handy for the video.
2. Quick (8) or Full (21) setup: how new, what to swipe, values (what matters most in a company's vision,
   which mission you'd back, how you see your investment), sectors, ESG, hidden sectors (+ 13 more in Full:
   controversies, risk, budget, rivals…) → Feed ready → How it works (brand-new users).
   A mission moves its sub-sectors up in the feed (`MISSION_SUBS` in the template); "How do you see your
   investment?" changes nothing in the ranking.
3. Discover: filter chips, one short card with the essentials only (name, tagline, sector, CEO, HQ,
   mission, what they do), edge to edge on the screen, pass/undo, Tune your feed.
4. Reveal = the company page: price chart, key numbers, staff, founding year, size, dividend, then segments,
   risks, ESG, controversies and the sources of the company facts (`companyDetails` in the template) + "New word" pop-up → amount → ID check (first buy, demo) → review with the risk checkbox →
   share with friends → Social.
5. Learn (4 lessons with a quiz, words met while swiping, glossary search), Social (sample friends,
   friend profile), Wishlist, Profile and settings.

Real data: the 12 company cards and their sources. Showcase only (no backend):
account, ID check, order, ESG rating, friends and lessons progress; everything lives in the page's memory.

## Rebuild after the data changes

```
cd ../data_pipeline/sp500
python build_profile_cards.py --assemble-only   # or without the flag once there is an API key
cd ../../prototype
python build_prototype.py                       # writes index.html from app.template.html
```

Edit the page in `app.template.html`, never in `index.html` (it is overwritten).

## What is demo-only

- **Share price:** no free price feed allows automated use, so the reveal says the price comes
  from the broker partner in the live app.
- **Buy:** no order is placed.
- **Analyse my portfolio (Portfolio tab):** a scripted replay of the analyst agent, with no API
  call. A robot hops you → sector → company on the map (about 8 s, "Skip" to jump), and each step
  shows a tool name (`query_financials`, `search_filing`) with a prepared label. Then a short
  concentration report. The percentages are computed from the holdings (a sector at 30% or more is
  coral, 20–29% or several tech sectors adding up to 40% or more is amber, and so is a single
  company at 15% or more). The TSMC link and the quote come from the Nvidia and AMD 10-Ks. The
  report describes the portfolio and gives no advice. Code: `botAnalysis`, `startBot` in the template.
- **Recommender:** sector weights from the answers, updated by each swipe (pass ×0.6, like ×1.3),
  with some randomness so other sectors still appear, and one "Tune your feed" card after 4 swipes.

## Netlify

`netlify.toml` at the repository root publishes this folder as is (no build step), so the site root
serves `index.html`. Rebuild and commit `index.html` before pushing. On Netlify there is no FastAPI
backend: the `/api/health` check fails and the page runs on its embedded data.
