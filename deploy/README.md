# Deploying the dashboard (Railway)

This deploys the **dashboard only**, serving the bundled NVDA demo day
(2026-07-22) out of a MongoDB running next to it. Anyone with the URL sees a
populated chart with no account, no IB Gateway and no local install.

Live IBKR collection is a **separate, much heavier** thing — see
[Adding live IBKR data](#adding-live-ibkr-data) at the bottom.

## What is in the image

`Dockerfile` installs `requirements-deploy.txt` (the short dashboard-only list
plus gunicorn) and runs `deploy/railway_start.py`, which:

1. waits for MongoDB (Railway starts services in parallel, so on a cold deploy
   the database is routinely not up yet);
2. loads `demo_data/` **if the candles collection is empty**, so a fresh volume
   is never left serving a blank chart and a restart costs nothing;
3. `exec`s gunicorn, which becomes PID 1 and therefore receives the platform's
   SIGTERM directly.

## Steps

1. **Create the project.** railway.app → New Project → Deploy from GitHub repo →
   pick `tradingCVDBubble`. Railway reads `railway.json` and builds the
   `Dockerfile`.

2. **Add a MongoDB service.** In the same project: New → Database → MongoDB.

3. **Wire the app to it.** On the app service, Variables → add:

   | Variable | Value |
   |---|---|
   | `MONGO_URI` | the MongoDB service's connection string (Railway offers it as a reference variable — use that rather than pasting, so it follows the database if it moves) |

   `PORT` is injected by Railway; `HOST`, `DASH_DEBUG` and the UTF-8 settings
   are already baked into the image.

4. **Generate a domain.** App service → Settings → Networking → Generate Domain.
   That URL is the deliverable.

5. **Check the deploy log** for `[start] demo dataset loaded` followed by
   `Listening at: http://0.0.0.0:<port>`.

Then open the URL and, **in this order** (step 3 is not optional — the demo
dataset is a fixed past day, so the default live view is legitimately empty):

1. **Search Ticker** → `NVDA` → Enter
2. **Jump to (ET)** → `2026-07-22 10:00` → **Jump**
3. **Active Timeframe** → `1min`
4. **L2 Depth** → `20 levels` for the order-book heatmap

The chart title should read `NVDA — 1min (Source: ibkr_tick 84% + finviz 16%)`.

## FinViz on the deployment

The image deliberately does **not** contain `finviz/api_keys.py` — that file
holds a live Elite token, and anything copied into an image is readable by
anyone who can pull it. Without credentials the dashboard still draws the demo
day correctly (its volume was already scaled when the data was collected), but
it shows the red `FinViz unavailable` banner.

To clear the banner and let the deployment fetch fresh bars, add the credentials
as Railway variables — **type them into Railway's own UI**, never into the repo:

| Variable | Value |
|---|---|
| `FINVIZ_USERNAME` | the FinViz Elite account e-mail |
| `FINVIZ_PASSWORD` | its password |

The app logs in on the first fetch, writes the token inside the container and
retries — the same path that already handles an expired token.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017/` | Database to read and write |
| `PORT` | `8050` | Injected by Railway |
| `HOST` | `0.0.0.0` (in image) | Bind address |
| `DASH_DEBUG` | `0` (in image) | **Keep 0.** Debug serves the Werkzeug console, which executes arbitrary Python for anyone who loads the page |
| `DEMO_SEED` | `1` | `0` skips the demo load (use once real data is being collected) |
| `MONGO_WAIT_SECONDS` | `90` | How long to wait for the database before giving up |

## Adding live IBKR data

Not done here, and not a small addition. It needs:

- a **fourth service** running IB Gateway headless (Java + Xvfb + IBC, e.g. the
  `gnzsnz/ib-gateway` image), ~1 GB RAM on its own;
- the collector as a **fifth** service (`python -m ibkr.dynamic_collector`),
  which also means the full `requirements.txt` rather than the short list;
- `ibkr/*.py` currently dials `127.0.0.1`, which is the collector's own
  container in a cloud deploy — the host has to become configurable first;
- a **paper** account for the Gateway login. A live account needs 2FA approval
  on a phone, and Gateway restarts daily, so the feed would die every day.
  Paper logs in unattended and receives the identical real-time feed once
  *"Share real-time market data with paper account"* is enabled in Client Portal;

- **only one IBKR session anywhere.** Market data is bound to a single session
  per account, and a paper account borrows the live account's entitlement, so a
  phone app or a second Gateway logged in elsewhere takes it: `reqTickByTickData`
  then fails with *error 10189, "Trading TWS session is connected from a
  different IP address"* and historical requests with *error 162*. A cloud
  Gateway is by definition a different IP, so once this is deployed a local
  Gateway will fight it for the feed;
- the market-data subscriptions on that account: US Securities Snapshot and
  Futures Value Bundle + US Equity and Options Add-On Streaming Bundle (both
  needed for `reqTickByTickData`) and NASDAQ TotalView-OpenView for depth.
