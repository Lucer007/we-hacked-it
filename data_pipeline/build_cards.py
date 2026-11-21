"""Build Dealdeck startup cards from official French public data.

What it does:
  1. Reads startups.json: the curated list (SIREN + what each startup says about itself).
  2. Fetches official data for each SIREN from the public company search API
     (recherche-entreprises.api.gouv.fr, which combines INSEE Sirene, the INPI
     national business register, published annual accounts and ADEME records).
  3. Turns it into numbered facts, each tagged official or self-reported, with its source.
  4. Merges the AI summaries from ai_summaries.json, if present (see summarize.py).
  5. Writes cards.json, the file the prototype reads.

Rule: numbers never come from the AI. They are copied from the registers here, in plain
code. The AI only writes a short summary that points back to these numbered facts.

Usage:
  python build_cards.py            # reuses cached API answers in raw/
  python build_cards.py --refresh  # downloads everything again
"""

import argparse
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
API_URL = "https://recherche-entreprises.api.gouv.fr/search"
USER_AGENT = "dealdeck-hackathon-prototype"
REGISTER_PAGE = "https://annuaire-entreprises.data.gouv.fr/entreprise/{siren}"

# The demo fund's investment thesis, used for the "Fits your thesis" score.
THESIS = {
    "label": "Energy & mobility · ≤ 8 yrs · ≤ 49 staff · SME",
    "sectors": ["energy", "mobility"],
    "max_age_years": 8,
    "max_headcount_code": "12",  # INSEE band 12 = 20 to 49 employees
}

# INSEE headcount bands (tranche d'effectif salarié).
HEADCOUNT_BANDS = {
    "00": "0", "01": "1–2", "02": "3–5", "03": "6–9", "11": "10–19", "12": "20–49",
    "21": "50–99", "22": "100–199", "31": "200–249", "32": "250–499", "41": "500–999",
    "42": "1,000–1,999", "51": "2,000–4,999", "52": "5,000–9,999", "53": "10,000+",
}

# INSEE company categories, assessed at group level.
CATEGORIES = {"PME": "SME", "ETI": "Mid-sized company", "GE": "Large company"}

# NACE Rev. 2 section names (first level of the French NAF activity code).
NACE_SECTIONS = {
    "A": "Agriculture, forestry and fishing", "B": "Mining and quarrying", "C": "Manufacturing",
    "D": "Electricity and gas supply", "E": "Water, sewerage and waste management",
    "F": "Construction", "G": "Wholesale and retail trade", "H": "Transportation and storage",
    "I": "Accommodation and food services", "J": "Information and communication",
    "K": "Financial and insurance activities", "L": "Real estate activities",
    "M": "Professional, scientific and technical activities",
    "N": "Administrative and support services", "O": "Public administration", "P": "Education",
    "Q": "Human health and social work", "R": "Arts, entertainment and recreation",
    "S": "Other service activities", "T": "Household activities",
    "U": "Extraterritorial organisations",
}

# English labels for the NACE classes used in the demo list. Unknown codes fall back
# to the section name above.
NACE_CLASSES = {
    "20.16": "Manufacture of plastics in primary forms",
    "22.29": "Manufacture of other plastic products",
    "26.11": "Manufacture of electronic components",
    "35.11": "Production of electricity",
    "58.29": "Software publishing",
    "62.01": "Computer programming",
    "63.11": "Data processing and hosting",
    "70.22": "Business and management consultancy",
    "71.12": "Engineering and technical consultancy",
    "72.11": "Research and development in biotechnology",
    "72.19": "Research and development in natural sciences and engineering",
    "74.90": "Other professional, scientific and technical activities",
    "77.11": "Renting and leasing of cars",
    "95.22": "Repair of household appliances",
}

# Official yes/no signals from the API's "complements" block.
SIGNALS = [
    ("a_aide_ademe", "Has received support from ADEME, the French ecological transition agency",
     "ADEME funding records"),
    ("bilan_ges_renseigne", "Has published a greenhouse gas emissions report",
     "ADEME greenhouse gas report register"),
    ("est_societe_mission", "Registered as a mission-driven company (société à mission)",
     "National Business Register (INPI)"),
    ("est_ess", "Part of the social and solidarity economy (ESS)",
     "Social and solidarity economy register"),
]

# English names for the officer roles found in the national business register.
OFFICER_ROLES = {
    "président de sas": "President", "président": "President",
    "directeur général": "Chief executive", "directeur général délégué": "Deputy chief executive",
    "gérant": "Managing director", "co-gérant": "Co-managing director",
    "président du conseil d'administration": "Chair of the board",
    "administrateur": "Board member", "président du directoire": "Chair of the management board",
    "membre du directoire": "Management board member",
    "président du conseil de surveillance": "Chair of the supervisory board",
    "membre du conseil de surveillance": "Supervisory board member",
    "autre": "Other officer",
}

SMALL_WORDS = {"de", "du", "des", "la", "le", "les", "sur", "sous", "en", "et", "d", "l", "aux", "au"}


def fetch_company(siren: str, refresh: bool) -> dict:
    """Return the API record for one SIREN, cached in raw/<siren>.json."""
    cache = HERE / "raw" / f"{siren}.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))

    last_error = None
    for attempt in range(5):
        try:
            response = requests.get(
                API_URL,
                params={"q": siren, "per_page": 5},
                headers={"User-Agent": USER_AGENT},
                timeout=20,
            )
            if response.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            response.raise_for_status()
            matches = [r for r in response.json().get("results", []) if r.get("siren") == siren]
            if not matches:
                raise LookupError(f"SIREN {siren} not found in the register")
            record = matches[0]
            cache.parent.mkdir(exist_ok=True)
            cache.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
            time.sleep(0.3)  # stay well under the API's rate limit
            return record
        except (requests.RequestException, ValueError) as error:
            last_error = error
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Could not fetch SIREN {siren}: {last_error}")


def euros(value: float) -> str:
    sign = "−" if value < 0 else ""
    value = abs(value)
    if value >= 1_000_000:
        return f"{sign}€{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{sign}€{round(value / 1_000)}k"
    return f"{sign}€{round(value)}"


def city_name(raw: str | None) -> str:
    if not raw:
        return "Unknown city"
    parts = []
    for word in raw.lower().split(" "):
        hyphenated = [p if p in SMALL_WORDS and i > 0 else p.capitalize()
                      for i, p in enumerate(word.split("-"))]
        parts.append("-".join(hyphenated))
    return " ".join(parts)


def activity_label(record: dict) -> tuple[str, str]:
    code = record.get("activite_principale") or ""
    label = NACE_CLASSES.get(code[:5]) or NACE_SECTIONS.get(record.get("section_activite_principale") or "", "Unknown activity")
    return label, code


def published_accounts(record: dict) -> list[tuple[int, dict]]:
    """Years with a published revenue figure, newest first.

    The API reports 0 when a company kept its income statement confidential (a legal
    option for small companies) and also when it had no sales, so 0 counts as not public.
    """
    finances = record.get("finances") or {}
    years = [(int(y), v) for y, v in finances.items() if v and v.get("ca")]
    return sorted(years, reverse=True)


def build_card(entry: dict, record: dict) -> dict:
    siren = entry["siren"]
    source_url = REGISTER_PAGE.format(siren=siren)
    siege = record.get("siege") or {}
    city = city_name(siege.get("libelle_commune"))
    founded = record.get("date_creation") or ""
    founded_year = int(founded[:4]) if founded else None
    activity, naf_code = activity_label(record)
    headcount_code = record.get("tranche_effectif_salarie") or "NN"
    headcount = HEADCOUNT_BANDS.get(headcount_code)
    headcount_year = record.get("annee_tranche_effectif_salarie")
    category_code = record.get("categorie_entreprise")
    accounts = published_accounts(record)

    facts = []

    def add(fact: str, source: str, official: bool, url: str | None = None):
        facts.append({"n": len(facts) + 1, "fact": fact, "source": source,
                      "source_url": url if official else None, "official": official})

    # What the startup says it does.
    if entry.get("pitch"):
        add(f"Activity: {entry['pitch']}", entry.get("pitch_source") or "Provided by the startup", False)

    # Official register data.
    add(f"Registered activity: {activity} (NAF {naf_code})", "INSEE Sirene register", True, source_url)
    if founded:
        add(f"Founded on {founded} in {city}", "INSEE Sirene register", True, source_url)
    if accounts:
        year, values = accounts[0]
        add(f"Revenue {year}: {euros(values['ca'])}", "Annual accounts filed with the commercial court registry", True, source_url)
        if values.get("resultat_net") is not None:
            add(f"Net income {year}: {euros(values['resultat_net'])}", "Annual accounts filed with the commercial court registry", True, source_url)
        if len(accounts) > 1:
            prev_year, prev = accounts[1]
            growth = (values["ca"] - prev["ca"]) / prev["ca"] * 100
            add(f"Revenue growth {prev_year}→{year}: {growth:+.0f}%", "Computed from filed annual accounts", True, source_url)
    else:
        add("Revenue: not public (accounts kept confidential or no sales reported)", "Annual accounts register", True, source_url)
    if headcount:
        add(f"Employees: {headcount} ({headcount_year})", "INSEE headcount band, Sirene register", True, source_url)
    if category_code in CATEGORIES:
        add(f"Company category: {CATEGORIES[category_code]} ({record.get('annee_categorie_entreprise')})",
            "INSEE, assessed at group level", True, source_url)
    officers = [d for d in record.get("dirigeants") or []
                if "commissaire aux comptes" not in (d.get("qualite") or "").lower()]
    if officers:
        roles = sorted({OFFICER_ROLES.get((d.get("qualite") or "").strip().lower().replace("’", "'"), d.get("qualite") or "Officer")
                        for d in officers})
        add(f"Registered officers: {len(officers)} ({', '.join(roles)})", "National Business Register (INPI)", True, source_url)
    complements = record.get("complements") or {}
    for key, text, source in SIGNALS:
        if complements.get(key):
            add(text, source, True, source_url)

    # Self-reported fundraising details.
    for key, label in (("stage", "Stage"), ("raising", "Raising"), ("already_raised", "Already raised")):
        if entry.get(key):
            add(f"{label}: {entry[key]}", "Provided by the startup, not verified", False)

    # Four headline numbers for the front of the card.
    stats = []
    if accounts:
        year, values = accounts[0]
        stats.append({"label": f"Revenue {year}", "value": euros(values["ca"]), "official": True})
        if values.get("resultat_net") is not None:
            stats.append({"label": f"Net income {year}", "value": euros(values["resultat_net"]), "official": True})
    else:
        stats.append({"label": "Revenue", "value": "Not public", "official": True})
    stats.append({"label": "Employees", "value": headcount or "Not public", "official": True})
    if founded_year:
        stats.append({"label": "Founded", "value": str(founded_year), "official": True})
    stats.append({"label": "Already raised", "value": entry.get("already_raised") or "Not provided", "official": False})
    stats = stats[:4]

    # Thesis fit: every criterion is checked against official data or Dealdeck's sector tags.
    sectors = entry.get("sectors") or []
    in_thesis_sector = any(s in THESIS["sectors"] for s in sectors)
    age = date.today().year - founded_year if founded_year else None
    small_enough = headcount_code != "NN" and headcount_code <= THESIS["max_headcount_code"]
    fit = [
        {"label": (sectors[0].capitalize() if in_thesis_sector else f"{(sectors or ['Unknown'])[0].capitalize()} (off-thesis)"),
         "ok": in_thesis_sector},
        {"label": (f"Founded {founded_year}" if age is not None and age <= THESIS["max_age_years"]
                   else f"Founded {founded_year} (too old)" if founded_year else "Founding date unknown"),
         "ok": age is not None and age <= THESIS["max_age_years"]},
        {"label": (f"{headcount} staff" if small_enough
                   else f"{headcount} staff (too big)" if headcount else "Headcount not public"),
         "ok": small_enough},
        {"label": ("SME" if category_code == "PME"
                   else f"{CATEGORIES.get(category_code, 'Unknown category')} (not an SME)"),
         "ok": category_code == "PME"},
    ]

    return {
        "siren": siren,
        "name": entry.get("display_name") or record.get("nom_raison_sociale"),
        "legal_name": record.get("nom_raison_sociale"),
        "sector": activity,
        "naf_code": naf_code,
        "sectors": sectors,
        "city": city,
        "founded": founded,
        "founded_year": founded_year,
        "stage": entry.get("stage"),
        "raising": entry.get("raising"),
        "pitch": entry.get("pitch"),
        "register_url": source_url,
        "stats": stats,
        "fit": fit,
        "fit_score": f"{sum(f['ok'] for f in fit)}/{len(fit)}",
        "facts": facts,
        "summary": None,
        "flags": {
            # True when the company has opposed commercial reuse of its register data.
            # In the real product, such a company only appears if it signs up itself.
            "commercial_use_opposition": bool(record.get("opposition_diffusion_commerciale")),
        },
    }


def merge_summaries(cards: list[dict]) -> int:
    path = HERE / "ai_summaries.json"
    if not path.exists():
        return 0
    summaries = json.loads(path.read_text(encoding="utf-8"))
    merged = 0
    for card in cards:
        item = summaries.get(card["siren"])
        if not item:
            continue
        valid_numbers = {f["n"] for f in card["facts"]}
        sentences = [s for s in item.get("sentences", [])
                     if s.get("refs") and set(s["refs"]) <= valid_numbers]
        if sentences:
            card["summary"] = sentences
            card["summary_generated_by"] = item.get("generated_by")
            merged += 1
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--refresh", action="store_true", help="download the API data again")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # € and accents in the Windows console

    config = json.loads((HERE / "startups.json").read_text(encoding="utf-8"))
    cards = []
    for entry in config["startups"]:
        try:
            record = fetch_company(entry["siren"], args.refresh)
        except (RuntimeError, LookupError) as error:
            print(f"SKIP {entry.get('display_name')}: {error}", file=sys.stderr)
            continue
        if record.get("etat_administratif") != "A":
            print(f"SKIP {entry.get('display_name')}: company is closed in the register", file=sys.stderr)
            continue
        card = build_card(entry, record)
        cards.append(card)
        revenue = next((s["value"] for s in card["stats"] if s["label"].startswith("Revenue")), "?")
        print(f"OK   {card['name']:<22} {card['city']:<22} founded {card['founded_year']}  revenue {revenue:<11} fit {card['fit_score']}")

    merged = merge_summaries(cards)
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "recherche-entreprises.api.gouv.fr (INSEE Sirene, INPI, annual accounts, ADEME)",
        "thesis": THESIS,
        "cards": cards,
    }
    (HERE / "cards.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote cards.json: {len(cards)} cards, {merged} with an AI summary.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
