"""
Diff each site's latest scrape against the previous one and send alerts for
units that match the site's filters.

Alerts go to every channel that has its keys set in the environment (or .env):
  ntfy:     NTFY_TOPIC (and optionally NTFY_SERVER, default https://ntfy.sh)
  Pushover: PUSHOVER_USER and PUSHOVER_TOKEN
  Discord:  DISCORD_WEBHOOK_URL
"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.parse
import urllib.request

from config import Site

USER_AGENT = "apartment-monitor (+https://github.com/kapilkrishna/apartment-monitor)"


def _post(url: str, data: bytes, content_type: str) -> None:
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": content_type, "User-Agent": USER_AGENT}
    )
    urllib.request.urlopen(req, timeout=15).close()


def send_ntfy(title: str, message: str, urgent: bool) -> None:
    server = os.environ.get("NTFY_SERVER") or "https://ntfy.sh"
    payload = {
        "topic": os.environ["NTFY_TOPIC"],
        "title": title,
        "message": message,
        "priority": 4 if urgent else 3,
        "tags": ["house"],
    }
    _post(server.rstrip("/"), json.dumps(payload).encode(), "application/json")


def send_pushover(title: str, message: str, urgent: bool) -> None:
    data = urllib.parse.urlencode({
        "token": os.environ["PUSHOVER_TOKEN"],
        "user": os.environ["PUSHOVER_USER"],
        "title": title,
        "message": message,
        # 1 = high priority, which gets past quiet hours.
        "priority": 1 if urgent else 0,
    }).encode()
    _post("https://api.pushover.net/1/messages.json", data, "application/x-www-form-urlencoded")


def send_discord(title: str, message: str, urgent: bool) -> None:
    payload = {"content": f"**{title}**\n{message}"[:2000]}
    _post(os.environ["DISCORD_WEBHOOK_URL"], json.dumps(payload).encode(), "application/json")


# (name, required env vars, sender)
CHANNELS = [
    ("ntfy", ("NTFY_TOPIC",), send_ntfy),
    ("Pushover", ("PUSHOVER_USER", "PUSHOVER_TOKEN"), send_pushover),
    ("Discord", ("DISCORD_WEBHOOK_URL",), send_discord),
]


def configured_channels() -> list[str]:
    return [name for name, keys, _ in CHANNELS if all(os.environ.get(k) for k in keys)]


def send(title: str, message: str, urgent: bool = False) -> list[str]:
    """Send to every configured channel. Returns the names it was sent to."""
    sent = []
    for name, keys, sender in CHANNELS:
        if not all(os.environ.get(k) for k in keys):
            continue
        try:
            sender(title, message, urgent)
            sent.append(name)
        except Exception as e:
            print(f"[notify] {name} failed: {e!r}", flush=True)
    return sent


def fmt_money(v) -> str:
    return f"${int(v):,}" if v else "—"


def fmt_unit(u) -> str:
    beds = "studio" if u["bedrooms"] == 0 else f"{u['bedrooms']}BR" if u["bedrooms"] is not None else "?BR"
    sqft = f", {u['square_feet']} sqft" if u["square_feet"] else ""
    return f"#{u['apartment_number'] or '?'} ({beds}{sqft})"


def changes(conn: sqlite3.Connection, site: Site) -> tuple[list[str], bool]:
    """Alert lines for this site's last two successful runs, and whether any is NEW."""
    runs = conn.execute(
        "SELECT run_id FROM runs WHERE site=? AND error IS NULL ORDER BY run_id DESC LIMIT 2",
        (site.name,),
    ).fetchall()
    if len(runs) < 2:
        return [], False

    def matching(run_id: int) -> dict:
        rows = conn.execute("SELECT * FROM observations WHERE run_id=?", (run_id,))
        return {r["slug"]: r for r in rows if site.matches(r)}

    curr, prev = matching(runs[0]["run_id"]), matching(runs[1]["run_id"])
    lines = []
    has_new = False

    if "new" in site.alert_on:
        for slug in sorted(set(curr) - set(prev)):
            u = curr[slug]
            avail = u["available_display"] or "now"
            lines.append(f"NEW {fmt_unit(u)} @ {fmt_money(u['rent_min'])}, {avail}")
            if u["permalink"]:
                lines.append(f"  {u['permalink']}")
            has_new = True

    if "gone" in site.alert_on:
        for slug in sorted(set(prev) - set(curr)):
            u = prev[slug]
            lines.append(f"GONE {fmt_unit(u)} (was {fmt_money(u['rent_min'])})")

    if "price" in site.alert_on:
        for slug in sorted(set(curr) & set(prev)):
            c, p = curr[slug], prev[slug]
            if c["rent_min"] != p["rent_min"]:
                delta = (c["rent_min"] or 0) - (p["rent_min"] or 0)
                sign = "+" if delta > 0 else ""
                lines.append(
                    f"PRICE {fmt_unit(c)}: "
                    f"{fmt_money(p['rent_min'])} → {fmt_money(c['rent_min'])} ({sign}{delta:,})"
                )

    return lines, has_new


def notify_site(conn: sqlite3.Connection, site: Site) -> None:
    lines, has_new = changes(conn, site)
    if not lines:
        return
    title = f"{site.name}: {site.describe_filters()}"
    body = "\n".join(lines)
    # NEW gets high priority; GONE/PRICE are normal.
    sent = send(title, body, urgent=has_new)
    where = ", ".join(sent) if sent else "nowhere (no notification keys set)"
    print(f"[notify] {site.name} → {where}:\n{body}", flush=True)
