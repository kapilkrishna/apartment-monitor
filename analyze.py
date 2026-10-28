"""Status report: every unit seen per site, plus every NEW/GONE/PRICE event."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from notify import fmt_money
from scrape import DB_PATH, connect


def _dash(v) -> str:
    return "—" if v is None else str(v)


def report_site(conn: sqlite3.Connection, site: str, bedrooms: int | None, since_minutes: int) -> None:
    runs = conn.execute(
        """
        SELECT COUNT(*) AS n,
               MIN(started_at) AS first_run,
               MAX(started_at) AS last_run,
               SUM(error IS NOT NULL) AS errors,
               (SELECT error FROM runs WHERE site=? AND error IS NOT NULL
                ORDER BY run_id DESC LIMIT 1) AS last_error
        FROM runs WHERE site=?
        """,
        (site, site),
    ).fetchone()
    print(f"== {site}")
    print(f"Checks: {runs['n']} ({runs['errors']} failed), {runs['first_run']} → {runs['last_run']}")
    if runs["last_error"]:
        print(f"  last failure: {runs['last_error']}")
    print()

    bed_filter = "" if bedrooms is None else "AND o.bedrooms = :bedrooms"
    params = {"site": site, "bedrooms": bedrooms}
    obs = f"""
        SELECT o.* FROM observations o JOIN runs r USING (run_id)
        WHERE r.site = :site {bed_filter}
    """

    units = conn.execute(
        f"""
        SELECT MAX(apartment_number)   AS apt,
               MAX(floor)              AS floor,
               MAX(bedrooms)           AS bed,
               MAX(square_feet)        AS sqft,
               MIN(rent_min)           AS rent_low,
               MAX(rent_max)           AS rent_premium,
               MIN(observed_at)        AS first_seen,
               MAX(observed_at)        AS last_seen,
               MAX(available_display)  AS avail,
               COUNT(*)                AS observations
        FROM ({obs})
        GROUP BY slug
        ORDER BY first_seen
        """,
        params,
    ).fetchall()

    label = "Units" if bedrooms is None else ("Studios" if bedrooms == 0 else f"{bedrooms}BR units")
    print(f"{label} ever seen:")
    print(f"  {'apt':<6} {'flr':>3} {'bed':>3} {'sqft':>5} {'rent_low':>9} {'rent_max':>9} "
          f"{'avail':<18} {'obs':>4} first→last")
    for u in units:
        print(
            f"  {_dash(u['apt']):<6} {_dash(u['floor']):>3} {_dash(u['bed']):>3} {_dash(u['sqft']):>5} "
            f"{fmt_money(u['rent_low']):>9} {fmt_money(u['rent_premium']):>9} "
            f"{(u['avail'] or '—'):<18} {u['observations']:>4} "
            f"{u['first_seen'][:16]} → {u['last_seen'][:16]}"
        )

    # NEW / PRICE: first sighting of a unit, or a rent change since its previous sighting.
    events = [
        (e["observed_at"], e)
        for e in conn.execute(
            f"""
            WITH ranked AS (
              SELECT *,
                     LAG(rent_min) OVER (PARTITION BY slug ORDER BY run_id) AS prev_min,
                     LAG(run_id)   OVER (PARTITION BY slug ORDER BY run_id) AS prev_run
              FROM ({obs})
            )
            SELECT * FROM ranked WHERE prev_run IS NULL OR rent_min != prev_min
            """,
            params,
        )
    ]

    # GONE: a unit in one successful check that's missing from the next.
    ok_runs = conn.execute(
        "SELECT run_id, started_at FROM runs WHERE site=? AND error IS NULL ORDER BY run_id", (site,)
    ).fetchall()
    by_run: dict[int, dict] = {}
    for r in conn.execute(obs, params):
        by_run.setdefault(r["run_id"], {})[r["slug"]] = r
    gone = []
    for prev, curr in zip(ok_runs, ok_runs[1:]):
        before, after = by_run.get(prev["run_id"], {}), by_run.get(curr["run_id"], {})
        for slug in set(before) - set(after):
            gone.append((curr["started_at"], before[slug]))

    if since_minutes:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=since_minutes)).isoformat(timespec="seconds")
        events = [e for e in events if e[0] >= cutoff]
        gone = [g for g in gone if g[0] >= cutoff]

    print()
    if not events and not gone:
        print("No events.")
        print()
        return
    suffix = f" in the last {since_minutes} min" if since_minutes else ""
    print(f"Events{suffix}:")
    lines = []
    for ts, e in events:
        if e["prev_run"] is None:
            text = f"NEW    #{e['apartment_number']} ({e['bedrooms']}BR) @ {fmt_money(e['rent_min'])}"
        else:
            delta = (e["rent_min"] or 0) - (e["prev_min"] or 0)
            sign = "+" if delta > 0 else ""
            text = (f"PRICE  #{e['apartment_number']} ({e['bedrooms']}BR): "
                    f"{fmt_money(e['prev_min'])} → {fmt_money(e['rent_min'])} ({sign}{delta:,})")
        lines.append((ts, text))
    for ts, u in gone:
        lines.append((ts, f"GONE   #{u['apartment_number']} ({u['bedrooms']}BR) (was {fmt_money(u['rent_min'])})"))
    for ts, text in sorted(lines):
        print(f"  {ts[:16]}  {text}")
    print()


def report(site: str | None = None, bedrooms: int | None = None, since_minutes: int = 0) -> int:
    if not DB_PATH.exists():
        print("No history yet. Run `python3 monitor.py` first.")
        return 1
    conn = connect()
    sites = [r["site"] for r in conn.execute("SELECT DISTINCT site FROM runs ORDER BY site")]
    if site:
        if site not in sites:
            print(f"No history for {site!r}. Sites with history: {', '.join(sites) or 'none'}")
            return 1
        sites = [site]
    for s in sites:
        report_site(conn, s, bedrooms, since_minutes)
    conn.close()
    return 0
