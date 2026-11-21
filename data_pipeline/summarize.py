"""Write the AI summary of each company card, with every sentence citing its facts.

For each card in cards.json, Claude gets the card's numbered facts and writes two or three
sentences for an investor. Each sentence lists the numbers of the facts it relies on.
The card builders later drop any sentence that cites a fact that does not exist.

Output: ai_summaries.json next to cards.json, keyed by SIREN (France) or ticker (S&P 500).
Run the card builder again afterwards to merge it.

Needs an Anthropic API key in ANTHROPIC_API_KEY, and: pip install -r requirements.txt

Usage:
  python summarize.py                      # French startups (cards.json here)
  python summarize.py --folder sp500 --only NVDA AAPL   # S&P 500 cards
  python summarize.py --all    # rewrite every summary
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic

HERE = Path(__file__).resolve().parent
MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = """You write the short summary shown on a company profile card. The readers are \
investors and curious people deciding whether to look closer at the company.

Use only the numbered facts you are given; never add outside knowledge or guess. Write two or \
three short sentences in plain English. For each sentence, list the numbers of the facts it \
relies on. When a fact is self-reported by the startup, make that clear ("according to the \
startup"). When a number is not public, you may say so.

Do not give investment advice: do not say whether the company is a good or bad investment, do \
not recommend buying or passing, and do not predict future performance."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "sentences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "refs": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["text", "refs"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["sentences"],
    "additionalProperties": False,
}


def facts_prompt(card: dict) -> str:
    lines = [f"Company: {card['name']}", "", "Facts:"]
    for fact in card["facts"]:
        kind = "official register" if fact["official"] else "self-reported by the startup"
        lines.append(f"[{fact['n']}] {fact['fact']} (source: {fact['source']}; {kind})")
    return "\n".join(lines)


def summarize(client: anthropic.Anthropic, card: dict) -> list[dict] | None:
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # if the model declines, the API retries on a fallback model
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": facts_prompt(card)}],
    )
    if response.stop_reason == "refusal":
        print(f"  declined for {card['name']}", file=sys.stderr)
        return None
    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        return None
    valid = {f["n"] for f in card["facts"]}
    sentences = [s for s in json.loads(text)["sentences"] if s["refs"] and set(s["refs"]) <= valid]
    return sentences or None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true", help="rewrite summaries that already exist")
    parser.add_argument("--folder", default=".", help="folder holding cards.json, e.g. sp500")
    parser.add_argument("--only", nargs="*", help="only these card ids (SIREN or ticker)")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    folder = HERE / args.folder
    cards = json.loads((folder / "cards.json").read_text(encoding="utf-8"))["cards"]
    out_path = folder / "ai_summaries.json"
    summaries = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}

    client = anthropic.Anthropic()
    for card in cards:
        card_id = card.get("siren") or card["id"]
        if args.only and card_id not in args.only:
            continue
        if card_id in summaries and not args.all:
            continue
        print(f"Summarising {card['name']}...")
        try:
            sentences = summarize(client, card)
        except anthropic.AuthenticationError:
            print("The Anthropic API key was rejected. Check ANTHROPIC_API_KEY and run again.", file=sys.stderr)
            return 1
        except TypeError as error:
            if "authentication" not in str(error):
                raise
            print("No Anthropic API key found. Set ANTHROPIC_API_KEY and run again.", file=sys.stderr)
            return 1
        except anthropic.APIStatusError as error:
            print(f"  API error for {card['name']}: {error.status_code} {error.message}", file=sys.stderr)
            continue
        except anthropic.APIConnectionError:
            print(f"  network error for {card['name']}, skipped", file=sys.stderr)
            continue
        if sentences:
            summaries[card_id] = {
                "fiscal_year": card.get("fiscal_year"),  # lets the card builder drop stale summaries
                "generated_by": MODEL,
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "sentences": sentences,
            }
            out_path.write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")

    print("Done. Now run: python build_cards.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
