"""
Apartment availability scraper.

For each configured site, fetches its floorplans page, extracts every available
unit, and appends one observation row per unit per run to SQLite. Also stores
the raw HTML snapshot per run, so old data can be re-parsed later.

A site is read by a "provider" that understands one property-website platform.
Providers return units normalized to the observations columns, so the rest of
the pipeline doesn't care where the data came from. To support a new platform,
write a fetch function and add it to PROVIDERS.
"""
from __future__ import annotations

import gzip
import html
import json
import random
import re
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

from config import Site

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "monitor.db"
SNAPSHOT_DIR = ROOT / "snapshots"

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0",
]


class ScrapeError(Exception):
    pass


@dataclass
class Listing:
    body: str               # raw page, saved as the snapshot
    units: list[dict]       # normalized; keys match observations columns
    floorplans: list[dict]  # normalized; keys match floorplan_observations columns


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def http_get(url: str, referer: str | None = None) -> str:
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    }
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
    except urllib.error.HTTPError as e:
        raise ScrapeError(f"HTTP {e.code} from {url}") from None
    return body.decode("utf-8", errors="replace")


def to_int(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def to_bedrooms(v) -> int | None:
    """Bedroom count, with studios as 0."""
    if isinstance(v, str) and v.strip().lower() in ("s", "studio"):
        return 0
    return to_int(v)


def floor_from_apt(apt: str | None) -> int | None:
    if not apt:
        return None
    digits = re.sub(r"\D", "", apt)
    if len(digits) <= 2:
        return None
    return int(digits[: len(digits) - 2])


# --- Jonah Digital (jonahdigital.com) ---------------------------------------
#
# Current sites embed every unit and floorplan in one JSON blob on the
# floorplans page. Older ones served the same data from a separate
# listing-chunks endpoint, which is kept as a fallback.

JONAH_APP_DATA_RE = re.compile(
    r'<script[^>]*id="jd-fp-data-script-app"[^>]*>(.*?)</script>',
    re.DOTALL,
)
JONAH_INSTANCE_RE = re.compile(r"instance%3D([0-9a-f]{32})")
JONAH_CHUNKS_PATH = (
    "_fp-renderable/params%3Ainstance%3D{instance}%26action%3Drender"
    "%26type%3Dlisting-chunks/?forcecache=1"
)
JONAH_UNIT_CHUNK_RE = re.compile(
    r'<script\s+type="application/json"\s+data-chunk-key="unit-data"[^>]*>(.*?)</script>',
    re.DOTALL,
)
JONAH_FLOORPLAN_CHUNK_RE = re.compile(
    r'<script\s+type="application/json"\s+data-chunk-key="floorplan-data"[^>]*>(.*?)</script>',
    re.DOTALL,
)


def _json_arrays(pattern: re.Pattern, body: str) -> list[list]:
    arrays = []
    for raw in pattern.findall(body):
        try:
            arr = json.loads(html.unescape(raw))
        except json.JSONDecodeError:
            continue
        if isinstance(arr, list):
            arrays.append(arr)
    return arrays


def _jonah_unit(u: dict, page_url: str) -> dict:
    return {
        "slug": u.get("slug"),
        "apartment_number": u.get("apartment_number"),
        "floor": floor_from_apt(u.get("apartment_number")),
        "floorplan_id": to_int(u.get("floorplan_id")),
        "floorplan_title": u.get("floorplan_title"),
        "bedrooms": to_bedrooms(u.get("bedrooms")),
        "bathrooms": to_int(u.get("bathrooms")),
        "square_feet": to_int(u.get("square_feet")),
        "rent_min": to_int(u.get("rent_min")),
        "rent_max": to_int(u.get("rent_max")),
        "price_display": (u.get("price_display") or "").strip() or None,
        "available_unix": to_int(u.get("available_date")),
        "available_display": u.get("available_display"),
        "availability_count": to_int(u.get("availability_count")),
        "permalink": urljoin(page_url, u["permalink"]) if u.get("permalink") else None,
        "raw_json": json.dumps(u, separators=(",", ":")),
    }


def _jonah_floorplan(p: dict) -> dict:
    return {
        "floorplan_id": to_int(p.get("id")),
        "title": p.get("title"),
        "bedrooms": to_bedrooms(p.get("bedrooms")),
        "bathrooms": to_int(p.get("bathrooms")),
        "sqft_min": to_int(p.get("square_feet_min")) or to_int(p.get("min_square_feet")) or to_int(p.get("square_feet")),
        "sqft_max": to_int(p.get("square_feet_max")) or to_int(p.get("max_square_feet")),
        "rent_min": to_int(p.get("rent_min")) or to_int(p.get("min_rent")),
        "rent_max": to_int(p.get("rent_max")) or to_int(p.get("max_rent")),
        "available_units": to_int(p.get("available_units")) or to_int(p.get("availability_count")),
        "raw_json": json.dumps(p, separators=(",", ":")),
    }


def fetch_jonah(url: str) -> Listing:
    body = http_get(url)
    m = JONAH_APP_DATA_RE.search(body)
    app = None
    if m:
        try:
            app = json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    if isinstance(app, dict):
        units = app.get("units") or []
        plans = app.get("floorplans") or []
    else:
        m = JONAH_INSTANCE_RE.search(body)
        if not m:
            raise ScrapeError(
                "no jonah digital floorplan data on this page. either the site "
                "uses a different platform, or its layout has changed"
            )
        base = url if url.endswith("/") else url + "/"
        body = http_get(urljoin(base, JONAH_CHUNKS_PATH.format(instance=m.group(1))), referer=url)
        if not JONAH_UNIT_CHUNK_RE.search(body):
            raise ScrapeError("no unit data found; the site layout may have changed")
        seen: dict[str, dict] = {}
        for arr in _json_arrays(JONAH_UNIT_CHUNK_RE, body):
            for u in arr:
                if isinstance(u, dict) and u.get("slug"):
                    seen.setdefault(u["slug"], u)
        units = list(seen.values())
        plans = next(iter(_json_arrays(JONAH_FLOORPLAN_CHUNK_RE, body)), [])

    return Listing(
        body=body,
        units=[_jonah_unit(u, url) for u in units if isinstance(u, dict) and u.get("slug")],
        floorplans=[_jonah_floorplan(p) for p in plans if isinstance(p, dict)],
    )


PROVIDERS: dict[str, Callable[[str], Listing]] = {
    "jonah": fetch_jonah,
}


def fetch(site: Site) -> Listing:
    try:
        provider = PROVIDERS[site.provider]
    except KeyError:
        raise ScrapeError(
            f"unknown provider {site.provider!r} (known: {', '.join(PROVIDERS)})"
        ) from None
    return provider(site.url)


# --- Storage -----------------------------------------------------------------

def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
            site          TEXT NOT NULL,
            started_at    TEXT NOT NULL,
            finished_at   TEXT,
            n_units       INTEGER,
            error         TEXT,
            snapshot_path TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_runs_site ON runs(site);

        CREATE TABLE IF NOT EXISTS observations (
            obs_id             INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id             INTEGER NOT NULL REFERENCES runs(run_id),
            observed_at        TEXT NOT NULL,
            slug               TEXT NOT NULL,
            apartment_number   TEXT,
            floor              INTEGER,
            floorplan_id       INTEGER,
            floorplan_title    TEXT,
            bedrooms           INTEGER,
            bathrooms          INTEGER,
            square_feet        INTEGER,
            rent_min           INTEGER,
            rent_max           INTEGER,
            price_display      TEXT,
            available_unix     INTEGER,
            available_display  TEXT,
            availability_count INTEGER,
            permalink          TEXT,
            raw_json           TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_obs_run  ON observations(run_id);
        CREATE INDEX IF NOT EXISTS idx_obs_slug ON observations(slug);

        CREATE TABLE IF NOT EXISTS floorplan_observations (
            fp_obs_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id          INTEGER NOT NULL REFERENCES runs(run_id),
            observed_at     TEXT NOT NULL,
            floorplan_id    INTEGER,
            title           TEXT,
            bedrooms        INTEGER,
            bathrooms       INTEGER,
            sqft_min        INTEGER,
            sqft_max        INTEGER,
            rent_min        INTEGER,
            rent_max        INTEGER,
            available_units INTEGER,
            raw_json        TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_fp_run ON floorplan_observations(run_id);
        """
    )
    return conn


def _insert(conn: sqlite3.Connection, table: str, run_id: int, observed_at: str, rows: list[dict]) -> None:
    if not rows:
        return
    cols = list(rows[0])
    conn.executemany(
        f"INSERT INTO {table} (run_id, observed_at, {', '.join(cols)}) "
        f"VALUES (?, ?, {', '.join('?' * len(cols))})",
        [(run_id, observed_at, *(r[c] for c in cols)) for r in rows],
    )


def save_snapshot(site: Site, body: str, started_at: str) -> Path:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    site_slug = re.sub(r"[^a-z0-9]+", "-", site.name.lower()).strip("-")
    safe_ts = started_at.replace(":", "").replace("-", "")
    path = SNAPSHOT_DIR / f"{site_slug}_{safe_ts}.html.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(body)
    return path


def scrape_site(conn: sqlite3.Connection, site: Site) -> bool:
    """Scrape one site and record the run. Returns True on success."""
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    run_id = conn.execute(
        "INSERT INTO runs (site, started_at) VALUES (?, ?)", (site.name, started_at)
    ).lastrowid
    conn.commit()

    error = None
    n_units = 0
    snapshot_path: Path | None = None
    try:
        listing = fetch(site)
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _insert(conn, "observations", run_id, observed_at, listing.units)
        _insert(conn, "floorplan_observations", run_id, observed_at, listing.floorplans)
        snapshot_path = save_snapshot(site, listing.body, started_at)
        n_units = len(listing.units)
        n_matching = sum(1 for u in listing.units if site.matches(u))
        log(f"{site.name}: {n_units} units listed, {n_matching} match your filters")
    except Exception as e:
        error = str(e) if isinstance(e, ScrapeError) else repr(e)
        log(f"{site.name}: FAILED: {error}")
    finally:
        conn.execute(
            """
            UPDATE runs SET finished_at=?, n_units=?, error=?, snapshot_path=?
            WHERE run_id=?
            """,
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                n_units,
                error,
                str(snapshot_path) if snapshot_path else None,
                run_id,
            ),
        )
        conn.commit()

    return error is None
