# apartment-monitor

i'm [kapil](https://x.com/Kapil_Krishna18). i moved to sf this past summer and, like many others, soon figured out how cooked the housing market is. i was looking for a 2br and wanted to track every price change in a couple of buildings.

i also wanted to get paged (even at night) so i could grab an apartment the second it opened. i wanted to set requirements (2br, price under X, apartment Y) and have a script watch for them. i found myself refreshing the floorplans page all day and knew that wasn't going to work.

the price history turned out to be the interesting part. i fed it into another project to predict how likely prices were to drop or rise. think google flights, but for apartments. in the apartment world none of that data is tracked, and that only hurts renters.

> **heads up:** i'm thankfully done apartment hunting (for now), so i don't run this anymore. it last worked in october 2026. building websites change, so if it breaks, the fix is usually small. PRs welcome!

so i wrote a script that checks the building's website every 20 minutes and buzzes my phone when something changes:

```
The Example: 2BR
NEW #1204 (2BR, 812 sqft) @ $5,950, Available Now
  https://example.com/floorplans/unit-.../
PRICE #0907 (2BR, 790 sqft): $5,400 → $5,200 (-200)
```

this is that script, cleaned up so you can point it at your own buildings. it's about 900 lines of plain python with no dependencies. `config.toml` is where you say what you want, `scrape.py` reads the listings, and `notify.py` compares them with the last check and pings you. that's it.

## quick start

first, tell it what you're looking for. edit [`config.toml`](config.toml):

```toml
[defaults]
bedrooms = [1, 2]    # 0 = studio
max_rent = 5500
min_sqft = 600

[[sites]]
name = "The Example"
url = "https://example.com/floorplans/"
apartments = ["1204", "1304"]    # only these units

[[sites]]
name = "Another Building"
url = "https://example.org/floorplans/"
max_rent = 4800      # any filter can be set per building
```

then pick how you want to run it.

**i don't want to leave a computer on.** run it free on github actions:

1. fork this repo, and edit `config.toml` in your fork.
2. set up [notifications](#notifications) and add the key as a repo secret (**settings → secrets and variables → actions → new repository secret**).
3. in the **actions** tab, enable workflows, open **monitor**, and click **run workflow**.

it now checks every 20 minutes. the first check only records what's listed, so alerts start with the second.

**i have a mac that's usually awake.** you'll need python 3.11+ (`brew install python`; the one that ships with macos is too old).

```bash
git clone https://github.com/kapilkrishna/apartment-monitor.git
cd apartment-monitor
cp .env.example .env              # add a notification key
python3 monitor.py test-notify    # your phone should buzz
python3 monitor.py                # check once
```

to run it every 20 minutes:

```bash
mkdir -p logs
sed -e "s|__PYTHON__|$(command -v python3)|" -e "s|__DIR__|$PWD|g" \
  launchd/apartment-monitor.plist > ~/Library/LaunchAgents/local.apartment-monitor.plist
launchctl load ~/Library/LaunchAgents/local.apartment-monitor.plist
```

this is how i ran it. logs go to `logs/launchd.log`, and `launchctl unload` with the same path stops it. on linux, use cron: `*/20 * * * * cd /path/to/apartment-monitor && python3 monitor.py`.

## config

| setting      | what it does                                           | default |
| ------------ | ------------------------------------------------------ | ------- |
| `bedrooms`   | bedroom counts to alert on. `0` = studio               | any     |
| `apartments` | only these unit numbers, like `["1204", "1304"]`       | any     |
| `min_sqft`   | smallest unit you want                                 | none    |
| `max_sqft`   | largest unit you want                                  | none    |
| `max_rent`   | highest base rent you'd pay                            | none    |
| `alert_on`   | any of `"new"`, `"gone"` (leased or pulled), `"price"` | all     |

anything in `[defaults]` applies to every building, and any building can override it. if you make a typo, it tells you which line.

## notifications

set up at least one. you'll get alerts on every one that has a key.

**ntfy** (free, no account, what i'd start with). install the app ([ios](https://apps.apple.com/app/ntfy/id1625396347) / [android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)), tap **+**, and subscribe to a long random topic like `apartments-x7k2p9q4`. anyone who knows the name can read it, so don't use `apartments`. that name is your `NTFY_TOPIC`.

**pushover** ($5 once, after a 30-day trial). this is what i used. new listings are sent as high priority, which gets past quiet hours. your user key on the [dashboard](https://pushover.net) is `PUSHOVER_USER`, and the token from [creating an app](https://pushover.net/apps/build) is `PUSHOVER_TOKEN`.

**discord.** in a server you own: **server settings → integrations → webhooks → new webhook → copy webhook url**. that's `DISCORD_WEBHOOK_URL`.

## will it work for my building?

maybe! it works with buildings whose site runs on [jonah digital](https://www.jonahdigital.com)'s floorplans app, which a lot of apartment buildings use. find out in a few seconds:

```
$ python3 monitor.py try https://example.com/floorplans/
works! 27 units listed right now:

  #0412 (studio, 480 sqft)        $3,650  Available Now
  #0715 (1BR, 610 sqft)           $4,300  Available Now
  ...
```

no terminal? open the building's floorplans page, view source, and search for `jd-fp-data-script-app`. if it's there, you're good.

if not, it needs a new provider: one function in `scrape.py` that takes a url and returns the units. the data is usually a json blob in the page source, or an api call you can spot in your browser's network tab. copy `fetch_jonah`, add yours to `PROVIDERS`, and set `provider = "yours"` on the building. i'd love PRs for rentcafe, entrata, or sightmap sites.

## history

every check is saved to `monitor.db`, so after a while you can see how long units sit and how prices move.

```bash
python3 monitor.py report                         # every building
python3 monitor.py report --site "The Example" --bedrooms 2
python3 monitor.py report --since-minutes 1440    # just the last day
```

each check also keeps a gzipped copy of the page in `snapshots/`, so you can re-parse old data if a site changes under you.

## if it breaks

`python3 monitor.py report` shows the last error. on github, open the failed run in the actions tab. most breakages are a site redesign, and the fix lives in that site's provider in `scrape.py`.

## todos

- providers for rentcafe, entrata, and sightmap
- email alerts
- alert on move-in date, not just price and size

## license

MIT
