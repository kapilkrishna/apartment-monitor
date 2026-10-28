"""
Load and validate config.toml: which sites to watch and what to alert on.

Secrets (notification keys) never go in this file; they come from the
environment or .env. See notify.py.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.toml"

ALERT_KINDS = ("new", "gone", "price")
FILTER_KEYS = {"bedrooms", "apartments", "min_sqft", "max_sqft", "max_rent", "alert_on"}
SITE_KEYS = FILTER_KEYS | {"name", "url", "provider"}


class ConfigError(Exception):
    pass


@dataclass
class Site:
    name: str
    url: str
    provider: str = "jonah"
    # None = any bedroom count. 0 = studio.
    bedrooms: set[int] | None = None
    # None = any unit. Otherwise apartment numbers, without leading zeros.
    apartments: set[str] | None = None
    min_sqft: int | None = None
    max_sqft: int | None = None
    max_rent: int | None = None
    alert_on: set[str] = field(default_factory=lambda: set(ALERT_KINDS))

    def matches(self, unit) -> bool:
        """Whether a unit (dict or sqlite3.Row from observations) passes this site's filters."""
        if self.bedrooms is not None and unit["bedrooms"] not in self.bedrooms:
            return False
        if self.apartments is not None and _apt_key(unit["apartment_number"]) not in self.apartments:
            return False
        sqft, rent = unit["square_feet"], unit["rent_min"]
        if self.min_sqft is not None and (sqft is None or sqft < self.min_sqft):
            return False
        if self.max_sqft is not None and (sqft is None or sqft > self.max_sqft):
            return False
        if self.max_rent is not None and (rent is None or rent > self.max_rent):
            return False
        return True

    def describe_filters(self) -> str:
        parts = []
        if self.bedrooms is not None:
            beds = sorted(self.bedrooms)
            parts.append("/".join("studio" if b == 0 else f"{b}BR" for b in beds))
        if self.apartments is not None:
            parts.append(", ".join(f"#{a}" for a in sorted(self.apartments)))
        if self.min_sqft is not None:
            parts.append(f">= {self.min_sqft} sqft")
        if self.max_sqft is not None:
            parts.append(f"<= {self.max_sqft} sqft")
        if self.max_rent is not None:
            parts.append(f"<= ${self.max_rent:,}")
        return ", ".join(parts) or "any unit"


def _apt_key(apt) -> str:
    """Compare apartment numbers loosely, so "201" in config matches "0201" on the site."""
    return str(apt or "").strip().lstrip("0").upper()


def _int_or_none(where: str, key: str, value) -> int | None:
    if value is None or value == 0:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ConfigError(f"{where}: {key} must be a whole number, got {value!r}")
    return value


def _parse_site(where: str, raw: dict, defaults: dict) -> Site:
    unknown = set(raw) - SITE_KEYS
    if unknown:
        raise ConfigError(f"{where}: unknown setting(s) {', '.join(sorted(unknown))}")
    merged = {**defaults, **raw}

    name, url = merged.get("name"), merged.get("url")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{where}: every site needs a name")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ConfigError(f"{where}: url must start with http:// or https://")

    bedrooms = merged.get("bedrooms")
    if isinstance(bedrooms, int) and not isinstance(bedrooms, bool):
        bedrooms = [bedrooms]
    if bedrooms is not None and (
        not isinstance(bedrooms, list) or not all(isinstance(b, int) for b in bedrooms)
    ):
        raise ConfigError(f"{where}: bedrooms must be a list of numbers, like [1, 2]")

    apartments = merged.get("apartments")
    if apartments is not None and (
        not isinstance(apartments, list) or not all(isinstance(a, (str, int)) for a in apartments)
    ):
        raise ConfigError(f'{where}: apartments must be a list, like ["1204", "1304"]')

    alert_on = merged.get("alert_on", list(ALERT_KINDS))
    bad = set(alert_on) - set(ALERT_KINDS)
    if bad:
        raise ConfigError(f"{where}: alert_on can only contain {', '.join(ALERT_KINDS)}")

    return Site(
        name=name.strip(),
        url=url,
        provider=merged.get("provider", "jonah"),
        bedrooms=set(bedrooms) if bedrooms else None,
        apartments={_apt_key(a) for a in apartments} if apartments else None,
        min_sqft=_int_or_none(where, "min_sqft", merged.get("min_sqft")),
        max_sqft=_int_or_none(where, "max_sqft", merged.get("max_sqft")),
        max_rent=_int_or_none(where, "max_rent", merged.get("max_rent")),
        alert_on=set(alert_on),
    )


def load(path: Path = CONFIG_PATH) -> list[Site]:
    if not path.exists():
        raise ConfigError(f"{path.name} not found. Copy the example from the README.")
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path.name} isn't valid TOML: {e}") from None

    defaults = data.get("defaults", {})
    unknown = set(defaults) - FILTER_KEYS
    if unknown:
        raise ConfigError(f"[defaults]: unknown setting(s) {', '.join(sorted(unknown))}")

    raw_sites = data.get("sites", [])
    if not raw_sites:
        raise ConfigError(f"{path.name} has no [[sites]]. Add at least one.")

    sites = [_parse_site(f"site #{i}", raw, defaults) for i, raw in enumerate(raw_sites, 1)]
    names = [s.name for s in sites]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ConfigError(f"site names must be unique: {', '.join(sorted(dupes))}")
    return sites
