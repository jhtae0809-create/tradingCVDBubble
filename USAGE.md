# CVD Bubble Dashboard — Reading Guide

A short guide to every visual on the chart: what each color means, how the data
sources are merged and marked, how the CVD lines are computed, and how to use
the Level-2 depth, the volume pies, and the "Jump to" time navigation.

The dashboard runs at `http://127.0.0.1:8050` (start it with `./start_all.sh`).
For installing it and for setup problems, see **[README.md](README.md)** —
this guide assumes the app is already up.

> **The chart draws only what is in MongoDB.** It has no data of its own. An
> empty or near-empty chart usually means the database has nothing for that
> ticker and time range, not that something crashed. See
> [§9 When the chart looks wrong or empty](#9-when-the-chart-looks-wrong-or-empty).

---

## 1. Quick start

1. **Search Ticker** — type a symbol (e.g. `NVDA`) in the top-left box and press
   Enter.
   - If the symbol is not recognized anywhere (FinViz or IBKR), the chart says
     **"'XYZ' is not a recognized ticker"** instead of spinning forever.
   - A valid but brand-new 1-second ticker shows **"Fetching … backfill in
     progress"** while its history is pulled in the background, then fills in.
2. **Base Data Source** — the radio toggle picks the base feed:
   - **`Tiered: IBKR ticks + history (default)`** → true trade-by-trade data
     where it exists (1-second and up), transparently falling back to coarser
     stored history further back. This is the one you normally want.
   - **`1-Min Base (FinViz legacy)`** → 1-minute consolidated bars and up, the
     pre-tick path. Requires a FinViz Elite token; without one this source has
     nothing to show.
3. **Active Timeframe** — the dropdown (1sec, 5sec, 1min, 5min, 1hr, 1day, …).
   The list adapts to the chosen source, and is **empty until a ticker with data
   is loaded**.

The chart has **three stacked panels** sharing one time axis:
- **Top:** price candles (+ bubbles, + L2 depth, + volume pies).
- **Middle & bottom:** the indicator panels (buy/sell volume bars, CVD lines,
  cumulative lines).

---

## 2. Data sources — how they are merged and marked

We stitch the best available feed for each time range into one continuous
chart. Because the feeds differ in quality, the **background shading tells you
how trustworthy the buy/sell split is** in each region:

| Background | Meaning | Buy/Sell quality |
|---|---|---|
| **No shading** | Real tick data (`ibkr_tick`) | **Ground truth** — every trade classified by the actual aggressor side |
| **Yellow / gold tint** | Estimated (`BVC` or `wick`) | Estimate from bar shape only — no tick detail |
| **Blue tint** | IBKR historical backfill / **mixed** | Partly estimated; a coarse bar mixing real ticks and estimated fill |

A small label at the left edge of each shaded run names the estimator
(`BVC (est.)`, `Wick (est.)`, `IBKR hist (est.)`, `Mixed (part est.)`).

**Rule of thumb: unshaded = real, shaded = estimated.**

### What happens when there is no tick data?

Without trade-by-trade ticks we cannot know the true aggressor, so we
**estimate** the buy/sell split from the bar itself using **BVC (Bulk Volume
Classification** — Easley, López de Prado & O'Hara, 2012):

```
buy_volume = Volume × Φ(ΔP / σ)
```

where `ΔP` is the close-to-close price change, `σ` is a rolling std of `ΔP`, and
`Φ` is the standard-normal CDF. In words: the more a bar closed up relative to
its recent volatility, the larger the share of its volume counted as buying.
This is the standard estimator for "bars only" data and is what the **default
CVD line** falls back to over any non-tick region.

> Closing-auction bars (the single Market-On-Close cross at ~15:59) are
> **neutralized** (buy = sell, delta = 0) and drawn as **gray** volume bars,
> because a single-priced auction has no meaningful direction. This keeps one
> giant print from dominating the whole CVD curve.

---

## 3. The three CVD lines (middle panel)

CVD = **Cumulative Volume Delta** = running sum of (buy − sell) volume. Three
versions are plotted so they can be compared; toggle each in the legend:

| Line | Color / style | How it is computed |
|---|---|---|
| **CVD (all-time)** | purple, solid | The **primary** CVD. Uses **real tick delta** where we have ticks, and **BVC** where only FinViz bars exist. This is the "best available per region" curve. |
| **CVD (BVC est.)** | cyan, dashed | **BVC everywhere** (probabilistic, from price change / volatility). Consistent method across the whole chart. |
| **CVD (wick est.)** | pink, dash-dot | **Pure wick decomposition everywhere** — buy/sell inferred from each candle's wick lengths, summed from the finest bars up. |

They agree in tick regions and diverge in estimated regions — the gap between
them is a visual measure of estimation uncertainty.

Also on this panel: **Cum Total / Cum Buy / Cum Sell** (cumulative volume lines)
and **Buy Ratio**.

---

## 4. Buy / Sell volume bars (indicator panel)

Per-bar buy volume points **up (teal)**, sell volume points **down (red)**.
Three estimation methods are available — **tick**, **wick**, and **BVC** — each
computed as `buy = (V + delta)/2`, `sell = (V − delta)/2` from that method's
delta. Each method is one legend entry that toggles its buy and sell together.

---

## 5. Z-Score Volume Bubbles (on the price panel)

A bubble marks a **statistically unusual bar**: it appears when the bar's
**delta z-score ≥ 2** OR its **volume z-score ≥ 3** (z-score = how many standard
deviations above the recent mean). Bubble **size grows with the z-score**; the
label shows the signed delta (`+3.2M`) or the total volume.

**Bubble colors:**

| Color | Meaning |
|---|---|
| 🟢 **Green** | Strong net **BUYING** (delta z ≥ 2, delta > 0) |
| 🔴 **Crimson** | Strong net **SELLING** (delta z ≥ 2, delta < 0) |
| 🟣 **Purple** | **Absorption** — a large delta (z ≥ 2) that barely moved price (candle body < 0.6× its average). Big pressure met by an opposing wall. |
| 🟡 **Gold** | **Volume spike** (volume z ≥ 3) without an extreme delta — heavy two-sided activity. |

Toggle all bubbles with the **Bubbles** button.

---

## 6. Volume Pies (on the price panel)

When **Pies** are on, the visible window is divided into equal slices and each
slice gets a pie showing its **Buy (teal) vs Sell (red)** volume split — a
quick left-to-right read of who dominated each segment. Hover a pie for the
exact buy/sell numbers and the time range it covers. The strip auto-sizes as you
zoom, so each pie always covers a whole number of bars.

---

## 7. Level-2 (L2) Depth (on the price panel)

The **L2 Depth** selector overlays the resting order book as a Bookmap-style
heatmap behind the candles, and lets you choose how deep to look:

- **Off** — no heatmap.
- **10 levels** — the 10 price levels nearest the current price on each side
  (the tight, near-touch book).
- **20 levels** — 20 each side (a wider view).
- **Full book** — every level the collector holds within the ±5% display band.

Pick a shallower depth for a clean near-the-money read, or **Full book** for the
complete Bookmap-style picture. The heatmap:

- **Heatmap color = resting size** at each price: transparent/dark = thin,
  blue → cyan = ordinary depth, **yellow → white = the big walls** (top ~2% of
  resting size). Hover any cell for `price · size`.
- **Support / Resistance lines** — dashed horizontal lines mark persistent
  resting liquidity: **blue = support**, **amber = resistance**. The right-edge
  label shows the wall's average size (e.g. `R 205.58 · 1.2M`). Thicker/brighter
  lines = stronger, more persistent walls.

The heatmap requests **20 book levels per side** from IBKR (up from 10), so the
depth band now spans roughly ±5% of price — a deeper, more Bookmap-like view.
IBKR's SMART depth caps how many levels it actually returns; the collector log
line `[col] subscribed depth …` and the snapshot documents show the real count.
The displayed price band is capped at ±5% by `L2_BAND` (tunable via the
`L2_BAND` environment variable) so a lone far-away limit order can't stretch the
axis and squash the candles.

### Why the two S&R lines are critical to the trade approach

The heatmap shows the *whole* resting book, but the **two lines distill it to the
two prices that actually matter** — the largest, most persistent bid wall below
(**support**) and ask wall above (**resistance**). They are where the biggest
passive liquidity is parked, and they matter because **that is where aggressive
flow meets a wall**:

- **They are the battlegrounds.** A resistance wall is a large block of resting
  sell limit orders; for price to rise through it, aggressive buyers must *absorb*
  the entire wall. A support wall is the mirror image for a fall. Price tends to
  **stall or reverse at these levels** and to **accelerate once one breaks**.
- **They tell you *where*; CVD tells you *how hard*.** This is the core of the
  approach: read the two together.
  - CVD climbing (aggressive buying) **into a resistance wall that holds** →
    buyers are being **absorbed**; the wall is winning → likely **rejection**.
    (This is exactly what a 🟣 purple *absorption* bubble flags — big delta, no
    price move — and it usually prints right at one of these lines.)
  - CVD climbing into a resistance wall and the wall **shrinks / disappears** on
    the heatmap → the wall is being eaten → a **breakout**.
- **They are the decision points.** Entries, exits and stops are placed relative
  to these two levels: buy the hold at support, take profit into resistance, and
  treat a decisive break (confirmed by CVD) as the signal to stay in for the
  continuation. Everything else on the book is context; **these two lines are the
  trigger levels.**

In short: the heatmap is the terrain, and the two lines are the front lines where
the fight between aggressive orders (CVD) and passive liquidity (the book) is
decided — which is why we surface them explicitly instead of leaving them buried
in the heatmap.

L2 is depth-only and available for the actively collected tickers. It also works
in **Jump-to** mode — jump to a past time and the book at that time is shown.

---

## 8. "Jump to (ET)" — go to a specific time

Type a date/time in the **Jump to (ET)** box and click **Jump**:

- `2026-07-22 19:50` → centers the **19:50 bar** of 2026-07-22 on screen.
- `2026-07-22` → jumps to that day.
- Times are **US Eastern (ET)**, matching how the data is stored.

The dashboard loads a window of history around that instant and centers the
exact bar you typed. Click **Live** to snap back to the live tail (most recent
bars, auto-updating).

---

## 9. Other controls

- **Bubbles** — show/hide the Z-Score bubbles.
- **Pies** — show/hide the volume pie strip.
- **L2 Depth** — choose the order-book heatmap depth (Off / 10 / 20 / Full book);
  the S&R lines follow the chosen depth.
- **Y Auto-Scale** — ON: the price axis auto-fits as you pan. OFF: your manual
  y-zoom sticks.
- **Manual Refresh** — force a data reload without waiting for the poll.

---

## 10. When the chart looks wrong or empty

Most "it's broken" moments are one of these, and most are expected behaviour
rather than faults. Setup and install problems are in
**[README.md → When it doesn't work](README.md#when-it-doesnt-work)**.

### Nothing renders at all

| What you see | What it means |
|---|---|
| Blank chart, **no message**, empty Timeframe dropdown | The database has nothing for this ticker. Load the bundled dataset (`python -m scripts.demo_dataset load`) or run the collector and search the ticker. |
| **"Fetching … backfill in progress"** (orange) | Normal for a brand-new 1-second ticker; the IBKR backfill takes up to ~1 minute. If it never resolves, the collector is not running or IB Gateway is not connected. |
| **"'XYZ' is not a recognized ticker"** (red) | FinViz does not list the symbol. A symbol that exists on IBKR but not on FinViz can be misflagged this way. |
| **"⚠ Error fetching …"** (red) | A real exception during fetch. Check `logs_app.log`. A missing FinViz token no longer produces this — it is skipped silently. |
| Chart never updates after restarting the app | Stale Dash callback spec — hard-refresh the browser. |

### It renders, but the data looks wrong

| What you see | What it means |
|---|---|
| Large **shaded (yellow / blue) regions** | Not an error — those bars have no tick detail, so buy/sell is a BVC/wick **estimate**. See §2. Only unshaded regions are measured aggressor flow. |
| 1-second candles disagree with TradingView | Backfilled 1-second data is thin (~10–15% of the consolidated tape, IEX-biased). Live-collected regular-hours data is accurate; historical backfill is not. |
| A **permanent gap** in 1-second history | The collector was not running then. Full 1-second history cannot be re-backfilled after the fact. |
| Volume looks far too small | The raw tick stream captures only ~7–14% of consolidated volume; FinViz bars are used to rescale it. Without a FinViz token that rescaling is unavailable. |
| Flat / zero-volume bars outside market hours | After-hours backfill filler, and genuinely thin odd-lot tick data. |
| One huge gray bar at 16:00 | The closing auction (MOC), detected and neutralized on purpose — see §2. |

### The Level-2 heatmap

| What you see | What it means |
|---|---|
| No heatmap at all | **L2 Depth** is Off, or there are no depth snapshots for this time range. Depth is only collected for the 3 most recently searched tickers. |
| Heatmap in live mode is empty and the log shows `error 309` | The IBKR account has no market-depth subscription. |
| Only one S&R line (support but no resistance, or vice versa) | Correct when the book is genuinely one-sided at that moment — a side only draws a line if it has a wall passing the persistence + size threshold. |
| The book looks shallow next to Bookmap | Expected: IBKR SMART depth returns ~20 levels within roughly ±5% of price, not the full book. |

---

*Buy/sell splits over shaded (estimated) regions are approximations, not
measured aggressor flow. Treat unshaded (real-tick) regions as ground truth and
shaded regions as directional estimates.*
