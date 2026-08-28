"""
cvd/visualizer.py
-----------------
Three-screen Bookmap-style chart:
  Screen 1: candlestick
  Screen 2: indicator panel A (Buy/Sell volume default + CVD/Cumulative/Ratio toggles)
  Screen 3: indicator panel B — identical set, independently toggled (own legend)

So you can show Buy/Sell volume on one panel and, say, CVD on the other and
compare them at the same time. Timeframe buttons auto-scale the x-axis (and the
candle y-axis); a second button row filters trading hours.
"""

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import numpy as np

from .calculator import run_pipeline, TIMEFRAME_RULE, DAILY_OR_ABOVE, WEEK_OR_ABOVE, TIMEFRAME_RULE_IBKR

# Default cap on bars per frame. Plotly SVG renders EVERY bar, on-screen or
# not, so this number IS the render cost. 1,000 keeps every refresh snappy;
# scrollback beyond it is loaded PROGRESSIVELY — app.py doubles the cap (its
# bars-to-show store, hard cap 12,000) whenever the user pans to the left
# edge or zooms out, and passes it via build_chart(max_candles=...).
MAX_CANDLES = 1000


# ── Timestamp → row lookup ──────────────────────────────────────────────────
def nearest_positions(index, targets):
    """Row positions of the bars closest in time to `targets`.

    pandas' ``Index.get_indexer(method="nearest")`` REFUSES a non-unique index,
    raising InvalidIndexError. Every resampled frame has a unique index, but the
    raw_tick frame does not: several prints routinely land on the same
    timestamp, so any lookup against it blew up the whole chart. Doing the
    search by hand needs only a sorted index, which all frames are. When
    several rows share the winning timestamp it returns one of them (the first
    on an exact hit); they are the same instant, so which one is immaterial.

    Returns an int array of positions, empty when there is nothing to search.
    """
    vals = np.asarray(index)
    t = np.asarray(targets)
    if vals.size == 0 or t.size == 0:
        return np.empty(0, dtype=int)
    # Normalise both sides through DatetimeIndex when the index is temporal.
    # A plain list of Timestamps — which is how the anchor callers pass a
    # single instant — becomes an OBJECT array, and `datetime64 - object` is a
    # TypeError, not a coercion. Rebuilding both at a common unit also settles
    # us-vs-ns frames, which subtract without complaint but compare wrongly.
    if vals.dtype.kind == "M":
        vals = pd.DatetimeIndex(index).to_numpy(dtype="datetime64[ns]")
        t = pd.DatetimeIndex(targets).to_numpy(dtype="datetime64[ns]")
    right = np.clip(np.searchsorted(vals, t), 0, vals.size - 1)
    left = np.clip(right - 1, 0, vals.size - 1)
    # Ties go left, matching get_indexer's behaviour.
    take_left = np.abs(vals[left] - t) <= np.abs(vals[right] - t)
    return np.where(take_left, left, right)


# ── Fixed-domain pie strip sizing ──────────────────────────────────────────
# The pie strip aims for ~PIE_TARGET pies at any zoom, with EVERY pie covering
# the same number of bars so a pie's size reflects real volume, not how many
# bars it happened to swallow. Bars-per-pie k = round(n_visible / PIE_TARGET),
# so the pie count stays near the target as you zoom (k grows with the window),
# and zoomed in far enough (n_visible <= PIE_TARGET) it collapses to 1 pie = 1
# bar. Because integer k rarely divides n_visible exactly, the actual pie count
# drifts a little above/below the target; MAX_PIE_SLOTS is the fixed pool of
# pie TRACES build_chart emits (extras hidden) so the pan patch — which
# addresses pies by fixed index — always has enough slots for that drift.
PIE_TARGET = 25
MAX_PIE_SLOTS = 40


# Injected into the saved HTML (via write_html post_script).
#
# Plotly does NOT re-scale the y-axis when the visible x-window or the set of
# shown traces changes, so this script keeps every left y-axis fitted to
# whatever is actually on screen. `refitY()` scans the *currently shown* traces
# (visible===true, so legend-toggled-off traces are excluded), takes the
# min/max of their values inside the visible x-window, and sets each left axis
# (candles=y, panel A=y2, panel B=y4) to that range +/- 10% padding. The
# Buy-Ratio axes (y3/y5) stay fixed at 0-100%.
#
# refitY() is triggered on three events:
#   * plotly_relayout  -> pan / zoom the x-axis (Y follows the visible bars)
#   * plotly_restyle   -> a legend toggle (or timeframe button) shows/hides a
#                         trace, so the visible set changed
#   * dblclick (DOM)    -> Plotly's native double-click ("autorange to ALL
#                         data") is disabled via config doubleClick:false; we
#                         catch the DOM dblclick and restore the active
#                         timeframe's ~N_VISIBLE-candle default window (dfltX).
#
# NOTE on braces: write_html inserts this verbatim and only substitutes the
# literal token {plot_id}, so this string uses normal single braces.
REFIT_JS = """
var gd = document.getElementById('{plot_id}');
var YPAD = 0.10, busy = false;
var LEFT = {'y':'yaxis', 'y2':'yaxis2', 'y4':'yaxis4'};
var dfltX = (gd._fullLayout.xaxis.range || []).slice();

function visibleX() {
    var ax = gd._fullLayout.xaxis;
    if (!ax || !ax.range) return null;
    return [ax.range[0], ax.range[1]];
}

function refitY() {
    var xr = visibleX(); if (!xr) return;
    var upd = {};
    Object.keys(LEFT).forEach(function(yref) {
        var lo = Infinity, hi = -Infinity;
        gd._fullData.forEach(function(f) {
            if (f.visible === false || f.visible === 'legendonly') return;
            if ((f.yaxis || 'y') !== yref) return;
            // Traces may carry an explicit x array OR the compact x0/dx form.
            var xs = f.x, ys = f.y || f.close;
            var offset, len;
            if (xs && xs.length) { offset = xs[0]; len = xs.length; }
            else if (ys && ys.length) { offset = (f.x0 != null ? f.x0 : 0); len = ys.length; }
            else return;
            var i0 = Math.max(0, Math.floor(xr[0] - offset));
            var i1 = Math.min(len - 1, Math.ceil(xr[1] - offset));
            if (i0 > i1 || i0 >= len || i1 < 0) return; 
            
            if (f.low && f.high) {
                var lows = f.low, highs = f.high;
                for (var i = i0; i <= i1; i++) {
                    if (lows[i]  < lo) lo = lows[i];
                    if (highs[i] > hi) hi = highs[i];
                }
            } else if (f.y) {
                var ys = f.y;
                for (var i = i0; i <= i1; i++) {
                    var v = ys[i];
                    if (v == null || isNaN(v)) continue;
                    if (v < lo) lo = v;
                    if (v > hi) hi = v;
                }
            }
        });
        if (lo < hi && lo !== Infinity) {
            var pad = (hi - lo) * YPAD;
            upd[LEFT[yref] + '.range'] = [lo - pad, hi + pad];
        }
    });
    if (Object.keys(upd).length) {
        busy = true;
        Plotly.relayout(gd, upd).then(function() { busy = false; });
    }
}

var refitTimer = null;
function requestRefit() {
    if (refitTimer) clearTimeout(refitTimer);
    refitTimer = setTimeout(refitY, 150);
}

gd.on('plotly_relayout', function(ev) {
    if (busy) return;
    var keys = Object.keys(ev);
    if (keys.some(function(k) { return k.indexOf('title') > -1; })) {
        var r = gd._fullLayout.xaxis.range;
        if (r) dfltX = [r[0], r[1]];
    }
    if (keys.some(function(k) { return k.indexOf('xaxis') === 0; })) requestRefit();
});

// ── Toggle Bubble Button ──
var toggleBtn = document.createElement('button');
toggleBtn.innerHTML = 'Toggle Bubble Chart';
toggleBtn.style.position = 'absolute';
toggleBtn.style.top = '10px';
toggleBtn.style.left = '10px';
toggleBtn.style.zIndex = '1000';
toggleBtn.style.padding = '8px 12px';
toggleBtn.style.backgroundColor = '#444';
toggleBtn.style.color = '#fff';
toggleBtn.style.border = '1px solid #777';
toggleBtn.style.borderRadius = '4px';
toggleBtn.style.cursor = 'pointer';
toggleBtn.style.fontFamily = 'sans-serif';
toggleBtn.style.fontSize = '12px';

var bubble_state = false;
var enforcing_bubbles = false;

function applyBubbleState() {
    if (enforcing_bubbles) return;
    enforcing_bubbles = true;
    
    var update_indices = [];
    var new_visibility = [];
    
    for(var i=0; i<gd.data.length; i++) {
        if(gd.data[i].name && gd.data[i].name.indexOf('Bubble') > -1) {
            var is_outer = gd.data[i].name.indexOf('Outer') > -1;
            // The candle trace is 1 or 2 indices before the bubble trace
            var candle_idx = is_outer ? i - 1 : i - 2;
            var candle_visible = gd.data[candle_idx].visible === true;
            
            var target_visible = bubble_state && candle_visible;
            if (gd.data[i].visible !== target_visible) {
                update_indices.push(i);
                new_visibility.push(target_visible);
            }
        }
    }
    
    if (update_indices.length > 0) {
        Plotly.restyle(gd, {'visible': new_visibility}, update_indices).then(function() {
            enforcing_bubbles = false;
            requestRefit();
        });
    } else {
        enforcing_bubbles = false;
    }
}

toggleBtn.onclick = function() {
    bubble_state = !bubble_state;
    toggleBtn.style.backgroundColor = bubble_state ? '#26a69a' : '#444';
    applyBubbleState();
};

document.body.appendChild(toggleBtn);

gd.on('plotly_restyle', function() { 
    if (!busy) requestRefit(); 
    applyBubbleState();
});
document.addEventListener('keydown', function(e) {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    // Timeframe hotkeys: 1-9 = buttons 1-9, 0 = 10th, - = 11th
    var keyInt = parseInt(e.key);
    var tfIdx = null;
    if (!isNaN(keyInt) && keyInt >= 1 && keyInt <= 9) tfIdx = keyInt - 1;
    else if (e.key === '0') tfIdx = 9;
    else if (e.key === '-') tfIdx = 10;
    if (tfIdx !== null) {
        var btnGrp = document.querySelectorAll('.updatemenu-container')[0];
        if (btnGrp) {
            var btns = btnGrp.querySelectorAll('.updatemenu-button');
            if (tfIdx < btns.length) {
                btns[tfIdx].dispatchEvent(new MouseEvent('click', {bubbles: true}));
            }
        }
    }
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
        var xr = visibleX(); if (!xr) return;
        var span = xr[1] - xr[0];
        var shift = span * 0.05 * (e.key === 'ArrowLeft' ? -1 : 1);
        busy = true;
        Plotly.relayout(gd, {
            'xaxis.range': [xr[0] + shift, xr[1] + shift],
            'xaxis2.range': [xr[0] + shift, xr[1] + shift],
            'xaxis3.range': [xr[0] + shift, xr[1] + shift]
        }).then(function() { busy = false; requestRefit(); });
    }
    if (e.key.toLowerCase() === 'r') {
        if (!dfltX || !dfltX.length) return;
        busy = true;
        Plotly.relayout(gd, {
            'xaxis.range': dfltX.slice(), 'xaxis2.range': dfltX.slice(), 'xaxis3.range': dfltX.slice(),
        }).then(function() { busy = false; requestRefit(); });
    }
});

requestRefit();
"""



def write_chart_html(fig, path: str):
    """Save the figure to HTML with the y-axis auto-refit script attached.
    doubleClick:false disables Plotly's native 'autorange to all data' so our
    dblclick handler in REFIT_JS can restore the default window instead."""
    fig.write_html(path, post_script=REFIT_JS, config={"doubleClick": False, "scrollZoom": True})


# How many candles to show by default, regardless of timeframe/interval.
# The view is NOT locked — the user can pan/zoom freely; this is only the
# initial window (and what each timeframe button snaps back to) so that any
# timeframe opens at a comfortable ~50-60 candles instead of being squashed.
N_VISIBLE = 100

PAD = 0.10   # y-axis padding (fraction) so candles aren't flush against edges


def _count_window(df, offset, n: int = N_VISIBLE):
    """Return (x_start_idx, x_end_idx) covering the last ~n bars."""
    if df.empty:
        return None, None
    n = min(n, len(df))
    start_idx = offset + len(df) - n
    end_idx = offset + len(df) - 1
    # add some padding on edges
    return max(0, start_idx - 1), end_idx + 1


def _yrange(series, extra=PAD):
    s = series.dropna()
    if s.empty:
        return None
    lo, hi = float(s.min()), float(s.max())
    if lo == hi:
        lo, hi = lo - 1, hi + 1
    rng = hi - lo
    return [lo - rng * extra, hi + rng * extra]


# One indicator panel's worth of traces (11): buy bar, sell bar, CVD all,
# CVD raw, CVD BVC, cum total/buy/sell, buy ratio, buy%/sell% strips.
def _add_indicator_panel(fig, df, row, legend_id, on, default_on):
    """default_on: set of trace names shown by default in this panel."""
    ratio = df["buy_pressure"] / (df["buy_pressure"] + df["sell_pressure"])

    # Gray out bars whose volume is dominated by the closing auction (>50%):
    # their buy/sell split is neutralized (50/50) so the direction isn't real.
    GRAY = "#969696"
    frac = df["auction_frac"] if "auction_frac" in df.columns else pd.Series(0.0, index=df.index)

    def _gray(color):
        """Per-bar color list graying auction-dominated bars, else the flat color.
        Built only when an auction bar is in frame (a per-bar list is ~6,000
        strings per trace in the figure JSON)."""
        if bool((frac > 0.5).any()):
            return [GRAY if a > 0.5 else color for a in frac]
        return color

    def vis(name):
        if not on:
            return False
        return True if name in default_on else "legendonly"

    # All panel traces sit on the same regular integer grid: ship x as x0/dx
    # instead of repeating a 6,000-element array on every trace.
    x0 = float(df['x_idx'].iloc[0]) if len(df) else 0.0

    # ── Buy/Sell volume — three classification methods (tick / wick / BVC) ──
    # Each method splits the SAME bar volume: buy=(vol+delta)/2, sell=(vol-delta)/2.
    # tick = stored aggressor split (buy_pressure); wick/BVC are derived from the
    # rolled-up delta_wick_sum / delta_bvc_sum. They share one teal/red palette
    # because only one is meant to be shown at a time (overlapping bar sets would
    # stack), and a method's buy+sell share a legendgroup so a single legend entry
    # toggles both (default groupclick = togglegroup). ALWAYS emit all three so the
    # trace count stays fixed for the timeframe-button visibility arrays.
    vol = df["volume"]
    _dtick = df["buy_pressure"] - df["sell_pressure"]
    _dwick = df["delta_wick_sum"] if "delta_wick_sum" in df.columns else _dtick
    _dbvc  = df["delta_bvc_sum"]  if "delta_bvc_sum"  in df.columns else _dtick
    _methods = [
        ("tick", df["buy_pressure"],   df["sell_pressure"]),
        ("wick", (vol + _dwick) / 2.0, (vol - _dwick) / 2.0),
        ("BVC",  (vol + _dbvc)  / 2.0, (vol - _dbvc)  / 2.0),
    ]
    for _mi, (_m, _buy, _sell) in enumerate(_methods):
        label = f"Buy/Sell ({_m})"
        grp = f"g_bs_{_m}_{row}"
        v = vis(label)
        fig.add_trace(go.Bar(
            x0=x0, dx=1, y=_buy, name=label,
            width=0.8, offset=-0.4,
            marker=dict(color=_gray("#26a69a"), opacity=0.85),
            visible=v, showlegend=on, legend=legend_id, legendgroup=grp,
            customdata=df['time_str'],
            hovertemplate="<b>%{customdata}</b><br>Buy " + _m + ": %{y:,.0f}<extra></extra>",
        ), row=row, col=1, secondary_y=False)
        fig.add_trace(go.Bar(
            x0=x0, dx=1, y=-_sell, name=label,
            width=0.8, offset=-0.4,
            marker=dict(color=_gray("#ef5350"), opacity=0.85),
            visible=v, showlegend=False, legend=legend_id, legendgroup=grp,
            customdata=np.stack((df['time_str'], _sell), axis=-1),
            hovertemplate="<b>%{customdata[0]}</b><br>Sell " + _m + ": %{customdata[1]:,.0f}<extra></extra>",
        ), row=row, col=1, secondary_y=False)

    # Total volume per bar (buy + sell). A light WebGL outline over the bars so
    # the combined size is readable next to the split buy/sell bars — the
    # unified tooltip then also shows the total, which the two bars alone omit.
    fig.add_trace(go.Scattergl(
        x0=x0, dx=1, y=(df["buy_pressure"] + df["sell_pressure"]), mode="lines",
        name="Total Volume",
        line=dict(color="#b0bec5", width=1.2), connectgaps=True,
        visible=vis("Total Volume"), showlegend=on, legend=legend_id,
        hovertemplate="Total Vol: %{y:,.0f}<extra></extra>",
    ), row=row, col=1, secondary_y=False)

    fig.add_trace(go.Scattergl(
        x0=x0, dx=1, y=df["cvd_all_end"], mode="lines", name="CVD (all-time)",
        line=dict(color="#ba68c8", width=2), connectgaps=True,
        visible=vis("CVD (all-time)"), showlegend=on, legend=legend_id, customdata=df['time_str'],
        hovertemplate="<b>%{customdata}</b><br>CVD all: %{y:,.0f}<extra></extra>",
    ), row=row, col=1, secondary_y=False)

    # Independent BVC-estimated CVD (calculator.add_cvd_columns → cvd_bvc_end).
    # Always added (NaN if the column is missing) so trace counts stay fixed
    # for the static-HTML timeframe buttons.
    y_bvc = df["cvd_bvc_end"] if "cvd_bvc_end" in df.columns \
        else pd.Series(np.nan, index=df.index)
    fig.add_trace(go.Scattergl(
        x0=x0, dx=1, y=y_bvc, mode="lines", name="CVD (BVC est.)",
        line=dict(color="#4dd0e1", width=1.6, dash="dash"), connectgaps=True,
        visible=vis("CVD (BVC est.)"), showlegend=on, legend=legend_id,
        hovertemplate="CVD BVC: %{y:,.0f}<extra></extra>",
    ), row=row, col=1, secondary_y=False)

    # Pure wick-decomposition CVD, unified to the finest granularity (IBKR 1-sec /
    # FinViz 1-min, summed up the tiers — calculator.add_cvd_columns → cvd_wick_end).
    # Always added (NaN if missing) so trace counts stay fixed for the buttons.
    y_wick = df["cvd_wick_end"] if "cvd_wick_end" in df.columns \
        else pd.Series(np.nan, index=df.index)
    fig.add_trace(go.Scattergl(
        x0=x0, dx=1, y=y_wick, mode="lines", name="CVD (wick est.)",
        line=dict(color="#f06292", width=1.6, dash="dashdot"), connectgaps=True,
        visible=vis("CVD (wick est.)"), showlegend=on, legend=legend_id,
        hovertemplate="CVD wick: %{y:,.0f}<extra></extra>",
    ), row=row, col=1, secondary_y=False)

    fig.add_trace(go.Scattergl(
        x0=x0, dx=1, y=df["volume"].cumsum(), mode="lines", name="Cum Total",
        line=dict(color="#ffd54f", width=2), connectgaps=True,
        visible=vis("Cum Total"), showlegend=on, legend=legend_id,
        hovertemplate="Cum Total: %{y:,.0f}<extra></extra>",
    ), row=row, col=1, secondary_y=False)

    fig.add_trace(go.Scattergl(
        x0=x0, dx=1, y=df["buy_pressure"].cumsum(), mode="lines", name="Cum Buy",
        line=dict(color="#66bb6a", width=1.6), connectgaps=True,
        visible=vis("Cum Buy"), showlegend=on, legend=legend_id,
        hovertemplate="Cum Buy: %{y:,.0f}<extra></extra>",
    ), row=row, col=1, secondary_y=False)

    fig.add_trace(go.Scattergl(
        x0=x0, dx=1, y=df["sell_pressure"].cumsum(), mode="lines", name="Cum Sell",
        line=dict(color="#e57373", width=1.6), connectgaps=True,
        visible=vis("Cum Sell"), showlegend=on, legend=legend_id,
        hovertemplate="Cum Sell: %{y:,.0f}<extra></extra>",
    ), row=row, col=1, secondary_y=False)

    fig.add_trace(go.Scattergl(
        x0=x0, dx=1, y=ratio.round(4), mode="lines", name="Buy Ratio",
        line=dict(color="#ff9800", width=1.6, dash="dot"), connectgaps=True,
        visible=vis("Buy Ratio"), showlegend=on, legend=legend_id,
        hovertemplate="Buy Ratio: %{y:.1%}<extra></extra>",
    ), row=row, col=1, secondary_y=True)

    # ── Delta Ratio Strip (Stacked Bar on Secondary Y) ──
    # barmode="relative" stacks same-sign bars sharing an offsetgroup, so the
    # sell strip needs no explicit base array (saved ~0.24 MB of JSON).
    tot_vol = df["buy_pressure"] + df["sell_pressure"]
    tot_vol = np.where(tot_vol == 0, 1, tot_vol)
    buy_pct = (df["buy_pressure"] / tot_vol).round(4)
    sell_pct = (df["sell_pressure"] / tot_vol).round(4)

    fig.add_trace(go.Bar(
        x0=x0, dx=1, y=buy_pct, name="Buy % (Strip)",
        marker_color="rgba(38,166,154,0.6)", visible=vis("Buy % (Strip)"), showlegend=on,
        legend=legend_id, hoverinfo="y", offsetgroup=f"strip_{row}"
    ), row=row, col=1, secondary_y=True)

    fig.add_trace(go.Bar(
        x0=x0, dx=1, y=sell_pct, name="Sell % (Strip)",
        marker_color="rgba(239,83,80,0.6)", visible=vis("Sell % (Strip)"), showlegend=on,
        legend=legend_id, hoverinfo="y", offsetgroup=f"strip_{row}"
    ), row=row, col=1, secondary_y=True)


def _add_source_annotations(fig: go.Figure, frames: dict) -> None:
    """
    Step 3: Add shaded background zones marking wick-estimated data regions.

    For each source that uses wick decomposition (finviz_wick, ibkr_hist,
    alpaca_wick) a light semi-transparent rectangle is drawn across the full
    chart height, plus a small text label at the left edge of each zone.
    Real-tick sources (ibkr_tick, alpaca_tick) are left unmarked — they are
    the ground truth and need no disclaimer.

    Every timeframe occupies its own disjoint x_idx interval, so rects for
    all timeframes can be drawn at once: only the active timeframe's region
    is ever inside the visible x-window. Mixed-source data is handled by
    splitting into contiguous runs per source (instead of one first→last
    rect that would also cover tick regions in between).

    Uses xref='x' (shared x-axis) and yref='paper' (full height) so the shape
    spans all three rows simultaneously.
    """
    EST_SOURCES = {
        "finviz_wick": dict(
            fillcolor="rgba(255,210,100,0.07)",
            label="FinViz (est.)",
            font_color="rgba(220,180,80,0.75)",
        ),
        "ibkr_hist": dict(
            fillcolor="rgba(100,160,255,0.07)",
            label="IBKR hist (est.)",
            font_color="rgba(100,160,220,0.75)",
        ),
        "alpaca_wick": dict(
            fillcolor="rgba(200,120,255,0.07)",
            label="Alpaca wick (est.)",
            font_color="rgba(180,100,220,0.75)",
        ),
    }

    # Tier-served frames (history package) carry a `quality` flag that is more
    # precise than the source tag: shade every non-tick-quality run.
    QUALITY_STYLES = {
        "bvc": dict(
            fillcolor="rgba(255,210,100,0.07)",
            label="BVC (est.)",
            font_color="rgba(220,180,80,0.75)",
        ),
        "wick": dict(
            fillcolor="rgba(255,210,100,0.07)",
            label="Wick (est.)",
            font_color="rgba(220,180,80,0.75)",
        ),
        "mixed": dict(
            fillcolor="rgba(100,160,255,0.05)",
            label="Mixed (part est.)",
            font_color="rgba(100,160,220,0.75)",
        ),
    }

    for df in frames.values():
        if "x_idx" not in df.columns:
            continue
        if "quality" in df.columns:
            flag_name, styles = "quality", QUALITY_STYLES
        elif "source" in df.columns:
            flag_name, styles = "source", EST_SOURCES
        else:
            continue
        # Empty session-grid slots carry NaN flags; let them inherit the
        # surrounding run instead of fragmenting it into thousands of
        # one-bar runs (each run costs an add_shape → massive figures).
        # Positional frame → safe on raw-tick indexes with duplicate labels.
        # Fill missing quality for raw ticks so they don't inherit 'bvc' via ffill
        if "quality" in df.columns and "source" in df.columns:
            df_flag = df["quality"].copy()
            df_flag = df_flag.mask(df_flag.isna() & df["source"].str.contains("tick", na=False), "tick")
            flag_array = df_flag.ffill().to_numpy()
        else:
            flag_array = df[flag_name].ffill().to_numpy()

        runs = pd.DataFrame({
            "flag": flag_array,
            "x": df["x_idx"].to_numpy(),
        })
        runs["rid"] = (runs["flag"] != runs["flag"].shift()).cumsum()
        # Absorb short runs (< MIN_RUN_BARS) into the preceding run: a one-off
        # estimated bar in a tick stretch (or vice versa) is noise, not a
        # regime change — shading/lines only mark multi-bar transitions.
        MIN_RUN_BARS = 3
        run_sizes = runs.groupby("rid")["flag"].transform("size")
        runs["flag"] = runs["flag"].mask(run_sizes < MIN_RUN_BARS).ffill().bfill()
        runs["rid"] = (runs["flag"] != runs["flag"].shift()).cumsum()
        last_top_text_x = -9999
        for _, run in runs.groupby("rid"):
            style = styles.get(run["flag"].iloc[0])
            if style is None:
                continue
            
            # Use -0.5 and +0.5 to make the rectangle fully cover the candle width
            x0 = run["x"].iloc[0] - 0.5
            x1 = run["x"].iloc[-1] + 0.5

            fig.add_shape(
                type="rect",
                x0=x0, x1=x1,
                y0=0, y1=1,
                yref="paper", xref="x",
                fillcolor=style["fillcolor"],
                line_width=0,
                layer="below",
            )
            
            # User prefers the colored top text. Add overlap protection.
            if (x0 - last_top_text_x) > (df["x_idx"].max() * 0.05):
                fig.add_annotation(
                    x=x0, y=0.98, yref="paper", xref="x",
                    text=style["label"], showarrow=False, xanchor="left", yanchor="top",
                    font=dict(size=8, color=style["font_color"]),
                )
                last_top_text_x = x0

        # Draw demarcation lines ONLY when transitioning from estimated to true tick
        run_list = [r for _, r in runs.groupby("rid")]
        
        for i in range(len(run_list) - 1):
            curr_run = run_list[i]
            next_run = run_list[i+1]
            
            curr_is_est = styles.get(curr_run["flag"].iloc[0]) is not None
            next_is_est = styles.get(next_run["flag"].iloc[0]) is not None
            
            if curr_is_est != next_is_est:
                # Place the line exactly between the two candles, which also perfectly matches
                # the x1 + 0.5 boundary of the background rectangle.
                x_split = curr_run["x"].iloc[-1] + 0.5
                
                fig.add_shape(
                    type="line", x0=x_split, x1=x_split, y0=0, y1=1,
                    yref="paper", xref="x", line=dict(color="rgba(255,255,255,0.10)", width=1.0, dash="dot"), layer="below",
                )




def _pie_range_label(idx, n: int) -> str:
    """Human 'this pie covers bars from → to' label for a candle group, from the
    frame's datetime index (seconds for intraday, date for daily+). Shown on
    pie hover so it's clear which candles each pie aggregates."""
    try:
        t0, t1 = pd.Timestamp(idx[0]), pd.Timestamp(idx[-1])
    except Exception:
        return f"{n} bar" + ("s" if n != 1 else "")
    if (t0 == t0.normalize()) and (t1 == t1.normalize()):   # daily / weekly / monthly
        s0, s1 = t0.strftime("%Y-%m-%d"), t1.strftime("%Y-%m-%d")
    else:                                                   # intraday: keep seconds
        s0, s1 = t0.strftime("%m-%d %H:%M:%S"), t1.strftime("%H:%M:%S")
    span = s0 if n <= 1 else f"{s0} → {s1}"
    return f"{span} ({n} bar" + ("s" if n != 1 else "") + ")"


def compute_pie_slots(df_vis, n_slots: int):
    """Group the visible candles into EQUAL-width pie slots and pad to a fixed
    pool of n_slots trace slots.

    Every real pie covers exactly k = round(n_visible / PIE_TARGET) candles
    (>= 1), so pie size compares volume fairly instead of rewarding a pie for
    swallowing more bars. The number of real pies is ceil(n_visible / k), which
    tracks PIE_TARGET across zoom (k grows with the window) and collapses to
    1 pie = 1 bar once zoomed in past the target. Any remainder goes to the
    OLDEST (leftmost) pie, so the most-recent bars the user watches are always
    exactly k. Returns a list of length n_slots; each entry is
    (buy, sell, center_x_idx, range_label) for a real pie or None for a hidden
    slot. Shared by build_chart (initial render) and app.update_pie_charts_on_pan
    (pan patch) so the two never drift out of alignment."""
    n_slots = int(n_slots)
    if n_slots <= 0:
        return []
    df_vis = df_vis.sort_values("x_idx")
    n_vis = len(df_vis)
    if n_vis == 0:
        return [None] * n_slots
    # Bars per pie, then the resulting pie count. Clamp the count to the trace
    # pool (bumping k) so a pathological window can never need more slots than
    # build_chart emitted.
    k = max(1, int(round(n_vis / PIE_TARGET)))
    n_groups = int(np.ceil(n_vis / k))
    if n_groups > n_slots:
        k = int(np.ceil(n_vis / n_slots))
        n_groups = int(np.ceil(n_vis / k))
    # Leftmost group absorbs the remainder (size 1..k); the rest are exactly k.
    first = n_vis - (n_groups - 1) * k
    bounds = [0, first] + [first + i * k for i in range(1, n_groups)]
    slots = []
    for g in range(n_groups):
        sub = df_vis.iloc[bounds[g]:bounds[g + 1]]
        slots.append((float(sub["buy_pressure"].sum()),
                      float(sub["sell_pressure"].sum()),
                      float(sub["x_idx"].mean()),
                      _pie_range_label(sub.index, len(sub))))
    slots += [None] * (n_slots - n_groups)
    return slots


def pie_layout(df_vis, n_slots: int, x0: float, x1: float):
    """Per-slot pie geometry from equal-width grouping + sqrt-volume radius.
    Returns a list of length n_slots; each entry is {"buy","sell","d_x","d_y"}
    for a real pie or None for a hidden slot. Each pie is centered on its candle
    group's mean x_idx mapped into the plot's paper width, so it sits under those
    candles. Sizing uses the number of REAL pies so a zoomed-in view (few
    candles) still fills the strip."""
    n_slots = int(n_slots)
    slots = compute_pie_slots(df_vis, n_slots)
    reals = [s for s in slots if s is not None and (s[0] + s[1]) > 0]
    if not reals:
        return [None] * n_slots
    max_vol = max((s[0] + s[1] for s in reals), default=0.0) or 1.0
    n_eff = len(reals)
    overall_width = 0.94
    width = overall_width / max(n_eff, 1)
    x_span = x1 - x0
    out = []
    for k, s in enumerate(slots):
        if s is None or (s[0] + s[1]) <= 0:
            out.append(None)
            continue
        buy, sell, c_center, label = s
        factor = max(0.15, ((buy + sell) / max_vol) ** 0.5)
        if x_span > 0:
            x_center = (c_center - x0) / x_span * overall_width
        else:
            x_center = k * width + width / 2.0
        radius = min(width * 0.45 * factor, overall_width * 0.45)   # cap: no domain inversion
        x_center = max(radius, min(overall_width - radius, x_center))
        out.append({"buy": buy, "sell": sell, "label": label,
                    "d_x": [x_center - radius, x_center + radius],
                    "d_y": [0.52 - 0.04 * factor, 0.52 + 0.04 * factor]})
    return out


def build_chart(df: pd.DataFrame, frames: dict, ticker: str, active_timeframe: str = "1sec", pie_chart_count: int = 0, x_range: tuple = None, max_candles: int = None, show_bubbles: bool = True, l2_data: dict = None) -> go.Figure:

    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        row_heights=[0.50, 0.25, 0.25],
        vertical_spacing=0.06,
        specs=[[{}], [{"secondary_y": True}], [{"secondary_y": True}]],
        subplot_titles=(f"{ticker} — Candlestick", "", "")
    )
    
    # ── Add zero lines for Indicator Panels (Must be done before Pie traces to avoid Plotly bugs) ──
    fig.add_hline(y=0, line=dict(color="gray", width=0.8, dash="dot"), row=2, col=1)
    fig.add_hline(y=0, line=dict(color="gray", width=0.8, dash="dot"), row=3, col=1)

    timeframes = list(frames.keys())
    if active_timeframe and active_timeframe in timeframes:
        timeframes = [active_timeframe]
        default_tf = active_timeframe
        default_idx = 0
    else:
        default_tf = "1hr" if "1hr" in timeframes else timeframes[-1]
        default_idx = timeframes.index(default_tf)

    tf_offsets = {}
    current_idx = 0
    candle_cap = int(max_candles) if max_candles else MAX_CANDLES
    for i, tf in enumerate(timeframes):
        # ── DOM Optimization: Limit short timeframes to the most recent bars
        df = frames[tf]
        if len(df) > candle_cap:
            df = df.iloc[-candle_cap:]
        # Copy so x_idx/time_str additions never mutate the caller's frames
        # (and never trigger SettingWithCopyWarning on the truncated slice).
        df = df.copy()

        # Trim float noise before serialization: BVC/wick splits carry 12+
        # decimal digits per value, which balloons the figure JSON the
        # browser has to download and parse on every refresh.
        for c in ("buy_pressure", "sell_pressure", "volume", "delta_sum",
                  "net_pressure", "cvd_all_end", "cvd_all_raw_end", "cvd_bvc_end",
                  "cvd_wick_end", "auction_vol", "delta_wins_sum", "delta_bvc_sum",
                  "delta_wick_sum",
                  "cvd_session_end", "cvd_wins_end", "cvd_session_wins_end"):
            if c in df.columns:
                df[c] = df[c].round(1)

        frames[tf] = df
        df['x_idx'] = np.arange(len(df)) + current_idx
        if tf in DAILY_OR_ABOVE:
            df['time_str'] = df.index.strftime('%Y-%m-%d')
        else:
            df['time_str'] = df.index.strftime('%Y-%m-%d %H:%M')
            
        # --- Z-Score & Absorption Calculation ---
        lookback = 60
        delta = df["buy_pressure"].fillna(0) - df["sell_pressure"].fillna(0)
        abs_delta = delta.abs()
        mean_delta = abs_delta.rolling(window=lookback, min_periods=10).mean()
        std_delta = abs_delta.rolling(window=lookback, min_periods=10).std()
        df["z_score_delta"] = np.where(std_delta > 0, (abs_delta - mean_delta) / std_delta, 0.0)
        
        tot_vol = df["buy_pressure"].fillna(0) + df["sell_pressure"].fillna(0)
        mean_vol = tot_vol.rolling(window=lookback, min_periods=10).mean()
        std_vol = tot_vol.rolling(window=lookback, min_periods=10).std()
        df["z_score_vol"] = np.where(std_vol > 0, (tot_vol - mean_vol) / std_vol, 0.0)
        
        candle_body = (df["close"] - df["open"]).abs()
        avg_body = candle_body.rolling(window=lookback, min_periods=10).mean()
        df["is_absorption"] = (df["z_score_delta"] >= 2.0) & (candle_body < (avg_body * 0.6))
        # ----------------------------------------
            
        tf_offsets[tf] = current_idx
        current_idx += len(df)
        
    # Source composition for the title. A bare mode() is misleading on mixed
    # tiers (e.g. today's bars IBKR-derived, history FinViz — the mode said
    # just "finviz"): show every source with its share instead.
    df_active = frames[default_tf]
    if not df_active.empty and "source" in df_active.columns:
        shares = df_active["source"].value_counts(normalize=True)
        parts = [f"{s} {frac:.0%}" for s, frac in shares.items() if frac >= 0.005]
        src_str = " + ".join(parts[:3]) if parts else "unknown"
    else:
        src_str = "unknown"

    N_TRACES = 19

    for tf in timeframes:
        df = frames[tf]
        on = (tf == default_tf)

        # ── Level-2 depth heatmap (single-frame mode only, like the pies —
        # the timeframe-button path never receives l2_data). Added BEFORE the
        # candle trace so price action paints on top of the liquidity bands.
        # z columns were computed against the same truncated tail (app passes
        # frames[tf].iloc[-max_candles:]), so they map onto the last n x_idx.
        if l2_data is not None and len(timeframes) == 1 and l2_data.get("z") is not None:
            _z = l2_data["z"]
            _n = min(int(_z.shape[1]), len(df))
            # Saturate the colorscale at the 98th percentile of resting size:
            # one transient iceberg print would otherwise own the whole scale
            # and wash the persistent walls (the interesting part) to near
            # invisibility.
            _pos = _z[_z > 0]
            _zmax = float(np.percentile(_pos, 98)) if _pos.size else None
            # Bookmap-style heat ramp: dark blue (thin) → cyan → yellow →
            # white (walls). zmax sits at the 98th percentile, so the top ~2%
            # of resting size — the persistent walls — pop in yellow/white
            # while ordinary depth stays a quiet translucent blue under the
            # candles.
            # Empty cells (no resting size) become NaN: not painted AND not
            # hoverable — otherwise every blank spot shows a "size 0" tooltip.
            _zplot = _z[:, -_n:].astype(float)
            _zplot[_zplot <= 0] = np.nan
            # Column x-positions: match the z-columns to the bars they were built
            # on by timestamp (l2_data["x_times"]), so anchor-centered views place
            # the bands on-screen. Fall back to the last _n x_idx (tail/live view)
            # when x_times is absent or can't be matched.
            _xt = l2_data.get("x_times")
            if _xt is not None and len(_xt) >= _n:
                _rows = nearest_positions(
                    df.index, pd.DatetimeIndex(list(_xt)[-_n:]))
                _xcoords = df['x_idx'].to_numpy()[_rows]
            else:
                _xcoords = df['x_idx'].iloc[-_n:].to_numpy()
            fig.add_trace(go.Heatmap(
                x=_xcoords,
                y=list(l2_data["y_levels"]),
                z=_zplot,
                # Ordinary depth is kept faint (low alpha) so it reads as a
                # quiet background and the candles stay legible on top; only the
                # persistent walls (top ~8%, yellow→white) turn opaque enough to
                # stand out. Dimming the mid stops fixes the "heatmap drowns the
                # candles" overlap.
                colorscale=[[0.0, "rgba(0,0,0,0)"],
                            [0.08, "rgba(21,60,160,0.16)"],
                            [0.35, "rgba(41,150,255,0.30)"],
                            [0.70, "rgba(0,229,255,0.48)"],
                            [0.92, "rgba(255,235,59,0.74)"],
                            [1.0, "rgba(255,255,255,0.92)"]],
                zmin=0.0, zmax=_zmax,
                showscale=False, hoverongaps=False,
                hovertemplate="price %{y:.2f} · size %{z:,.0f}<extra>L2</extra>",
                name=f"L2 Depth ({tf})", visible=on, showlegend=False,
            ), row=1, col=1, secondary_y=False)

        # Screen 1: candle or scatter (for raw ticks)
        if tf == "raw_tick":
            # For raw ticks, OHLC are all the same, so we plot a simple line or scatter.
            # Using markers allows seeing individual trades clearly.
            fig.add_trace(go.Scattergl(
                x0=float(df['x_idx'].iloc[0]) if len(df) else 0.0, dx=1,
                y=df["close"],
                mode="lines+markers",
                name=f"Raw Ticks",
                line=dict(color="#29b6f6", width=1),
                marker=dict(size=3, color="#29b6f6"),
                visible=on, showlegend=False, customdata=df['time_str'],
                hovertemplate="<b>%{customdata}</b><br>Price: %{y:.2f}<extra></extra>",
            ), row=1, col=1, secondary_y=False)
        else:
            fig.add_trace(go.Candlestick(
                x=df['x_idx'], open=df["open"], high=df["high"], low=df["low"], close=df["close"],
                name=f"Candle ({tf})",
                increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
                increasing_fillcolor="#26a69a", decreasing_fillcolor="#ef5350",
                visible=on, showlegend=False, customdata=df['time_str'],
                hovertemplate="<b>%{customdata}</b><br>O: %{open:.2f}<br>H: %{high:.2f}<br>L: %{low:.2f}<br>C: %{close:.2f}<extra></extra>",
            ), row=1, col=1, secondary_y=False)

        # --- Add Volume/Absorption Bubbles ---
        if show_bubbles:
            if "z_score_delta" in df.columns:
                mask_bubble = (df["z_score_delta"] >= 2.0) | (df["z_score_vol"] >= 3.0)
            else:
                mask_bubble = pd.Series(False, index=df.index)
            if mask_bubble.any():
                df_b = df[mask_bubble].copy()
                
                colors = []
                sizes = []
                texts = []
                for idx, row_data in df_b.iterrows():
                    z_delta = row_data["z_score_delta"]
                    z_vol = row_data["z_score_vol"]
                    is_abs = row_data["is_absorption"]
                    d_val = row_data["buy_pressure"] - row_data["sell_pressure"]
                    tot_val = row_data["buy_pressure"] + row_data["sell_pressure"]
                    
                    if is_abs:
                        c = "rgba(157, 0, 255, 0.9)"
                        sz = z_delta
                        raw_val = d_val
                        prefix = "+" if d_val > 0 else ""
                    elif z_delta >= 2.0:
                        c = "rgba(0, 150, 100, 0.7)" if d_val > 0 else "rgba(182, 0, 61, 0.7)"
                        sz = z_delta
                        raw_val = d_val
                        prefix = "+" if d_val > 0 else ""
                    else:
                        c = "rgba(255, 215, 0, 0.7)"
                        sz = z_vol
                        raw_val = tot_val
                        prefix = ""
                    
                    if abs(raw_val) >= 1e6:
                        txt = f"{prefix}{raw_val/1e6:.1f}M"
                    elif abs(raw_val) >= 1e3:
                        txt = f"{prefix}{raw_val/1e3:.1f}K"
                    else:
                        txt = f"{prefix}{raw_val:.0f}"
                        
                    colors.append(c)
                    sizes.append(min(45, 12 + sz * 6))
                    texts.append(txt)
                    
                y_coords = (df_b["open"] + df_b["high"] + df_b["low"] + df_b["close"]) / 4.0
                
                fig.add_trace(go.Scatter(
                    x=df_b["x_idx"],
                    y=y_coords,
                    mode="markers+text",
                    name=f"Volume Bubbles ({tf})",
                    marker=dict(
                        size=sizes,
                        color=colors,
                        line=dict(width=1, color="rgba(255,255,255,0.7)")
                    ),
                    text=texts,
                    textposition="middle center",
                    textfont=dict(size=10, color="white", family="Arial Black"),
                    visible=on,
                    showlegend=False,
                    hoverinfo="skip"
                ), row=1, col=1, secondary_y=False)
            else:
                # Empty placeholder: the timeframe-button visibility arrays
                # below assume a fixed trace count per frame, so the bubble
                # trace must exist even when no bar clears the threshold.
                fig.add_trace(go.Scatter(
                    x=[], y=[], mode="markers+text",
                    name=f"Volume Bubbles ({tf})",
                    visible=on, showlegend=False, hoverinfo="skip",
                ), row=1, col=1, secondary_y=False)
        # -------------------------------------

        # ── Fixed Domain Pie Charts ──
        if pie_chart_count > 0 and len(timeframes) == 1:
            N_total = len(df)
            if x_range is not None:
                x0_init = x_range[0]
                x1_init = x_range[1]
            else:
                x0_init = max(0, N_total - 100)
                x1_init = N_total
                
            df_vis = df[(df['x_idx'] >= x0_init) & (df['x_idx'] <= x1_init)]

            # Pies are an on/off feature now: any positive pie_chart_count means
            # "on", and the strip auto-sizes to ~PIE_TARGET equal-width pies via
            # compute_pie_slots. We ALWAYS emit MAX_PIE_SLOTS traces — extras are
            # hidden (zeroed domain) — because the pan patch in
            # app.update_pie_charts_on_pan addresses pies by fixed index and the
            # real pie count drifts with the zoom level.
            n_slots = MAX_PIE_SLOTS
            layout = pie_layout(df_vis, n_slots, x0_init, x1_init)
            for slot in layout:
                if slot is not None:
                    fig.add_trace(go.Pie(
                        values=[slot["buy"], slot["sell"]],
                        labels=["Buy", "Sell"],
                        marker=dict(colors=["rgba(38,166,154,0.7)", "rgba(239,83,80,0.7)"]),
                        textinfo="none",
                        hoverinfo="text",
                        hovertext=(f"{slot['label']}<br>Buy: {slot['buy']:,.0f}"
                                   f"<br>Sell: {slot['sell']:,.0f}"),
                        hole=0.0,
                        domain=dict(x=slot["d_x"], y=slot["d_y"]),
                        sort=False,
                        direction="clockwise",
                        showlegend=False
                    ))
                else:
                    # Hidden slot (fewer visible candles than pies, or empty): keep
                    # the trace so the pan patch's fixed indices stay valid.
                    fig.add_trace(go.Pie(
                        values=[0, 0], marker=dict(colors=["rgba(100,100,100,0.5)", "rgba(100,100,100,0.5)"]),
                        domain=dict(x=[0, 0], y=[0, 0]),
                        hoverinfo='none', textinfo='none', hole=0.0
                    ))



        # Screen 2: defaults to tick Buy/Sell volume; Screen 3: defaults to CVD (all-time)
        _add_indicator_panel(fig, df, row=2, legend_id="legend",  on=on,
                             default_on={"Buy/Sell (tick)", "Total Volume"})
        _add_indicator_panel(fig, df, row=3, legend_id="legend2", on=on,
                             default_on={"CVD (all-time)"})

    # ── Timeframe buttons ──
    buttons = []
    for i, tf in enumerate(timeframes):
        df = frames[tf]
        offset = tf_offsets[tf]
        
        # panel order (16): B/S tick buy, B/S tick sell, B/S wick buy, B/S wick sell,
        # B/S BVC buy, B/S BVC sell, total, CVD all, CVD BVC, CVD wick, cum total,
        # cum buy, cum sell, ratio, buy%, sell%
        LO = "legendonly"
        panelA = [True, True, LO, LO, LO, LO, True, LO, LO, LO, LO, LO, LO, LO, LO, LO]
        panelB = [LO, LO, LO, LO, LO, LO, LO, True, LO, LO, LO, LO, LO, LO, LO, LO]
        # Sell bars share a legendgroup with their buy (one legend entry per
        # method), so they never carry their own legend item.
        SHOWLEG = [True, False, True, False, True, False, True, True, True, True,
                   True, True, True, True, True, True]
        visibility = []

        # In static HTML mode, we skip dynamic traces (pie/strip) for updatemenus
        # because their trace count is variable. This loop is only used if not active_timeframe.
        head = 2 if show_bubbles else 1     # candle (+ bubble trace) per frame
        N_TRACES = head + 32                # + 32 indicators (16 per panel)
        for j in range(len(timeframes)):
            if j == i:
                # Candle (+ bubbles) + panels
                visibility += [True] * head + panelA + panelB
            else:
                visibility += [False] * N_TRACES

        showlegend = []
        for j in range(len(timeframes)):
            if j == i:
                showlegend += [False] * head + SHOWLEG + SHOWLEG
            else:
                showlegend += [False] * N_TRACES

        x0, x1 = _count_window(df, offset)
        in_view = df.iloc[-min(N_VISIBLE, len(df)):]
        if not in_view.empty:
            y1 = _yrange(pd.concat([in_view["high"], in_view["low"]]))
            y_pa = _yrange(pd.concat([in_view["buy_pressure"], -in_view["sell_pressure"]]))
            y_pb = _yrange(in_view["cvd_all_end"])
        else:
            y1 = y_pa = y_pb = None

        layout = {
            "title": f"<b>{ticker}</b> — {tf}",
            "xaxis.range":  [x0, x1],
            "xaxis2.range": [x0, x1],
            "xaxis3.range": [x0, x1],
        }
        if y1:  layout["yaxis.range"]  = y1
        if y_pa: layout["yaxis2.range"] = y_pa   # panel A (buy/sell scale)
        if y_pb: layout["yaxis4.range"] = y_pb   # panel B (CVD scale)

        buttons.append(dict(
            label=tf, method="update",
            args=[{"visible": visibility, "showlegend": showlegend}, layout],
        ))

    # ── Layout ──
    df0 = frames[default_tf]
    offset0 = tf_offsets[default_tf]
    x0, x1 = _count_window(df0, offset0)
    in0 = df0.iloc[-min(N_VISIBLE, len(df0)):]
    y1_0 = _yrange(pd.concat([in0["high"], in0["low"]]))
    y_pa_0 = _yrange(pd.concat([in0["buy_pressure"], -in0["sell_pressure"]]))
    y_pb_0 = _yrange(in0["cvd_all_end"])

    layout_kwargs = dict(
        title=dict(text=f"<b>{ticker}</b> — {default_tf} <span style='font-size:12px;color:gray;'>(Source: {src_str})</span>", font=dict(size=18)),
        template="plotly_dark",
        height=1150,
        barmode="relative",
        bargap=0.1,
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        hoverlabel=dict(bgcolor="rgba(25, 25, 25, 0.95)", font_size=12, font_color="white", bordercolor="#444"),
        dragmode="pan",  # Left-click drags to pan instead of selecting a zoom box
        # two independent legends, one per indicator panel
        legend=dict(orientation="h", yanchor="bottom", y=0.25, xanchor="left", x=0,
                    bgcolor="rgba(0,0,0,0)", font=dict(size=10)),
        legend2=dict(orientation="h", yanchor="top", y=-0.02, xanchor="left", x=0,
                     bgcolor="rgba(0,0,0,0)", font=dict(size=10)),
        margin=dict(l=60, r=60, t=120, b=150 if pie_chart_count > 0 else 60),
        uirevision="constant",  # Prevents Plotly from resetting UI state on relayout
    )
    if not active_timeframe:
        layout_kwargs["updatemenus"] = [
            dict(type="buttons", direction="right", x=0.0, y=1.09, xanchor="left",
                 buttons=buttons, bgcolor="#2d2d2d", bordercolor="#888",
                 font=dict(color="#aaaaaa", size=12), active=default_idx),
        ]
        
    fig.update_layout(**layout_kwargs)

    fig.update_xaxes(type="linear", showticklabels=False, showspikes=True, spikemode="across", spikedash="solid", spikecolor="gray", spikethickness=1)
    fig.update_yaxes(title_text="Price (USD)", fixedrange=True, row=1, col=1, showspikes=True, spikemode="across", spikedash="solid", spikecolor="gray", spikethickness=1)
    fig.update_yaxes(title_text="Volume / CVD", fixedrange=True, row=2, col=1, secondary_y=False, showspikes=True, spikemode="across", spikedash="solid", spikecolor="gray", spikethickness=1)
    fig.update_yaxes(title_text="Ratio", fixedrange=True, row=2, col=1, secondary_y=True, tickformat=".0%", range=[0, 1])
    fig.update_yaxes(title_text="Volume / CVD", fixedrange=True, row=3, col=1, secondary_y=False, showspikes=True, spikemode="across", spikedash="solid", spikecolor="gray", spikethickness=1)
    fig.update_yaxes(title_text="Ratio", fixedrange=True, row=3, col=1, secondary_y=True, tickformat=".0%", range=[0, 1])
    fig.update_xaxes(title_text="Time", row=3, col=1)

    # ── Current Value Labels ──
    # Use the last bar that actually traded: session-grid frames end with
    # empty (NaN) slots whenever the final expected bin has no data yet.
    if not df.empty and df["close"].notna().any():
        valid = df.dropna(subset=["close"])
        last_close = valid["close"].iloc[-1]
        last_open = valid["open"].iloc[-1]
        cvd_col = "cvd_all_end" if "cvd_all_end" in df.columns else "cvd_all"
        _cvd_valid = df[cvd_col].dropna()
        last_cvd = _cvd_valid.iloc[-1] if not _cvd_valid.empty else 0.0
        
        fig.add_annotation(
            xref="paper", yref="y",
            x=1, y=last_close,
            text=f"{last_close:.2f}",
            showarrow=False,
            xanchor="left",
            bgcolor="rgba(38,166,154,0.9)" if last_close >= last_open else "rgba(239,83,80,0.9)",
            font=dict(color="white", size=11),
            borderpad=3
        )
        fig.add_annotation(
            xref="paper", yref="y4",
            x=1, y=last_cvd,
            text=f"{last_cvd:,.0f}",
            showarrow=False,
            xanchor="left",
            bgcolor="#ba68c8",
            font=dict(color="white", size=11),
            borderpad=3
        )

    fig.update_xaxes(range=[x0, x1])
    if y1_0:
        fig.update_yaxes(range=y1_0, row=1, col=1)
    if y_pa_0:
        fig.update_yaxes(range=y_pa_0, row=2, col=1, secondary_y=False)
    if y_pb_0:
        fig.update_yaxes(range=y_pb_0, row=3, col=1, secondary_y=False)

    # ── Level-2 support/resistance lines (persistent resting liquidity,
    # computed in level2_webapp.data_provider.compute_support_resistance) ──
    if l2_data is not None and len(timeframes) == 1:
        # Blue/amber on purpose: candle green/red must stay unmistakable, so
        # the S&R lines use a different hue family and stay thin — only the
        # right-edge label is fully opaque.
        for lvl in l2_data.get("sr") or []:
            _c = "#42a5f5" if lvl["side"] == "support" else "#ffa726"
            fig.add_shape(
                type="line", xref="x domain", x0=0, x1=1,
                yref="y", y0=lvl["price"], y1=lvl["price"],
                line=dict(color=_c, width=1.8 + 1.4 * lvl["score"], dash="dash"),
                opacity=0.42 + 0.33 * lvl["score"], layer="above",
            )
            # Bookmap-style: show the wall's thickness (avg resting shares
            # while present) next to the price, so "how big is this level"
            # is readable without hovering.
            _sz = lvl.get("size")
            if _sz and _sz >= 1e6:
                _sz_txt = f" · {_sz/1e6:.1f}M"
            elif _sz and _sz >= 1e3:
                _sz_txt = f" · {_sz/1e3:.0f}K"
            elif _sz:
                _sz_txt = f" · {_sz:.0f}"
            else:
                _sz_txt = ""
            fig.add_annotation(
                xref="x domain", x=0.999, xanchor="right",
                yref="y", y=lvl["price"], yanchor="bottom",
                text=f"{'S' if lvl['side'] == 'support' else 'R'} {lvl['price']:.2f}{_sz_txt}",
                showarrow=False, font=dict(size=9, color=_c),
            )

    # Step 3: Source-aware annotations — shade wick-estimated data regions.
    _add_source_annotations(fig, frames)

    return fig


def show_chart(ticker: str = "NVDA", save_html: bool = True, auto_fetch: bool = True):
    ticker = ticker.upper()
    df_1min, frames = run_pipeline(ticker)

    if (df_1min.empty or not frames) and auto_fetch:
        print(f"[Visualizer] No data for {ticker} — fetching from FinViz...")
        try:
            from finviz.new_finviz import fetch_and_save
            fetch_and_save(ticker, timeframe="i1")
            df_1min, frames = run_pipeline(ticker)
        except Exception as e:
            print(f"[Visualizer] Auto-fetch failed: {e}")

    if df_1min.empty or not frames:
        print(f"[Visualizer] No data for {ticker}. Aborting.")
        return

    fig = build_chart(df_1min, frames, ticker)

    if save_html:
        path = "chart.html"
        write_chart_html(fig, path)
        print(f"[Visualizer] Saved → {path}")
        
        import webbrowser
        from pathlib import Path
        # as_uri(), not an f-string: on Windows the absolute path is
        # C:\\Users\\..., and "file://" + that keeps the backslashes and is one
        # slash short, so the browser gets a malformed URL and opens nothing.
        webbrowser.open(Path(path).resolve().as_uri())
    else:
        fig.show()


if __name__ == "__main__":
    import sys
    ticker = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
    show_chart(ticker)
