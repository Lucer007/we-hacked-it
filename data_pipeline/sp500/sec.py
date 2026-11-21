"""Small client for the SEC's free EDGAR APIs (no key needed).

The SEC asks automated tools to say who they are in the User-Agent header and to stay
under 10 requests per second. For real use, set SEC_USER_AGENT to
"<project name> <contact email>".
"""

import json
import os
import time
from pathlib import Path

import requests

USER_AGENT = os.environ.get("SEC_USER_AGENT", "DealdeckHackathonPrototype/0.1 (student project)")
CACHE_DIR = Path(__file__).resolve().parent / "cache"
_last_request = [0.0]


def _download(url: str) -> bytes:
    last_error = None
    for attempt in range(5):
        wait = 0.15 - (time.monotonic() - _last_request[0])  # about 7 requests per second
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.monotonic()
        try:
            response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
            if response.status_code == 404:
                raise FileNotFoundError(url)
            if response.status_code == 403 and "@" not in USER_AGENT:
                raise SystemExit(
                    f"The SEC refused {url}.\nwww.sec.gov requires a contact email in the User-Agent. "
                    'Set it, then run again:  SEC_USER_AGENT="Dealdeck you@example.com"')
            if response.status_code in (429, 500, 502, 503, 504):
                last_error = f"HTTP {response.status_code}"
                time.sleep(2 * (attempt + 1))
                continue
            response.raise_for_status()
            return response.content
        except requests.RequestException as error:
            last_error = error
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Could not download {url}: {last_error}")


def fetch(url: str, cache_name: str, refresh: bool = False) -> bytes:
    """Download a URL once and keep it in cache/."""
    path = CACHE_DIR / cache_name
    if path.exists() and not refresh:
        return path.read_bytes()
    data = _download(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def fetch_json(url: str, cache_name: str, refresh: bool = False):
    return json.loads(fetch(url, cache_name, refresh))
