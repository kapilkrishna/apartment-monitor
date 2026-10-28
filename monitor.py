#!/usr/bin/env python3
"""
Apartment availability monitor.

  python3 monitor.py              check every site in config.toml and send alerts
  python3 monitor.py try URL      check whether a building's website is supported
  python3 monitor.py test-notify  send a test alert to every configured channel
  python3 monitor.py report       print history and events for your sites

This is the script to schedule (launchd, cron, or GitHub Actions). Notification
keys come from the environment, or from a .env file next to this script.
"""
from __future__ import annotations

import argparse
import os
import random
import signal
import socket
import sys
import time
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit(
        f"This needs Python 3.11 or newer (you have {sys.version.split()[0]}).\n"
        "On a Mac: brew install python  (then use python3 from Homebrew)"
    )

ROOT = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    """Minimal KEY=value loader so launchd/cron don't need a wrapper script."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def _watchdog(signum, frame):
    sys.stderr.write("watchdog: run took over 10 minutes, aborting\n")
    sys.exit(2)


def cmd_run(args) -> int:
    import config
    import notify
    import scrape

    # Hard wall-clock cap so a stuck socket after macOS sleep/wake can't hang
    # launchd forever. urllib's timeout= doesn't fire reliably on stale sockets.
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _watchdog)
        signal.alarm(600)
    socket.setdefaulttimeout(30)

    sites = config.load()
    if not notify.configured_channels():
        print("[monitor] no notification keys set, so changes will only print here", flush=True)

    # Random delay so a fixed schedule doesn't hit the site on the dot.
    if not args.no_jitter and os.environ.get("MONITOR_NO_JITTER") != "1":
        time.sleep(random.randint(0, 180))

    conn = scrape.connect()
    failures = 0
    for site in sites:
        if scrape.scrape_site(conn, site):
            notify.notify_site(conn, site)
        else:
            failures += 1
    conn.close()
    return 1 if failures else 0


def cmd_try(args) -> int:
    import notify
    import scrape
    from config import Site

    site = Site(name="test", url=args.url, provider=args.provider)
    try:
        listing = scrape.fetch(site)
    except Exception as e:
        print(f"not supported yet: {e}")
        print("\nonly jonah digital sites work out of the box. the readme's \"will it "
              "work for my building?\" section covers adding one.")
        return 1

    units = sorted(listing.units, key=lambda u: (u["bedrooms"] is None, u["bedrooms"] or 0, u["rent_min"] or 0))
    print(f"works! {len(units)} units listed right now:\n")
    for u in units:
        avail = u["available_display"] or ""
        print(f"  {notify.fmt_unit(u):<28} {notify.fmt_money(u['rent_min']):>8}  {avail}")
    print("\nadd it to config.toml as a new [[sites]] block with this url.")
    return 0


def cmd_test_notify(args) -> int:
    import notify

    sent = notify.send(
        "apartment-monitor",
        "if you can read this, you're all set. good luck with the hunt.",
        urgent=False,
    )
    if sent:
        print(f"sent to {', '.join(sent)}")
        return 0
    if notify.configured_channels():
        print("every channel failed. see the errors above.")
    else:
        print("no notification keys set. add one to .env (see .env.example).")
    return 1


def cmd_report(args) -> int:
    import analyze

    return analyze.report(site=args.site, bedrooms=args.bedrooms, since_minutes=args.since_minutes)


def main() -> int:
    load_dotenv(ROOT / ".env")

    p = argparse.ArgumentParser(description="Get alerts when apartments open up.")
    sub = p.add_subparsers(dest="command")

    run = sub.add_parser("run", help="check every site and send alerts (the default)")
    run.add_argument("--no-jitter", action="store_true", help="skip the random start delay")
    run.set_defaults(func=cmd_run)

    t = sub.add_parser("try", help="check whether a building's website is supported")
    t.add_argument("url")
    t.add_argument("--provider", default="jonah")
    t.set_defaults(func=cmd_try)

    sub.add_parser("test-notify", help="send a test alert").set_defaults(func=cmd_test_notify)

    r = sub.add_parser("report", help="print history and events")
    r.add_argument("--site", help="only this site (by name)")
    r.add_argument("--bedrooms", type=int, help="only this bedroom count (0 = studio)")
    r.add_argument("--since-minutes", type=int, default=0, help="only events in the last N minutes")
    r.set_defaults(func=cmd_report)

    # No subcommand means `run`, so `monitor.py --no-jitter` works too.
    argv = sys.argv[1:]
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv = ["run", *argv]
    args = p.parse_args(argv)

    try:
        return args.func(args)
    except Exception as e:
        from config import ConfigError
        if isinstance(e, ConfigError):
            print(f"config.toml problem: {e}", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    sys.exit(main())
