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

## Live IBKR data

Built and verified locally as a four-service stack (`deploy/docker-compose.yml`):
Mongo, Gateway, collector, app. Run it before paying Railway anything:

```
cp deploy/.env.example deploy/.env   # fill in, then:
cd deploy && docker compose up -d --build
```

### What compose proves, and what it does not

Verified end to end in containers: Gateway logs into the paper account
unattended, the collector reaches it and subscribes ticks + depth, catch-up
backfill writes 1sec bars, FinViz fetches its own token and its i1 bars, the
rollups run, and the app renders a chart with the source mix in its header.

Live tick and depth **volume** can only be confirmed while the market is open —
IBKR sends nothing when it is shut, so a quiet overnight run is not a fault:

```
python scripts/check_stack.py --mongo mongodb://127.0.0.1:27018/
python scripts/check_depth_sources.py --ticker NVDA --port 4004
```

Compose is a good rehearsal but not a proof of Railway, because Railway does
not read this file — each service there is configured by hand — and its network
behaves differently (below).

### Mapping it onto Railway

Four services. Only the app gets a public domain; the rest talk over the
private network.

| Service | Source | Reaches |
|---|---|---|
| `mongo` | Railway's MongoDB | — |
| `gateway` | `deploy/Dockerfile.gateway` | — |
| `collector` | `deploy/Dockerfile.collector` | `MONGO_URI`, `IB_HOST=gateway` |
| `app` | root `Dockerfile` | `MONGO_URI` |

Things that differ from compose and will bite otherwise:

- **Railway's private network is IPv6-only.** Services resolve each other at
  `<name>.railway.internal`, and a listener bound to `0.0.0.0` is unreachable.
  The upstream Gateway image puts socat in front of the API on IPv4 only, which
  is why `deploy/Dockerfile.gateway` exists: it makes that one listener
  dual-stack, so the same image works in both places. Set `IB_HOST` to the
  Gateway service's private hostname.
- **The API port is not the port Gateway prints.** Gateway binds 4002 (paper) /
  4001 (live) to loopback; socat republishes on 4004 / 4003. Dial the socat one.
- **`EXISTING_SESSION_DETECTED_ACTION` must not be `manual`** (its default).
  IBKR allows one session per account, and the dialog it raises has no one to
  click it on a headless container: Gateway hangs at login and never opens the
  API port.
- **Memory.** Gateway is the heavy one — measured at ~640 MB, Java plus Xvfb.
  The others are ~140 MB (mongo), ~235 MB (app), ~75 MB (collector).
- **The filesystem is ephemeral.** Mongo therefore has to be Railway's managed
  service, not a container with a local volume. `finviz/api_keys.py` is
  regenerated on each boot from the credentials, so losing it costs one login.

### The account

- Use a **paper** account. A live login needs 2FA approval on a phone and
  Gateway restarts daily, so the feed would die every day. Paper logs in
  unattended and gets the identical real-time feed once *"Share real-time
  market data with paper trading account"* is enabled in Client Portal.
- **One IBKR session anywhere.** Market data is bound to a single session per
  account, and a paper account borrows the live account's entitlement, so a
  phone app or a second Gateway takes it: `reqTickByTickData` then fails with
  *error 10189* and historical requests with *error 162*, both worded as an
  IP-address problem. A cloud Gateway is by definition a different IP, so once
  deployed it will fight any local Gateway for the feed.
- Subscriptions on that account: US Securities Snapshot and Futures Value
  Bundle + US Equity and Options Add-On Streaming Bundle (both needed for
  `reqTickByTickData`), and NASDAQ TotalView-OpenView for depth.
