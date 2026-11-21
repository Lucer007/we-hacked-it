# Equiteas prototype

`index.html` is a single, self-contained page: the swipe app from Lijith's mockup running on
12 real companies (NVDA, AMD, TSM, AAPL, MSFT, GOOGL, AMZN, WMT, TSLA, KO, JPM, LLY).

Flow: welcome → 3 preference questions → feed summary → blind swipe (front, tap for the back
with sources) → right swipe reveals ticker and key numbers → demo buy with the compliance
checkbox → wishlist. "Ask" opens ready-made questions answered only from verified quotes.

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
- **Free questions:** the text box explains that the analyst agent answers them in the live app.
  The listed questions are real: built from quotes checked against the reports.
- **Buy:** no order is placed.
- **Recommender:** sector weights from the answers, updated by each swipe (pass ×0.6, like ×1.3),
  with some randomness so other sectors still appear, and one "Tune your feed" card after 4 swipes.
