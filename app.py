import dash
from dash import dcc, html, Input, Output, State, ctx
import dash_bootstrap_components as dbc
from cvd.calculator import run_pipeline, TIMEFRAME_RULE_IBKR, TIMEFRAME_RULE
from cvd.visualizer import build_chart, MAX_CANDLES, pie_layout
from history.schema import SERVE_TIER, SERVE_WINDOW_DAYS
from history.serve import run_pipeline_tiered, invalidate_cache
from level2_webapp.data_provider import fetch_and_aggregate_l2_data, compute_support_resistance
from finviz.errors import FinvizNotConfigured
import plotly.graph_objects as go
import numpy as np
import pandas as pd
import logging
import json
import os
import time
from dash.exceptions import PreventUpdate

logging.basicConfig(level=logging.INFO)
# Errors/timings also go to a rotating file so tracebacks survive terminal
# scrollback (the one-shot "'<' not supported ..." startup error was lost
# because stdout was the only sink). logs_*.log is gitignored.
from logging.handlers import RotatingFileHandler
_fh = RotatingFileHandler("logs_app.log", maxBytes=5_000_000, backupCount=2)
_fh.setLevel(logging.INFO)
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logging.getLogger().addHandler(_fh)

app = dash.Dash(__name__, external_stylesheets=[dbc.themes.CYBORG])

app.index_string = '''
<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <title>{%title%}</title>
        {%favicon%}
        {%css%}
    </head>
    <body>
        {%app_entry%}
        <footer>
            {%config%}
            {%scripts%}
            <script>
                window.dash_clientside = window.dash_clientside || {};
                window.dash_clientside.clientside = window.dash_clientside.clientside || {};

                // Y Auto-Scale checkbox state (manual mode when unchecked).
                window.__yAutoOn = function() {
                    var el = document.querySelector('#yauto-check input[type="checkbox"]');
                    return el ? el.checked : true;
                };

                // Manual mode: every user y movement sticks as-is — plot-area
                // pan, price-axis drag/wheel, all of it. Persistence across
                // rebuilds is handled by the manual-y Store (track_manual_y
                // below). The only interception left is double-click, which
                // Plotly would turn into a plain trace autorange: route it
                // through __requestRefit so the one-shot fit uses our own
                // logic (L2 band inclusion, visible-window scoping); the
                // refit's relayout then lands in the Store like any other.
                window.__guardManualY = function(gd, e) {
                    if (window.__yAutoOn() || window.__is_refitting || !e) return;
                    var keys = Object.keys(e);
                    if (keys.some(function(k) { return k.indexOf('autorange') !== -1; })) {
                        window.__requestRefit();
                    }
                };

                // Debounced y-axis refit, shared by the Dash clientside callback
                // (pan / data refresh) and the plotly_restyle hook below
                // (legend toggles, which Dash does not expose as a prop).
                // onlyAxes (optional): array of layout axis names — restricts
                // the fit to those panels (manual-mode legend toggles refit
                // ONLY the panel whose trace changed).
                window.__requestRefit = function(onlyAxes) {
                    if (window.__refit_timer) {
                        clearTimeout(window.__refit_timer);
                    }

                    window.__refit_timer = setTimeout(function() {
                        if (window.__is_refitting) {
                            return; // Wait until current refit is done
                        }

                        var wrapper = document.getElementById('main-chart');
                        if (!wrapper) return;
                        var gd = wrapper.classList.contains('js-plotly-plot') ? wrapper : wrapper.querySelector('.js-plotly-plot');
                        if (!gd || !gd._fullLayout) return;
                        
                        var YPAD = 0.15;
                        var LEFT = {'y':'yaxis', 'y2':'yaxis2', 'y4':'yaxis4'};
                        // Linear-interpolated quantile of a pre-sorted array.
                        var __qtile = function(sorted, q) {
                            var n = sorted.length;
                            if (n === 0) return NaN;
                            if (n === 1) return sorted[0];
                            var idx = q * (n - 1), lo = Math.floor(idx), hi = Math.ceil(idx);
                            if (lo === hi) return sorted[lo];
                            return sorted[lo] + (sorted[hi] - sorted[lo]) * (idx - lo);
                        };
                        var ax = gd._fullLayout.xaxis;
                        if (!ax || !ax.range) return;
                        var xr = [ax.range[0], ax.range[1]];

                        // ── Rebase each CVD line to 0 at the visible left edge ──
                        // The CVD variants (all-time / raw / BVC / wick) start at
                        // very different levels, so on one axis they squash each
                        // other. Shifting every CVD trace by its own value at the
                        // leftmost VISIBLE bar makes them all start at 0 for the
                        // current view, so the y-fit below frames their SHAPES
                        // instead of the gaps between them. Originals are stashed
                        // per figure (re-captured when the server sends a fresh
                        // one — see refit_y's __cvdStale) so repeated pans always
                        // shift from raw values, never cumulatively.
                        if (gd.__cvdStale || !gd.__cvdOrig) {
                            gd.__cvdOrig = {};
                            gd._fullData.forEach(function(f, i) {
                                if (f && f.name && f.name.indexOf('CVD') === 0 && f.y) {
                                    gd.__cvdOrig[i] = Array.prototype.slice.call(f.y);
                                }
                            });
                            gd.__cvdStale = false;
                        }
                        var rebaseY = [], rebaseIdx = [];
                        Object.keys(gd.__cvdOrig).forEach(function(k) {
                            var i = +k, orig = gd.__cvdOrig[i], f = gd._fullData[i];
                            if (!orig || !f) return;
                            var off = (f.x0 != null ? f.x0 : 0);
                            var i0 = Math.max(0, Math.floor(xr[0] - off));
                            var i1 = Math.min(orig.length - 1, Math.ceil(xr[1] - off));
                            if (i0 > i1) return;
                            var base = null;
                            for (var j = i0; j <= i1; j++) {
                                var v = orig[j];
                                if (v != null && !isNaN(v)) { base = v; break; }
                            }
                            if (base === null) return;      // no visible point → leave as-is
                            var arr = new Array(orig.length);
                            for (var j = 0; j < orig.length; j++) {
                                var v = orig[j];
                                arr[j] = (v == null || isNaN(v)) ? v : (v - base);
                            }
                            rebaseIdx.push(i); rebaseY.push(arr);
                        });

                        var doFit = function() {
                        var upd = {};
                        Object.keys(LEFT).forEach(function(yref) {
                            if (onlyAxes && onlyAxes.indexOf(LEFT[yref]) === -1) return;
                            var vals = [];                           // candles + lines (primary) visible y-values
                            var hlo = Infinity, hhi = -Infinity;     // L2 heatmap levels (secondary)
                            gd._fullData.forEach(function(f) {
                                if (f.visible === false || f.visible === 'legendonly') return;
                                if ((f.yaxis || 'y') !== yref) return;
                                if (f.type === 'pie') return;
                                // L2 heatmap: y is a price-LEVEL axis, not a
                                // per-bar series. Include only the levels that
                                // hold meaningful liquidity (>=15% of the
                                // trace zmax) in the visible x window, so the
                                // bands stay on screen without a faint stray
                                // level stretching the whole axis.
                                if (f.type === 'heatmap') {
                                    if (!f.x || !f.x.length || !f.z || !f.y) return;
                                    var h0 = Math.max(0, Math.floor(xr[0] - f.x[0]));
                                    var h1 = Math.min(f.x.length - 1, Math.ceil(xr[1] - f.x[0]));
                                    var thr = 0.15 * (f.zmax || 0);
                                    if (h0 > h1 || !(thr > 0)) return;
                                    for (var r = 0; r < f.z.length; r++) {
                                        var zrow = f.z[r], m = 0;
                                        for (var c = h0; c <= h1; c++) if (zrow[c] > m) m = zrow[c];
                                        if (m >= thr) {
                                            var yv = f.y[r];
                                            // Heatmap goes to its OWN bounds, not
                                            // the candle bounds — it may only
                                            // extend the axis by a small margin
                                            // (applied after the loop) so a far
                                            // liquidity wall can't squish candles.
                                            if (yv < hlo) hlo = yv;
                                            if (yv > hhi) hhi = yv;
                                        }
                                    }
                                    return;
                                }
                                // Traces may carry an explicit x array OR the
                                // compact x0/dx form (dx is always 1 here).
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
                                        if (lows[i]  != null && !isNaN(lows[i]))  vals.push(lows[i]);
                                        if (highs[i] != null && !isNaN(highs[i])) vals.push(highs[i]);
                                    }
                                } else if (f.y) {
                                    var ys = f.y;
                                    for (var i = i0; i <= i1; i++) {
                                        var v = ys[i];
                                        if (v == null || isNaN(v)) continue;
                                        vals.push(v);
                                    }
                                }
                            });
                            var lo = Infinity, hi = -Infinity;
                            if (vals.length) {
                                vals.sort(function(a, b) { return a - b; });
                                lo = vals[0];
                                hi = vals[vals.length - 1];
                                // Outlier-robust bounds: a lone bad-print wick
                                // would otherwise stretch the axis and crush the
                                // candles into a sliver. Clip lo/hi to a Tukey
                                // fence (Q1-3·IQR, Q3+3·IQR) — k=3 is the "far
                                // out" fence so only true extremes are dropped;
                                // when there are no outliers the fence sits
                                // outside the data and nothing is clipped. Needs
                                // enough points for a stable IQR.
                                // PRICE PANEL ONLY (yref 'y'): the indicator
                                // panels (y2/y4) use plain min–max so an auction
                                // or block bar's real magnitude is shown in full.
                                if (yref === 'y' && vals.length >= 8) {
                                    var q1 = __qtile(vals, 0.25), q3 = __qtile(vals, 0.75), iqr = q3 - q1;
                                    if (iqr > 0) {
                                        lo = Math.max(lo, q1 - 3.0 * iqr);
                                        hi = Math.min(hi, q3 + 3.0 * iqr);
                                    }
                                }
                            }
                            if (lo < hi && lo !== Infinity) {
                                // Candles/lines set the scale. Let the L2 heatmap
                                // widen it by at most HEAT_MARGIN of the candle
                                // span, so a wall parked near price still shows
                                // but a far one doesn't blow up the axis (empty
                                // space above, candles crushed into a sliver).
                                if (hlo !== Infinity) {
                                    var HEAT_MARGIN = 0.15;
                                    var m = (hi - lo) * HEAT_MARGIN;
                                    lo = Math.min(lo, Math.max(hlo, lo - m));
                                    hi = Math.max(hi, Math.min(hhi, hi + m));
                                }
                                // The two S&R lines are LAYOUT SHAPES (not traces),
                                // so the loop above never sees them. They are the
                                // key trade signal and the depth selector may clip a
                                // wall out of the heatmap, so pull the price-panel
                                // S&R line prices into the fit UNCONDITIONALLY (not
                                // capped like the heatmap) — otherwise this
                                // client-side refit re-crops the axis to candles +
                                // clipped heatmap right after the server render and
                                // hides the far wall (the "only one S&R line shows
                                // at 10 levels" bug). On the price panel (yref 'y')
                                // the only line shapes are the S&R lines; the source
                                // shading / demarcation shapes use yref 'paper'.
                                if (yref === 'y' && gd._fullLayout.shapes) {
                                    gd._fullLayout.shapes.forEach(function(sh) {
                                        if (sh && sh.type === 'line' &&
                                            (sh.yref === 'y' || sh.yref === 'y1') &&
                                            sh.y0 != null) {
                                            if (sh.y0 < lo) lo = sh.y0;
                                            if (sh.y0 > hi) hi = sh.y0;
                                        }
                                    });
                                }
                                var pad = (hi - lo) * YPAD;
                                var new_lo = lo - pad;
                                var new_hi = hi + pad;

                                // Relative threshold: the old absolute 0.05 test always
                                // failed for CVD values in the millions, forcing a full
                                // re-render (Plotly.relayout) after every data refresh.
                                var span = new_hi - new_lo;
                                var old_range = gd._fullLayout[LEFT[yref]] ? gd._fullLayout[LEFT[yref]].range : null;
                                if (!old_range || Math.abs(old_range[0] - new_lo) > span * 0.02 || Math.abs(old_range[1] - new_hi) > span * 0.02) {
                                    upd[LEFT[yref] + '.range'] = [new_lo, new_hi];
                                }
                            }
                        });
                        
                            return Object.keys(upd).length > 0 ? Plotly.relayout(gd, upd) : null;
                        };

                        // Apply the CVD rebase first (if any), then the y-fit reads
                        // the shifted arrays. __is_refitting stays true across both
                        // so the restyle/relayout we emit don't re-enter this refit.
                        var finish = function() { window.__is_refitting = false; };
                        if (rebaseIdx.length) {
                            window.__is_refitting = true;
                            Plotly.restyle(gd, {y: rebaseY}, rebaseIdx)
                                .then(doFit).then(finish).catch(finish);
                        } else {
                            var r = doFit();
                            if (r) { window.__is_refitting = true; r.then(finish).catch(finish); }
                        }
                    }, 60);
                };

                // Relayout debounce → dcc.Store('settled-relayout'). A zoom or
                // pan gesture fires many intermediate relayout events; the heavy
                // server callbacks (pie reposition, days-to-load) and bars growth
                // only need the FINAL settled view. Each call captures its own
                // relayoutData and arms a timer; a newer event clears the prior
                // timer, so only the last gesture's promise resolves (~160ms after
                // motion stops). Earlier promises are left unresolved (harmless —
                // Dash simply never applies their output).
                window.dash_clientside.clientside.debounce_relayout = function(relayoutData) {
                    if (!relayoutData || !Object.keys(relayoutData).length) {
                        return window.dash_clientside.no_update;
                    }
                    if (window.__settleTimer) { clearTimeout(window.__settleTimer); }
                    return new Promise(function(resolve) {
                        window.__settleTimer = setTimeout(function() {
                            window.__settleTimer = null;
                            resolve(relayoutData);
                        }, 160);
                    });
                };

                // Progressive scrollback growth (bars-to-show doubling) runs
                // CLIENTSIDE: a server callback on relayoutData gets aborted
                // whenever the y-refit fires a newer relayout event, silently
                // eating the growth. Constants mirror app.py DEFAULT_BARS /
                // BARS_HARD_CAP — keep them in sync.
                window.dash_clientside.clientside.grow_scrollback = function(relayoutData, currentBars, lastStateJson) {
                    var nu = window.dash_clientside.no_update;
                    if (!relayoutData) return nu;
                    var x0 = null, x1 = null;
                    if (relayoutData['xaxis.range[0]'] !== undefined && relayoutData['xaxis.range[1]'] !== undefined) {
                        x0 = +relayoutData['xaxis.range[0]']; x1 = +relayoutData['xaxis.range[1]'];
                    } else if (Array.isArray(relayoutData['xaxis.range'])) {
                        x0 = +relayoutData['xaxis.range'][0]; x1 = +relayoutData['xaxis.range'][1];
                    } else return nu;
                    var st = {};
                    try { st = JSON.parse(lastStateJson || '{}'); } catch(e) { return nu; }
                    var n = st.n_active, tf = st.active_tf;
                    if (!n || !tf) return nu;
                    var atLeft = x0 <= 10;
                    var zoomedOut = (x1 - x0) >= 0.95 * n;
                    if (!atLeft && !zoomedOut) return nu;
                    var DEFAULT_BARS = 1000, HARD_CAP = 6000;   // keep in sync with app.py BARS_HARD_CAP
                    var cur = DEFAULT_BARS;
                    if (currentBars && typeof currentBars === 'object') {
                        cur = (currentBars.tf === tf) ? (currentBars.bars || DEFAULT_BARS) : DEFAULT_BARS;
                    } else if (currentBars) { cur = +currentBars; }
                    if (n < cur) return nu;   // data-limited: the server grows days-to-load instead
                    var next = Math.min(cur * 2, HARD_CAP);
                    if (next <= cur) return nu;
                    return {'bars': next, 'tf': tf, 'x0': x0, 'x1': x1};
                };

                // Manual-mode y tracker → dcc.Store('manual-y'). The server
                // re-applies these ranges on every rebuild (uirevision does
                // NOT protect API/relayout edits, only true GUI drags), the
                // same explicit-reapply pattern the x window already uses.
                // EVERY event carrying y ranges counts — plot-area pans
                // (x+y) included: with the checkbox off, wherever the user
                // moves y is the new manual scale.
                window.dash_clientside.clientside.track_manual_y = function(relayoutData, yautoValue, current) {
                    var nu = window.dash_clientside.no_update;
                    var trg = dash_clientside.callback_context.triggered;
                    if (trg && trg.length && trg[0].prop_id === 'yauto-check.value') {
                        return null;   // mode flip: stale manual ranges must not resurface
                    }
                    if (window.__yAutoOn() || !relayoutData) return nu;
                    var out = (current && typeof current === 'object') ? Object.assign({}, current) : {};
                    var touched = false;
                    ['yaxis', 'yaxis2', 'yaxis4'].forEach(function(ax) {
                        if (relayoutData[ax + '.range[0]'] !== undefined) {
                            out[ax] = [+relayoutData[ax + '.range[0]'], +relayoutData[ax + '.range[1]']];
                            touched = true;
                        } else if (Array.isArray(relayoutData[ax + '.range'])) {
                            out[ax] = relayoutData[ax + '.range'].slice();
                            touched = true;
                        }
                    });
                    return touched ? out : nu;
                };

                window.dash_clientside.clientside.refit_y = function(relayoutData, figureData) {
                    // Triggered by EITHER user pan (relayoutData) OR data refresh (figureData)

                    var trigger = dash_clientside.callback_context.triggered;
                    var isFigUpdate = trigger && trigger.length > 0 && trigger[0].prop_id === 'main-chart.figure';

                    if (isFigUpdate) {
                        // Fresh figure from the server → its CVD y-arrays are raw
                        // cumulative values again; mark the rebase stash stale so
                        // __requestRefit re-captures the originals before shifting.
                        var _w = document.getElementById('main-chart');
                        var _g = _w && (_w.classList.contains('js-plotly-plot') ? _w : _w.querySelector('.js-plotly-plot'));
                        if (_g) _g.__cvdStale = true;
                    }

                    // Manual mode: no continuous refits — the server only
                    // rewrites y on fresh views and otherwise re-applies the
                    // manual-y Store, so there is nothing to do here.
                    if (!window.__yAutoOn()) {
                        return window.dash_clientside.no_update;
                    }

                    if (!isFigUpdate) {
                        if (!relayoutData) return window.dash_clientside.no_update;
                        var keys = Object.keys(relayoutData);
                        var hasX = keys.some(function(k) { return k.indexOf('xaxis') === 0; });
                        if (!hasX) return window.dash_clientside.no_update;
                    }

                    window.__requestRefit();
                    return window.dash_clientside.no_update;
                };

                // Legend toggles fire plotly_restyle, which Dash has no Input
                // for — bind directly so showing/hiding a trace (e.g. the CVD
                // variants) refits the y-axes to what is actually visible.
                // Rebinds automatically if the graph node is ever re-created.
                setInterval(function() {
                    var wrapper = document.getElementById('main-chart');
                    if (!wrapper) return;
                    var gd = wrapper.classList.contains('js-plotly-plot') ? wrapper : wrapper.querySelector('.js-plotly-plot');
                    if (!gd || !gd.on || gd.__restyleBound) return;
                    gd.__restyleBound = true;
                    gd.on('plotly_restyle', function(ev) {
                        if (window.__is_refitting) return;
                        if (window.__yAutoOn()) { window.__requestRefit(); return; }
                        // Manual mode: legend toggles refit ONLY the panel the
                        // toggled trace lives on — candle/other panels keep
                        // their manual scale.
                        var axes = {};
                        var idx = (ev && ev[1]) || [];
                        idx.forEach(function(i) {
                            var t = gd._fullData[i];
                            if (!t) return;
                            var ax = {'y': 'yaxis', 'y2': 'yaxis2', 'y4': 'yaxis4'}[t.yaxis || 'y'];
                            if (ax) axes[ax] = true;
                        });
                        var list = Object.keys(axes);
                        if (list.length) {
                            window.__requestRefit(list);
                        }
                    });
                    // Manual-mode double-click: route autorange through the
                    // one-shot custom refit (all other y moves stick as-is).
                    gd.on('plotly_relayout', function(e) { window.__guardManualY(gd, e); });
                }, 1000);
            </script>
            {%renderer%}
        </footer>
    </body>
</html>
'''

app.layout = html.Div([
    dbc.NavbarSimple(
        brand="Trading CVD Bubble Dashboard",
        brand_href="#",
        color="dark",
        dark=True,
        className="mb-3",
        style={"borderBottom": "1px solid #222", "boxShadow": "0 4px 10px rgba(0,0,0,0.5)"}
    ),
    
    dbc.Container([
        dbc.Row([
            dbc.Col([
                html.Label("Search Ticker:", style={"fontWeight": "bold", "color": "#eee"}),
                dbc.Input(
                    id='ticker-input', 
                    value='NVDA', 
                    type='text', 
                    debounce=True, 
                    placeholder="Enter Ticker & hit Enter...",
                    style={"color": "#000", "backgroundColor": "white"}
                )
            ], width=2),
            
            dbc.Col([
                html.Label("Base Data Source:", style={"fontWeight": "bold", "color": "#eee"}),
                dcc.RadioItems(
                    id='source-radio',
                    options=[
                        {'label': ' Tiered: IBKR ticks + history (default)', 'value': 'raw_tick'},
                        {'label': ' 1-Min Base (FinViz legacy)', 'value': 'i1'}
                    ],
                    value='raw_tick',
                    inline=True,
                    className="mt-1",
                    labelStyle={"marginRight": "12px", "color": "#ccc"}
                )
            ], width=3),
            
            dbc.Col([
                html.Label("Active Timeframe:", style={"fontWeight": "bold", "color": "#eee"}),
                dcc.Dropdown(
                    id='timeframe-dropdown',
                    options=[], # Populated by callback
                    value='1min',
                    clearable=False,
                    style={"color": "#000", "minWidth": "90px"}
                )
            ], width=3),
            
            dbc.Col([
                html.Button("Manual Refresh", id="refresh-btn", className="btn btn-outline-info btn-sm mt-4 w-100")
            ], width=1),
            
            dbc.Col([
                html.Div(id='last-updated-text', className="mt-4 text-muted text-end", style={"fontSize": "14px", "marginRight": "10px"})
            ], width=3),
            
            # Isolated Loading Spinner (does not wrap the main chart)
            dbc.Col([
                html.Div([
                    dcc.Loading(
                        id="loading-spinner",
                        type="circle",
                        color="#29b6f6",
                        children=html.Div(id='loading-dummy', style={"width": "30px", "height": "30px"})
                    )
                ], className="mt-3")
            ], width=1)
        ], className="mb-2 align-items-center"),
        
        dbc.Row([
            dbc.Col([
                html.Label("Auto Refresh:", style={"fontWeight": "bold", "color": "#eee", "marginRight": "10px"}),
                dcc.Dropdown(
                    id='refresh-interval-dropdown',
                    options=[
                        {'label': 'Off', 'value': 0},
                        {'label': '5 sec', 'value': 5000},
                        {'label': '10 sec', 'value': 10000},
                        {'label': '20 sec', 'value': 20000},
                        {'label': '30 sec', 'value': 30000},
                        {'label': '60 sec', 'value': 60000}
                    ],
                    # Default 20s: each refresh rebuilds the whole figure (a brief
                    # freeze), so a slower default keeps interaction smooth while
                    # staying live enough. Users can pick 5/10s for faster ticks.
                    value=20000,
                    clearable=False,
                    style={"color": "#000", "width": "120px", "display": "inline-block", "marginRight": "20px"}
                ),
                dcc.Checklist(
                    id='yauto-check',
                    options=[{'label': ' Y Auto-Scale', 'value': 'on'}],
                    value=['on'],
                    labelStyle={"color": "white", "fontWeight": "bold"},
                    style={"display": "inline-block", "marginRight": "20px"}
                ),
                html.Label("Z-Score Bubbles:", style={"fontWeight": "bold", "color": "#eee", "marginRight": "10px"}),
                dcc.Dropdown(
                    id='bubbles-dropdown',
                    options=[
                        {'label': 'On', 'value': True},
                        {'label': 'Off', 'value': False}
                    ],
                    value=False,
                    clearable=False,
                    style={"color": "#000", "width": "100px", "display": "inline-block", "marginRight": "20px"}
                ),
                html.Label("L2 Depth:", style={"fontWeight": "bold", "color": "#eee", "marginRight": "10px"}),
                dcc.Dropdown(
                    id='l2-dropdown',
                    options=[
                        {'label': 'Off', 'value': 0},
                        {'label': '10 levels', 'value': 10},
                        {'label': '20 levels', 'value': 20},
                        {'label': 'Full book', 'value': 999},
                    ],
                    value=0,
                    clearable=False,
                    style={"color": "#000", "width": "130px", "display": "inline-block", "marginRight": "20px"}
                ),
                html.Label("Volume Pies (Bottom):", style={"fontWeight": "bold", "color": "#eee", "marginRight": "10px"}),
                dcc.Dropdown(
                    id='pie-chart-dropdown',
                    # On/off only: the strip auto-sizes to ~25 equal-width pies
                    # (each covering the same number of bars) for the current
                    # zoom, so there is nothing for the user to tune.
                    options=[
                        {'label': 'Off', 'value': 0},
                        {'label': 'On', 'value': 1}
                    ],
                    value=0,  # Default to Off
                    clearable=False,
                    style={"color": "#000", "width": "150px", "display": "inline-block"}
                )
            ], width=12, className="d-flex align-items-center justify-content-end")
        ], className="mb-3"),

        # Date/time jump: load a fixed ±1500-bar window around a pinned instant
        # (ET, as stored) instead of the live tail. Empty + Live = normal mode.
        dbc.Row([
            dbc.Col([
                html.Label("Jump to (ET):", style={"fontWeight": "bold", "color": "#eee", "marginRight": "10px"}),
                dcc.Input(
                    id='anchor-input', type='text', debounce=False,
                    placeholder='YYYY-MM-DD HH:MM',
                    style={"width": "180px", "marginRight": "8px"}
                ),
                html.Button("Jump", id="anchor-go", n_clicks=0,
                            className="btn btn-outline-warning btn-sm", style={"marginRight": "6px"}),
                html.Button("Live", id="anchor-clear", n_clicks=0,
                            className="btn btn-outline-success btn-sm"),
                html.Span(id="anchor-status", style={"color": "#ffa726", "marginLeft": "12px", "fontSize": "0.85em"}),
            ], width=12, className="d-flex align-items-center justify-content-end")
        ], className="mb-2"),

        # Main Chart Area
        html.Div([
            dcc.Graph(
                id='main-chart',
                style={'height': '1100px'},
                config={'scrollZoom': True, 'displayModeBar': False}
            )
        ], style={
            "padding": "10px", 
            "backgroundColor": "rgba(10, 10, 10, 0.8)", 
            "backdropFilter": "blur(15px)",
            "borderRadius": "12px",
            "border": "1px solid rgba(255, 255, 255, 0.1)",
            "boxShadow": "0 8px 32px 0 rgba(0, 0, 0, 0.5)"
        }),
        
        dcc.Interval(
            id='interval-component',
            interval=20 * 1000,   # matches the 20s dropdown default
            n_intervals=0
        ),
        
        # Dummy div for clientside callback to prevent invalid ID errors
        html.Div(id='clientside-dummy', style={'display': 'none'}),
        
        # State tracking stores
        dcc.Store(id='last-data-state', data='{}'),
        dcc.Store(id='days-to-load', data=3),
        # Progressive scrollback: bars rendered per frame. Starts at the
        # visualizer default (1,000 — cheap re-renders); doubles when the user
        # pans to the left edge or zooms out, up to BARS_HARD_CAP.
        dcc.Store(id='bars-to-show', data=None),
        dcc.Store(id='manual-y', data=None),
        # Debounced relayout: mirrors main-chart.relayoutData but only after a
        # zoom/pan gesture SETTLES (~160ms of quiet). The heavy server callbacks
        # (pie repositioning, days-to-load growth) and the bars-to-show growth
        # listen to THIS instead of the raw per-frame relayout stream, so one
        # gesture triggers one rebuild instead of a per-frame callback storm
        # (which locked the UI once the SVG candle count grew on zoom-out).
        dcc.Store(id='settled-relayout', data=None),
        # Anchor (date/time jump): a datetime string pins a fixed ±N-bar
        # historical window; None = live tail. Set by the Jump/Live buttons.
        dcc.Store(id='anchor-active', data=None),
        dcc.Store(id='pan-state', data='{"panned": false, "time": 0}')
        
        
    ], fluid=True, style={"padding": "0 2% 50px 2%"})
], style={"backgroundColor": "#0d0d0d", "minHeight": "100vh"})


# ── Callbacks ──

@app.callback(
    [Output('timeframe-dropdown', 'options'),
     Output('timeframe-dropdown', 'value')],
    [Input('source-radio', 'value')],
    [State('timeframe-dropdown', 'value')]
)
def update_timeframes(base_tf, current_value):
    # The available timeframes depend only on the data source (tick vs FinViz).
    if base_tf == 'raw_tick':
        tfs = list(TIMEFRAME_RULE_IBKR.keys())
        new_value = "1min" if current_value not in tfs else current_value
    else:
        tfs = list(TIMEFRAME_RULE.keys())
        new_value = current_value if current_value in tfs else "1hr"

    options = [{'label': t, 'value': t} for t in tfs]
    return options, new_value


# Global state to prevent spamming FinViz fetches
import time
last_finviz_fetch = {}
# Per-ticker cooldown on the background IBKR backfill spawn. update_graph fires
# on every polling interval, and each backfill process connects to IB Gateway on
# the same clientId (=11) which Gateway REJECTS as a duplicate — so an unthrottled
# spawn just piles up failing processes. A 1-day backfill runs ~65s (see DONE.md).
last_backfill = {}
BACKFILL_COOLDOWN_SEC = 120

# Ticker-validity cache. When a fresh ticker loads empty we probe FinViz once to
# tell a valid-but-still-backfilling symbol from an unknown one, and remember the
# verdict so the 10-s poll doesn't re-probe every tick. A ticker that later
# collects real data drops out of the empty path entirely, so a False here is
# only ever set for symbols FinViz doesn't recognize.
_symbol_valid_cache = {}


def _ticker_is_valid(ticker: str) -> bool:
    """Cached wrapper around finviz.symbol_exists (see there). Defaults to True
    on any error so a real ticker is never wrongly flagged as unknown."""
    if ticker in _symbol_valid_cache:
        return _symbol_valid_cache[ticker]
    try:
        from finviz.new_finviz import symbol_exists
        ok = bool(symbol_exists(ticker))
    except Exception as e:
        logging.warning(f"symbol validity probe failed for {ticker}: {e}")
        ok = True
    _symbol_valid_cache[ticker] = ok
    return ok


def _clip_l2_levels(y_levels, z, z_bid, mid, n_each_side):
    """Keep only the `n_each_side` price levels nearest `mid` on each side of the
    L2 heatmap — the "L2 Depth" selector (10 / 20 / Full book) is a Bookmap-style
    depth zoom. A falsy/large n_each_side, or a book already shorter than the
    request, is returned unchanged (Full book uses a large sentinel)."""
    if not y_levels or not n_each_side:
        return y_levels, z, z_bid
    arr = np.asarray(y_levels, dtype=float)
    if n_each_side * 2 + 1 >= len(arr):
        return y_levels, z, z_bid
    ci = int(np.abs(arr - mid).argmin())          # level nearest the current mid
    lo = max(0, ci - n_each_side)
    hi = min(len(arr), ci + n_each_side + 1)
    zc = z[lo:hi, :] if z is not None else None
    zbc = z_bid[lo:hi, :] if z_bid is not None else None
    return list(arr[lo:hi]), zc, zbc


DATA_CACHE = {} # Cache for fast pie chart HUD updates

# Progressive scrollback bounds: rendering is O(bars) in Plotly SVG, so the
# default stays small and the user "buys" deeper scrollback by panning left /
# zooming out (bars-to-show doubles per hit, never past the hard cap).
DEFAULT_BARS = MAX_CANDLES          # 1,000
# Worst-case render cap when the user pans/zooms far back. Render cost is O(bars)
# in Plotly's SVG candlestick, so this bounds the deepest-scrollback jank. Raise
# it for more history at the cost of a slower far-left view. Keep in sync with the
# HARD_CAP constant in the grow_scrollback clientside function (index_string).
BARS_HARD_CAP = 6000

# Anchor (date/time jump) mode: bars loaded on EACH side of the pinned instant.
ANCHOR_BARS_EACH_SIDE = 1500
# Initial on-screen width (bars) for an anchor jump, centered on the pinned bar
# so the exact typed instant sits mid-screen rather than at a window edge.
ANCHOR_VIEW_BARS = 200


def _parse_anchor(val):
    """Parse the anchor-active Store into an ET-naive datetime, or None (live).
    Accepts 'YYYY-MM-DD HH:MM[:SS]' or 'YYYY-MM-DD' (ET, as stored in Mongo)."""
    if not val or not isinstance(val, str):
        return None
    from datetime import datetime
    s = val.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None

def _resolve_bars(bars_to_show, active_tf) -> int:
    """Per-TF bars-to-show store value → effective render cap."""
    if isinstance(bars_to_show, dict):
        bars = bars_to_show.get('bars', DEFAULT_BARS) if bars_to_show.get('tf') == active_tf else DEFAULT_BARS
    else:
        bars = bars_to_show or DEFAULT_BARS
    return max(200, min(int(bars), BARS_HARD_CAP))

# Opportunistic incremental rollup: keeps the materialized tiers (1min/30min/
# 1day) fresh for the active ticker while collectors stream 1-sec bars, without
# requiring the standalone `python -m history.rollup --loop` worker to be up.
# Incremental passes only touch bars past the watermark, so this is cheap.
last_rollup = {}
ROLLUP_COOLDOWN_SEC = 60

# Fetch/rollup run OFF the request thread: a timeframe switch or interval tick
# must never wait on a FinViz HTTP round-trip (~1-3s) or a rollup pass. The
# chart renders from the tiers as-is; the next poll serves the merged bars.
import threading
_refresh_inflight = set()

def _spawn_data_refresh(ticker: str, fetch: bool):
    if ticker in _refresh_inflight:
        return

    _refresh_inflight.add(ticker)

    def work():
        try:
            if fetch:
                from finviz.new_finviz import fetch_and_save
                fetch_and_save(ticker, timeframe="i1")
            # Rollup is pandas/CPU-heavy: run it in a SUBPROCESS, not in this
            # thread. A thread shares the GIL with the web worker, and since
            # _maybe_rollup's cooldown resets exactly on fresh-view triggers,
            # the rollup used to crunch concurrently with the TF-switch
            # rebuild — measured 0.08s serve stretching to ~4.6s of "data"
            # stage. The subprocess exit is awaited here (blocking only this
            # daemon thread) so invalidate_cache still runs after the rollup.
            import subprocess
            import sys
            import os
            subprocess.run(
                [sys.executable, "-m", "history.rollup", "--ticker", ticker],
                cwd=os.path.dirname(os.path.abspath(__file__)),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=600,
            )
            invalidate_cache(ticker)
        except Exception as e:
            logging.warning(f"Background refresh failed for {ticker}: {e}")
        finally:
            _refresh_inflight.discard(ticker)

    threading.Thread(target=work, daemon=True).start()

def _maybe_rollup(ticker: str):
    now = time.time()
    if now - last_rollup.get(ticker, 0) < ROLLUP_COOLDOWN_SEC:
        return
    last_rollup[ticker] = now
    _spawn_data_refresh(ticker, fetch=False)

# On-demand tick collection: viewing a ticker in tiered mode upserts a request
# that ibkr/dynamic_collector.py (separate process, own clientId) picks up and
# turns into a live tick-by-tick subscription + catch-up 1sec backfill. The
# research collectors' tickers are excluded by the collector itself.
_last_collect_req = {}
COLLECT_REQ_COOLDOWN_SEC = 30

def _request_tick_collection(ticker: str):
    now = time.time()
    if now - _last_collect_req.get(ticker, 0) < COLLECT_REQ_COOLDOWN_SEC:
        return
    _last_collect_req[ticker] = now

    def work():
        try:
            from datetime import datetime, timezone
            from history.schema import mongo_client, DB_NAME
            mongo_client()[DB_NAME]["collector_requests"].update_one(
                {"_id": ticker},
                {"$set": {"last_requested": datetime.now(timezone.utc)}},
                upsert=True,
            )
        except Exception as e:
            logging.debug(f"collector request failed for {ticker}: {e}")

    threading.Thread(target=work, daemon=True).start()

@app.callback(
    [Output('interval-component', 'interval'),
     Output('interval-component', 'disabled')],
    [Input('refresh-interval-dropdown', 'value')]
)
def update_refresh_interval(val):
    if val == 0:
        return 10000, True
    return val, False

# Date/time jump: "Jump" pins the typed instant (anchor-active Store → a fixed
# ±ANCHOR_BARS_EACH_SIDE window in update_graph); "Live" clears it back to the
# live tail. The status span echoes the active pin.
@app.callback(
    [Output('anchor-active', 'data'),
     Output('anchor-status', 'children')],
    [Input('anchor-go', 'n_clicks'),
     Input('anchor-clear', 'n_clicks')],
    State('anchor-input', 'value'),
    prevent_initial_call=True,
)
def set_anchor(go_clicks, clear_clicks, anchor_text):
    trig = ctx.triggered_id
    if trig == 'anchor-clear':
        return None, ""
    dt = _parse_anchor(anchor_text)
    if dt is None:
        return None, "⚠ Use format YYYY-MM-DD HH:MM"
    return dt.strftime("%Y-%m-%d %H:%M:%S"), f"📌 {dt.strftime('%Y-%m-%d %H:%M')} (±{ANCHOR_BARS_EACH_SIDE} bars)"

@app.callback(
    [Output('main-chart', 'figure'),
     Output('last-updated-text', 'children'),
     Output('loading-dummy', 'children'),
     Output('last-data-state', 'data'),
     Output('pan-state', 'data')],
    [Input('ticker-input', 'value'),
     Input('source-radio', 'value'),
     Input('timeframe-dropdown', 'value'),
     Input('interval-component', 'n_intervals'),
     Input('refresh-btn', 'n_clicks'),
     Input('days-to-load', 'data'),
     Input('pie-chart-dropdown', 'value'),
     Input('bubbles-dropdown', 'value'),
     Input('l2-dropdown', 'value'),
     Input('yauto-check', 'value'),
     Input('bars-to-show', 'data'),
     Input('anchor-active', 'data')],
    # NOTE: deliberately NO State('main-chart', 'figure') here — that would
    # upload the entire multi-MB figure JSON from the browser on every
    # interval tick, which is what made the 10-s refresh feel slow.
    # relayoutData is tiny (just the last pan/zoom event) and gives the
    # freshest user window without waiting for the pan-state Store round-trip.
    [State('last-data-state', 'data'),
     State('pan-state', 'data'),
     State('main-chart', 'relayoutData'),
     State('manual-y', 'data')]
)
def update_graph(ticker, base_tf, active_tf, n_intervals, n_clicks, days_to_load, pie_chart_count, show_bubbles, show_l2, yauto_value, bars_to_show, anchor_active, last_state_json, pan_state_json, relayout_data, manual_y_store):
    trigger = ctx.triggered_id
    
    if not ticker:
        raise PreventUpdate
    
    ticker = str(ticker).strip().upper()
    logging.info(f"Dash update triggered by {trigger} for {ticker} ({base_tf}) TF: {active_tf} Days: {days_to_load}")
    _t_cb0 = time.monotonic()   # stage timing (logged at return)
    
    try:
        # Tier-aware serving flag (used for the serve path below AND to decide
        # whether tiered mode needs a fresh FinViz i1 fetch).
        tier_served = (base_tf == 'raw_tick' and active_tf in SERVE_TIER)

        # NOTE: the live-tick collection request used to fire here, before we
        # knew the ticker was real. Typing "INTC" then sent I/IN/INT/INTC and
        # each partial polluted collector_requests, churning the 3 tick slots
        # (and starving real tickers). The request now fires only AFTER data
        # loads successfully (see below the df_base.empty guard), so junk/partial
        # symbols — which never resolve to data — never enter the queue.

        # days-to-load is scoped to the timeframe it was grown on: panning
        # deep on one TF (up to 180 days) must not make every later timeframe
        # switch reload that much history.
        if isinstance(days_to_load, dict):
            days_req = float(days_to_load.get('days', 3)) if days_to_load.get('tf') == active_tf else 3.0
        else:
            days_req = float(days_to_load or 3)

        # Progressive scrollback: how many bars to render for this TF.
        bars_req = _resolve_bars(bars_to_show, active_tf)

        # Periodic FinViz fetch (every 60 seconds) or forced by Manual Refresh
        now = time.time()
        should_fetch_finviz = False

        if trigger == 'refresh-btn':
            should_fetch_finviz = True
        elif base_tf == 'i1' or (tier_served and SERVE_TIER[active_tf] in ('1sec', '1min')):
            # Tiered sub-daily charts need fresh FinViz 1-min bars too: the
            # rollup merges them into the 1min tier wherever no IBKR data
            # exists, so the current day stays dense (consolidated-tape bars)
            # even when the tick collector is down or lagging.
            if ticker not in last_finviz_fetch or (now - last_finviz_fetch[ticker]) > 60:
                should_fetch_finviz = True
                
        if should_fetch_finviz:
            last_finviz_fetch[ticker] = now
            if trigger == 'refresh-btn':
                # Manual refresh: the user explicitly wants fresh data NOW,
                # so this one waits for the fetch (and merge, in tiered mode).
                logging.info(f"Fetching latest FinViz data for {ticker}...")
                try:
                    from finviz.new_finviz import fetch_and_save
                    fetch_and_save(ticker, timeframe="i1")
                    if base_tf == 'raw_tick':
                        from history.rollup import rollup_ticker
                        rollup_ticker(ticker)
                        last_rollup[ticker] = now
                except Exception as e:
                    logging.error(f"Auto-fetch failed: {e}")
            else:
                # Periodic path: fetch + rollup in the background so a
                # timeframe switch never blocks on a FinViz HTTP round-trip.
                _spawn_data_refresh(ticker, fetch=True)
                last_rollup[ticker] = now

        # While the user is panned away from the live tail, an interval tick
        # cannot change anything visible (historical bars are immutable and
        # pies are patched client-side on pan) — skip the full reload and the
        # multi-second re-render. Background fetch/rollup above still ran, so
        # data keeps accumulating; the next tail view or view switch shows it.
        # All state here is per-client (Stores/relayoutData), so another open
        # browser tab on a different view can't defeat the check.
        if trigger == 'interval-component':
            try:
                _ps = json.loads(pan_state_json) if pan_state_json else {}
            except Exception:
                _ps = {}
            try:
                _ls = json.loads(last_state_json) if last_state_json else {}
            except Exception:
                _ls = {}
            if (_ps.get('panned')
                    and _ls.get('ticker') == ticker
                    and _ls.get('base_tf') == base_tf
                    and _ls.get('active_tf') == active_tf):
                _x1 = None
                if relayout_data and 'xaxis.range[1]' in relayout_data:
                    _x1 = float(relayout_data['xaxis.range[1]'])
                elif relayout_data and isinstance(relayout_data.get('xaxis.range'), (list, tuple)):
                    _x1 = float(relayout_data['xaxis.range'][1])
                elif 'x1' in _ps:
                    _x1 = float(_ps['x1'])
                n_active = _ls.get('n_active')
                # Never skip while a bars-to-show growth is pending delivery:
                # dash-renderer aborts an in-flight request when a newer one
                # (this interval tick) supersedes it, so this rebuild is the
                # grown figure's only ride to the client.
                if (_x1 is not None and n_active and _x1 < n_active - 2
                        and _ls.get('bars') == bars_req):
                    raise PreventUpdate

        # ── Tier-aware serving (history package) ────────────────────────────
        # Every timeframe except the raw tick view is served from its
        # materialized tier (1day chart reads ~2k daily rows, never 1-sec
        # bars). The raw_tick chart and the legacy FinViz-only radio keep the
        # old run_pipeline path.
        anchor_dt = None
        if tier_served:
            default_win = SERVE_WINDOW_DAYS.get(active_tf)
            actual_days = None if default_win is None else max(days_req, default_win)
            if trigger == 'refresh-btn':
                invalidate_cache(ticker)
            _maybe_rollup(ticker)
            # Anchor mode: a pinned datetime loads a fixed ±N-bar window around
            # that instant (a historical jump) instead of the live tail. Parsed
            # here from the anchor-active Store; falls back to live on any error.
            anchor_dt = _parse_anchor(anchor_active)
            if anchor_dt is not None:
                df_base, frames = run_pipeline_tiered(
                    ticker, active_tf, anchor=anchor_dt, bars_each_side=ANCHOR_BARS_EACH_SIDE)
                # The pinned bar sits at the CENTER of the ±N window; render the
                # WHOLE loaded window (never the live-tail cap) so build_chart's
                # iloc[-max_candles:] trim can't drop the anchor region. The
                # window is bounded (~2·N bars ≤ hard cap), so this stays sane.
                if active_tf in frames and len(frames[active_tf]):
                    bars_req = max(bars_req, len(frames[active_tf]))
            else:
                df_base, frames = run_pipeline_tiered(ticker, active_tf, days=actual_days)
        else:
            # Dynamic Lookback based on Timeframe (legacy path)
            min_days = 3
            if active_tf == '1month': min_days = 1000
            elif active_tf == '1week': min_days = 365
            elif active_tf == '1day': min_days = 180
            elif active_tf == '1hr': min_days = 30

            actual_days = max(days_req, min_days)
            if active_tf in ('1sec', 'raw_tick'):
                # The chart caps at 3,000 ticks (~minutes of trading), yet the
                # generic 3-day default reloaded ~190k tick docs on every 10-s
                # poll. Half a day is plenty unless the user panned deeper on
                # this exact timeframe (then days-to-load carries their intent).
                grown = isinstance(days_to_load, dict) and days_to_load.get('tf') == active_tf
                actual_days = days_req if grown else 0.5

            # The chart shows ONE timeframe — skip aggregating the other 11.
            tf_map = TIMEFRAME_RULE_IBKR if base_tf in ('1sec', 'raw_tick') else TIMEFRAME_RULE
            only = [active_tf]
            df_base, frames = run_pipeline(ticker, base_timeframe=base_tf, days=actual_days, only=only)
        
        # Fallback & Auto-Backfill Pipeline
        fallback_msg = ""
        # Distinguishes a real fetch failure (an exception was raised while
        # fetching FinViz/spawning the IBKR backfill) from the normal "data not
        # here YET" case — a brand-new 1sec ticker has its IBKR backfill running
        # asynchronously in the background, so an empty frame is "fetching", not
        # "failed". Set only in the except blocks below.
        fetch_error = None
        
        # Tiered path: a new/uncovered ticker gets instant history — FinViz
        # daily (~8y, one request) + FinViz i1 (recent 1-min) merged through
        # the rollup chain — then the tier query is retried.
        if tier_served and df_base.empty:
            try:
                from history.backfill_finviz_daily import backfill_daily
                from history.rollup import rollup_ticker
                logging.info(f"Tier empty for {ticker} — running instant FinViz backfill...")
                backfill_daily(ticker)
                if SERVE_TIER[active_tf] != '1day':
                    from finviz.new_finviz import fetch_and_save
                    fetch_and_save(ticker, timeframe='i1')
                rollup_ticker(ticker)
                invalidate_cache(ticker)
                df_base, frames = run_pipeline_tiered(ticker, active_tf, days=actual_days)
                fallback_msg = " [FinViz history backfilled]"
            except FinvizNotConfigured as e:
                # No FinViz account configured: an expected state, not a failure.
                # Leave fetch_error unset so the chart falls through to the
                # normal "no data for this range yet" message instead of a red
                # error banner — IBKR data (incl. the demo dataset) still draws.
                logging.info(f"FinViz backfill skipped for {ticker}: {e}")
            except Exception as e:
                logging.error(f"Instant FinViz backfill failed: {e}")
                fetch_error = e

        # Daily-tier coverage check: a ticker may have a few rolled-up days
        # (from i1/1sec) without ever having had its FinViz daily history
        # fetched — detect via backfill_meta and fill the ~8 years once.
        if tier_served and not df_base.empty and SERVE_TIER[active_tf] == '1day':
            from history.store import get_backfill_coverage
            cov_key = f"{ticker}:1day"
            if (get_backfill_coverage(ticker, '1day') is None
                    and now - last_backfill.get(cov_key, 0) > BACKFILL_COOLDOWN_SEC):
                last_backfill[cov_key] = now
                try:
                    from history.backfill_finviz_daily import backfill_daily
                    logging.info(f"No daily coverage for {ticker} — fetching FinViz daily history...")
                    backfill_daily(ticker)
                    invalidate_cache(ticker)
                    df_base, frames = run_pipeline_tiered(ticker, active_tf, days=actual_days)
                    fallback_msg = " [FinViz daily history backfilled]"
                except Exception as e:
                    logging.error(f"Daily coverage backfill failed: {e}")

        # Check if we got enough data (rough heuristic: if we want 1000 days but got < 10 days of data)
        # Or if df_base is completely empty.
        needs_backfill = False
        if df_base.empty:
            needs_backfill = True
        elif not tier_served and actual_days is not None and actual_days > 10:
            # Check date range of loaded data
            data_span_days = (df_base.index[-1] - df_base.index[0]).total_seconds() / 86400.0
            if data_span_days < (actual_days * 0.5): # If we have less than half the requested history
                needs_backfill = True

        if needs_backfill and base_tf != 'i1' and not tier_served:
            logging.info(f"Insufficient data for {ticker} (requested {actual_days} days). Spawning auto-backfills...")

            if now - last_backfill.get(ticker, 0) > BACKFILL_COOLDOWN_SEC:
                last_backfill[ticker] = now
                try:
                    import subprocess
                    import os
                    # 1. Spawn IBKR backfill
                    ibkr_days = min(int(max(3, actual_days or 3)), 180) # Cap IBKR 1-sec fetch to 180 days to avoid API blocks
                    subprocess.Popen(
                        ["python", "-m", "ibkr.backfill", "--ticker", ticker, "--days", str(ibkr_days)],
                        cwd=os.path.abspath(os.path.join(os.path.dirname(__file__))),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
                    
                    # 2. Synchronously fetch FinViz data for immediate rendering
                    from finviz.new_finviz import fetch_and_save
                    fv_tf = 'd' if actual_days > 60 else 'i1'
                    logging.info(f"Fetching fallback FinViz data (tf={fv_tf}) for immediate render...")
                    fetch_and_save(ticker, timeframe=fv_tf)
                    
                    # 3. Reload pipeline with newly fetched FinViz data as temporary fallback
                    df_base, frames = run_pipeline(ticker, base_timeframe='i1' if fv_tf == 'i1' else 'd', days=actual_days)
                    fallback_msg = " [Rendering FinViz Fallback - IBKR Backfill in Progress...]"
                except FinvizNotConfigured as e:
                    logging.info(f"FinViz fallback skipped for {ticker}: {e}")
                except Exception as e:
                    logging.error(f"Fallback fetch failed: {e}")
                    fetch_error = e
        
        if df_base.empty:
            # Three different empty states, shown differently so the user isn't
            # misled into thinking a valid ticker failed:
            #   • symbol unknown → FinViz doesn't recognize the ticker at all
            #     (e.g. a typo, or a symbol not on FinViz/IBKR). It would spin on
            #     "Fetching…" forever, so we probe once and say so outright.
            #   • fetch_error set → an exception was raised while fetching
            #     (network, API block) → a real, transient error.
            #   • otherwise → the data just isn't here YET. A new 1sec ticker has
            #     its IBKR backfill running asynchronously (seconds to minutes),
            #     and FinViz can't supply 1sec at all, so an empty frame is
            #     normal "fetching", not a failure. The poll fills it in.
            is_1sec = base_tf in ('1sec', 'raw_tick')
            if not _ticker_is_valid(ticker):
                _title = (f"⚠ '{ticker}' is not a recognized ticker — check the "
                          f"symbol. (No data on FinViz or IBKR.)")
                _status = "Unknown ticker"
                _color = "#ef5350"
            elif fetch_error is not None:
                _title = f"⚠ Error fetching {ticker}: {fetch_error}"
                _status = "Fetch error"
                _color = "#ef5350"
            elif is_1sec:
                _title = (f"⏳ Fetching {ticker} 1-sec data — IBKR backfill in "
                          f"progress. This can take up to a minute; the chart "
                          f"fills in automatically.")
                _status = "Fetching…"
                _color = "#ffa726"
            else:
                _title = f"⏳ Fetching {ticker} — backfill in progress…"
                _status = "Fetching…"
                _color = "#ffa726"
            empty_fig = go.Figure()
            empty_fig.update_layout(
                template="plotly_dark",
                title=dict(text=_title, font=dict(color=_color)),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
                yaxis=dict(showgrid=False, zeroline=False, showticklabels=False)
            )
            return empty_fig, _status, "", "{}", pan_state_json

        # Ticker is confirmed real (data loaded) → NOW ask the dynamic collector
        # for live ticks. Doing it here (not at callback entry) keeps partial /
        # invalid symbols out of collector_requests, so the LRU tick slots follow
        # the tickers you actually view. The most-recent request wins the top
        # slot, so the ticker on screen gets priority for a live tick line.
        if base_tf == 'raw_tick':
            _request_tick_collection(ticker)

        # Optimization: Only re-render if data has actually grown/changed
        current_state = {
            "ticker": ticker,
            "base_tf": base_tf,
            "active_tf": active_tf,
            "len": len(df_base),
            "oldest_date": str(df_base.index[0]),
            "newest_date": str(df_base.index[-1]),
            "days_loaded": days_to_load,
            "pie_chart_count": pie_chart_count,
            "bubbles": show_bubbles,
            "l2": show_l2,
            "yauto": bool(yauto_value),
            "bars": bars_req
        }
        # Compare ignoring n_active (a post-build fact appended below); keep
        # the previous value — the bars-to-show view remap below needs it.
        try:
            _prev_state = json.loads(last_state_json) if last_state_json else {}
        except Exception:
            _prev_state = {}
        n_active_prev = _prev_state.pop('n_active', None)
        if current_state == _prev_state and trigger == 'interval-component':
            raise PreventUpdate

        # A render-cap growth may be delivered by ANY trigger (the direct
        # bars-to-show request is aborted by dash-renderer whenever a newer
        # request supersedes it), so detect it from the state delta.
        prev_bars = _prev_state.get('bars')
        bars_grew = (prev_bars is not None and bars_req > prev_bars
                     and _prev_state.get('active_tf') == active_tf
                     and _prev_state.get('ticker') == ticker)
            
        try:
            pan_state = json.loads(pan_state_json) if pan_state_json else {}
        except:
            pan_state = {}

        panned = pan_state.get('panned', False)
        pan_time = pan_state.get('time', 0)

        # A timeframe/ticker/source switch is a fresh view: forget the pan
        # window from the previous chart (its x_idx coordinates are
        # meaningless here) and open at the newest bars. An anchor jump (or
        # "Live" clear) is likewise a fresh view — reset the pan so the window
        # snaps to the anchor bar (below) rather than a stale panned window.
        if trigger in ('timeframe-dropdown', 'ticker-input', 'source-radio', 'anchor-active'):
            panned = False
            pan_state = {'panned': False, 'time': 0}

        # NOTE: there is deliberately no idle timeout here — a panned view
        # stays where the user put it across every auto-refresh, until they
        # switch timeframe/ticker or double-click (autorange) back to live.

        # A bars-to-show growth only ever happens mid-pan/zoom, but the
        # pan-state Store (written by a parallel callback) may not have
        # committed yet — trust relayoutData directly so the grown chart
        # keeps the user's window instead of snapping to the tail.
        # EXCEPT an anchor jump: it legitimately grows bars_req (whole ±N
        # window, see above) which would read here as bars_grew and force
        # panned=True, suppressing the anchor centering below. The jump is a
        # fresh centered view, not a pan, so leave panned alone for it.
        if (trigger == 'bars-to-show' or bars_grew) and trigger != 'anchor-active':
            panned = True
            pan_state['panned'] = True
            pan_state.setdefault('time', time.time())

        x_range = None
        if panned:
            # Prefer the graph's own last relayout event over the pan-state
            # Store: the Store is written by a separate callback, so an
            # interval rebuild racing a fresh pan could otherwise snap the
            # view back to the tail with pies patched for the panned window.
            if relayout_data and 'xaxis.range[0]' in relayout_data and 'xaxis.range[1]' in relayout_data:
                x_range = (float(relayout_data['xaxis.range[0]']), float(relayout_data['xaxis.range[1]']))
            elif relayout_data and isinstance(relayout_data.get('xaxis.range'), (list, tuple)):
                x_range = (float(relayout_data['xaxis.range'][0]), float(relayout_data['xaxis.range'][1]))
            elif 'x0' in pan_state and 'x1' in pan_state:
                x_range = (pan_state['x0'], pan_state['x1'])

        # The growth payload carries the anchor window (see grow_scrollback):
        # it is the exact event that triggered the growth, fresher than both
        # relayoutData (may hold a y-only refit by now) and the pan Store.
        if ((trigger == 'bars-to-show' or bars_grew)
                and isinstance(bars_to_show, dict) and 'x0' in bars_to_show):
            x_range = (float(bars_to_show['x0']), float(bars_to_show['x1']))

        # A render-cap growth shifts the x_idx coordinate space: index 0 now
        # points further into the past, so the view window recorded in OLD
        # coordinates must slide right by the number of bars prepended —
        # otherwise the chart would jump to a different region after loading.
        if bars_grew and x_range is not None and n_active_prev:
            _key = active_tf if active_tf in frames else list(frames.keys())[0]
            _shift = min(len(frames[_key]), bars_req) - n_active_prev
            if _shift > 0:
                x_range = (x_range[0] + _shift, x_range[1] + _shift)

        # ── Level-2 depth heatmap + support/resistance (mock or real —
        # trading_cvd.level2_snapshots, filled by tests/mock_level2_stream.py
        # today and ibkr/level2_collector.py once the depth subscription is
        # live). Computed on the SAME truncated tail build_chart will render
        # (iloc[-bars_req:]) so heatmap columns line up with x_idx 1:1.
        _t_data = time.monotonic()
        l2_data = None
        if show_l2:
            try:
                _l2_key = active_tf if active_tf in frames else list(frames.keys())[0]
                _l2_full = frames[_l2_key]
                # Live view renders the tail, so match L2 to the tail. An anchor
                # jump renders a window CENTERED on the pinned bar, so match L2 to
                # THAT window instead — otherwise the depth is fetched/placed at
                # the frame's end (far off-screen) and the heatmap never shows.
                if anchor_dt is not None and len(_l2_full):
                    try:
                        _apos = _l2_full.index.get_indexer(
                            [pd.Timestamp(anchor_dt)], method='nearest')[0]
                    except Exception:
                        _apos = len(_l2_full) - 1
                    _lo = max(0, _apos - 150)
                    _l2_win = _l2_full.iloc[_lo:_apos + 150]
                else:
                    _l2_win = _l2_full.iloc[-bars_req:]
                _, _yl, _zm, _zb = fetch_and_aggregate_l2_data(ticker, _l2_win, max_candles=300)
                _closes = _l2_win["close"].dropna() if len(_l2_win) else _l2_win
                if _zm is not None and len(_closes):
                    # last VALID close — session-grid empty candles carry NaN
                    _mid = float(_closes.iloc[-1])
                    # S&R is computed on the FULL book (before the depth clip) so
                    # the strongest support AND resistance walls are always found
                    # and classified with full-book context — the depth selector
                    # must never hide one side. (The score threshold and bid/ask
                    # dominance are relative to the levels passed in, so clipping
                    # first would drop the far wall and skew the classification.)
                    _sr = compute_support_resistance(_yl, _zm, _mid, z_bid=_zb)
                    # Depth-zoom: the "L2 Depth" selector (10 / 20 / Full book)
                    # then clips only the HEATMAP display to that many levels each
                    # side of the current mid, so it reads like Bookmap at the
                    # chosen depth while the two S&R lines stay full-book.
                    _yl, _zm, _zb = _clip_l2_levels(_yl, _zm, _zb, _mid, show_l2)
                    # x_times = the candle timestamps the z-columns were built on
                    # (the fetch used the LAST max_candles of _l2_win); build_chart
                    # maps these to x_idx so the bands land on the right bars in
                    # either tail or anchor-centered views.
                    _nz = int(_zm.shape[1])
                    l2_data = {"y_levels": _yl, "z": _zm,
                               "x_times": list(_l2_win.index[-_nz:]),
                               "sr": _sr}
                else:
                    logging.info(f"L2: no depth snapshots for {ticker} in the visible window")
            except Exception as e:
                logging.error(f"L2 fetch failed (chart renders without it): {e}")

        _t_l2 = time.monotonic()
        fig = build_chart(df_base, frames, ticker, active_timeframe=active_tf, pie_chart_count=pie_chart_count, show_bubbles=show_bubbles, x_range=x_range, max_candles=bars_req, l2_data=l2_data)
        _t_build = time.monotonic()

        # build_chart mutates `frames`: the active frame is now truncated to
        # MAX_CANDLES with its FINAL x_idx/time_str — cache THAT frame so the
        # pie pan-callback works in the same coordinate space as the figure.
        # (Caching the pre-build frame shifted every x_idx by len(df)-6000
        # whenever the frame exceeded the candle cap, so the pies summed
        # buy/sell from completely different bars than the ones on screen.)
        # (no `global` needed: DATA_CACHE is only item-assigned, never rebound)
        active_key = active_tf if active_tf and active_tf in frames else list(frames.keys())[0]
        df_active = frames[active_key]
        DATA_CACHE['df'] = df_active
        DATA_CACHE['pie_chart_count'] = pie_chart_count
        DATA_CACHE['pie_indices'] = [i for i, tr in enumerate(fig.data) if tr.type == 'pie']
        # Post-build frame length, carried in the per-client state Store: the
        # off-tail interval skip and handle_panning both need it.
        current_state['n_active'] = len(df_active)
        current_state_json = json.dumps(current_state)

        # View window: an un-panned anchor jump centers on the pinned bar; a
        # panned view keeps its window; otherwise auto-tail to the newest bars.
        N_total = len(df_active)
        anchor_view = None
        if anchor_dt is not None and not panned and N_total:
            # Locate the bar nearest the pinned instant and frame a fixed-width
            # window centered on it, so the exact typed date/time lands mid-
            # screen instead of the front (or tail) of the ±N loaded window.
            try:
                pos = df_active.index.get_indexer([pd.Timestamp(anchor_dt)],
                                                  method='nearest')[0]
                if pos >= 0:
                    cx = float(df_active['x_idx'].iloc[pos])
                    half = ANCHOR_VIEW_BARS / 2.0
                    anchor_view = (cx - half, cx + half)
            except Exception as e:
                logging.warning(f"anchor centering failed ({anchor_dt}): {e}")

        if anchor_view is not None:
            x0, x1 = anchor_view
        elif x_range is not None:
            x0, x1 = float(x_range[0]), float(x_range[1])
        else:
            x0, x1 = max(0, N_total - 100), N_total
        # Write the window to EVERY x axis: the subplots' MASTER axis is
        # xaxis3 (xaxis/xaxis2 carry matches='x3'), and build_chart has just
        # stamped its own tail window on all of them — updating only the
        # slave `xaxis` would let the master's tail win via the constraint.
        fig.update_xaxes(range=[x0, x1])

        # Server-side Y fit to the visible window (same 10% pad as the client
        # refit JS). The delivered figure is already correctly scaled, so the
        # clientside refit becomes a no-op on data refreshes — no more
        # full-height stretch followed by a slow re-scale on every tick.
        def _fit(*series):
            s = pd.concat([pd.Series(x, dtype="float64") for x in series]).dropna()
            if s.empty:
                return None
            lo, hi = float(s.min()), float(s.max())
            if lo == hi:
                lo, hi = lo - 1, hi + 1
            pad = (hi - lo) * 0.10
            return [lo - pad, hi + pad]

        # The user's own y-zoom arrives in relayoutData (drag/scroll on an
        # axis) — while panned, those explicit ranges beat the server fit, so
        # a refresh doesn't yank a hand-scaled axis back to auto.
        user_y = {}
        if panned and relayout_data:
            for ax in ('yaxis', 'yaxis2', 'yaxis4'):
                k0, k1 = f'{ax}.range[0]', f'{ax}.range[1]'
                if k0 in relayout_data and k1 in relayout_data:
                    user_y[ax] = [float(relayout_data[k0]), float(relayout_data[k1])]

        # Y Auto-Scale OFF → the server writes y ranges ONLY on fresh-view
        # triggers (new coordinate space / toggle flips); on every other
        # trigger the axes stay untouched and uirevision preserves whatever
        # manual scale the user set on the price axis.
        y_auto = bool(yauto_value)
        # 'l2-dropdown' included so changing the L2 depth always refits the price
        # axis to the new heatmap + both S&R lines, even when Y Auto-Scale is off.
        fresh_view = trigger in ('ticker-input', 'source-radio',
                                 'timeframe-dropdown', 'yauto-check', 'l2-dropdown')

        df_vis = df_active[(df_active['x_idx'] >= x0) & (df_active['x_idx'] <= x1)]
        if not df_vis.empty and (y_auto or fresh_view):
            y_price = _fit(df_vis['high'], df_vis['low']) if 'high' in df_vis.columns else _fit(df_vis['close'])
            y_pa = _fit(df_vis['buy_pressure'], -df_vis['sell_pressure'])
            y_pb = _fit(df_vis['cvd_all_end']) if 'cvd_all_end' in df_vis.columns else None
            # L2 on: widen the price fit so liquidity bands worth seeing
            # (>=15% of the strongest resting size in the visible window)
            # aren't clipped — but ignore faint fringe levels so one deep
            # stray wall can't squash the candles.
            if l2_data is not None and l2_data.get("z") is not None and y_price:
                _z = l2_data["z"]
                _n_cols = _z.shape[1]
                _col0 = max(0, int(x0) - (N_total - _n_cols))
                _col1 = min(_n_cols - 1, int(x1) - (N_total - _n_cols))
                if _col0 <= _col1 and _z.max() > 0:
                    _win = _z[:, _col0:_col1 + 1]
                    # Same liquidity reference as the heatmap colorscale and
                    # the clientside refit: 98th percentile of positive sizes
                    # (trace zmax), NOT the raw max an iceberg can own.
                    _zref = float(np.percentile(_z[_z > 0], 98))
                    _liquid = _win.max(axis=1) >= 0.15 * _zref
                    if _liquid.any():
                        _lv = [l2_data["y_levels"][i] for i in range(len(_liquid)) if _liquid[i]]
                        _pad = max(0.02, 0.05 * (max(_lv) - min(_lv)))
                        y_price = [min(y_price[0], min(_lv) - _pad),
                                   max(y_price[1], max(_lv) + _pad)]
                # The two S&R lines are computed on the full book but the heatmap
                # may be clipped to a shallow depth, so a wall can sit outside the
                # visible band. Include the S&R prices in the fit so BOTH the
                # support and resistance lines are always on screen — they are the
                # key signal, and the depth selector must not hide one.
                _sr_lv = [lvl["price"] for lvl in (l2_data.get("sr") or [])]
                if _sr_lv and y_price:
                    _srpad = 0.02 * (max(_sr_lv) - min(_sr_lv)) if len(_sr_lv) > 1 else 0.0
                    _srpad = max(0.02, _srpad)
                    y_price = [min(y_price[0], min(_sr_lv) - _srpad),
                               max(y_price[1], max(_sr_lv) + _srpad)]
            y_price = user_y.get('yaxis', y_price)
            y_pa = user_y.get('yaxis2', y_pa)
            y_pb = user_y.get('yaxis4', y_pb)
            if y_price: fig.update_layout(yaxis=dict(range=y_price))
            if y_pa:    fig.update_layout(yaxis2=dict(range=y_pa))
            if y_pb:    fig.update_layout(yaxis4=dict(range=y_pb))
        elif (not y_auto) and isinstance(manual_y_store, dict):
            # Manual mode, non-fresh rebuild: bake the user's hand-set ranges
            # into the served figure. uirevision alone cannot protect them —
            # it only preserves true GUI drags, and build_chart writes its own
            # tail-fit ranges that would win on every new candle otherwise.
            for _ax in ('yaxis', 'yaxis2', 'yaxis4'):
                _rng = manual_y_store.get(_ax)
                if _rng and len(_rng) == 2:
                    fig.update_layout({_ax: dict(range=[float(_rng[0]), float(_rng[1])])})

        # Manual mode unlocks the y axes: price-axis drag/wheel AND the y
        # component of plot-area pans both stick (tracked by the manual-y
        # Store and re-applied above).
        fig.update_layout(yaxis=dict(fixedrange=y_auto),
                          yaxis2=dict(fixedrange=y_auto),
                          yaxis4=dict(fixedrange=y_auto))

        # Axis uirevision: changes only when the x-coordinate space itself
        # changes (bars added/removed shift x_idx), so Plotly then accepts the
        # server-computed ranges above; while data is unchanged the user's
        # pan/zoom state is preserved as before. The global uirevision keeps
        # legend toggles etc. stable per ticker.
        # bars_req is part of the coordinate space: growing the render cap
        # remaps every x_idx, so the server-computed ranges must win then too.
        # EVERY x axis must carry the revision: the subplots share x via
        # `matches`, and a matched axis whose uirevision did NOT change would
        # restore its old range and drag the primary axis back with it.
        axis_rev = f"{ticker}:{current_state['len']}:{current_state['oldest_date']}:{bars_req}"
        # Manual y mode: the y axes get a DATA-INDEPENDENT revision, so the
        # user's hand-set scale survives every refresh (len grows each bar —
        # with the data-sensitive revision Plotly would drop the client's y
        # state and adopt build_chart's baked tail fit on every new candle).
        # Fresh-view triggers change ticker/tf (or the toggle itself), which
        # changes this string too, letting the server's one-shot fit land.
        y_rev = axis_rev if y_auto else f"{ticker}:{active_tf}:manual"
        for ax in list(fig.layout):
            if ax.startswith('xaxis'):
                fig.update_layout({ax: dict(uirevision=axis_rev)})
            elif ax in ('yaxis', 'yaxis2', 'yaxis4'):
                fig.update_layout({ax: dict(uirevision=y_rev)})

        fig.update_layout(
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            uirevision=ticker,
        )

        import datetime
        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        msg = f"Last Updated: {now_str} (Trigger: {trigger}){fallback_msg}"

        _t_end = time.monotonic()
        logging.info(
            f"[timing] update_graph {_t_end-_t_cb0:.2f}s "
            f"(data {_t_data-_t_cb0:.2f} | l2 {_t_l2-_t_data:.2f} | "
            f"build {_t_build-_t_l2:.2f} | post {_t_end-_t_build:.2f})")

        # Return fig, text, empty string for loading, new state, pan state
        return fig, msg, "", current_state_json, json.dumps(pan_state)
        
    except PreventUpdate:
        raise
    except Exception as e:
        import traceback
        logging.error(f"Error building chart: {e}\n{traceback.format_exc()}")
        empty_fig = go.Figure()
        empty_fig.update_layout(template="plotly_dark", title=dict(text=f"Error: {e}", font=dict(color="red")))
        return empty_fig, f"Error: {e}", "", last_state_json, pan_state_json

app.clientside_callback(
    dash.ClientsideFunction(
        namespace='clientside',
        function_name='refit_y'
    ),
    Output('clientside-dummy', 'children'),
    [Input('main-chart', 'relayoutData'),
     Input('main-chart', 'figure')]
)

# Manual-y tracker: records every user y movement (plot-area pan, price-axis
# drag/wheel, double-click refit result) so the server can re-apply the ranges
# on every rebuild while Y Auto-Scale is off.
app.clientside_callback(
    dash.ClientsideFunction(
        namespace='clientside',
        function_name='track_manual_y'
    ),
    Output('manual-y', 'data'),
    [Input('main-chart', 'relayoutData'),
     Input('yauto-check', 'value')],
    State('manual-y', 'data'),
    prevent_initial_call=True,
)

# Relayout debounce: raw per-frame relayout stream → a single settled value
# (~160ms after motion stops). The heavy callbacks below hang off this so one
# gesture is one rebuild, not a per-frame storm (see debounce_relayout).
app.clientside_callback(
    dash.ClientsideFunction(
        namespace='clientside',
        function_name='debounce_relayout'
    ),
    Output('settled-relayout', 'data'),
    Input('main-chart', 'relayoutData'),
    prevent_initial_call=True,
)

# Bars-to-show growth: clientside so it can never be aborted by a newer
# relayout event (see grow_scrollback in index_string). Driven by the
# DEBOUNCED relayout so a zoom-out doubles bars once per gesture, not per frame.
app.clientside_callback(
    dash.ClientsideFunction(
        namespace='clientside',
        function_name='grow_scrollback'
    ),
    Output('bars-to-show', 'data'),
    Input('settled-relayout', 'data'),
    [State('bars-to-show', 'data'),
     State('last-data-state', 'data')],
    prevent_initial_call=True,
)


# Progressive scrollback, server half. The x axis is a LINEAR bar index
# (x_idx: 0..n_active-1), NOT a date — hitting the left edge or zooming out
# past what's rendered means the user wants MORE BARS:
#   * render-cap limited (n_active == bars-to-show): the CLIENTSIDE
#     grow_scrollback callback doubles bars-to-show (it must run in the
#     browser: a server callback on relayoutData gets aborted whenever the
#     y-refit fires a newer relayout event, eating the growth).
#   * data limited (n_active < bars-to-show): this callback doubles
#     days-to-load so the next serve pulls a deeper window from Mongo.
# Both stores are per-TF, so growth on one timeframe never slows the others.
@app.callback(
    Output('days-to-load', 'data'),
    Input('settled-relayout', 'data'),
    [State('days-to-load', 'data'),
     State('bars-to-show', 'data'),
     State('last-data-state', 'data')],
    prevent_initial_call=True,
)
def handle_panning(relayout_data, current_days, current_bars, last_state_json):
    if not relayout_data:
        raise PreventUpdate
    if 'xaxis.range[0]' in relayout_data and 'xaxis.range[1]' in relayout_data:
        x0, x1 = float(relayout_data['xaxis.range[0]']), float(relayout_data['xaxis.range[1]'])
    elif isinstance(relayout_data.get('xaxis.range'), (list, tuple)):
        x0, x1 = float(relayout_data['xaxis.range'][0]), float(relayout_data['xaxis.range'][1])
    else:
        raise PreventUpdate

    if not last_state_json or last_state_json == "{}":
        raise PreventUpdate

    try:
        state = json.loads(last_state_json)
        active_tf = state.get('active_tf')
        n_active = state.get('n_active')
        if not n_active:
            raise PreventUpdate

        if x0 > 10:   # only deepen when the view actually reaches the oldest bars
            raise PreventUpdate

        cur_bars = _resolve_bars(current_bars, active_tf)
        if n_active >= cur_bars:
            raise PreventUpdate   # cap-limited: clientside bars growth handles it

        if isinstance(current_days, dict):
            cur = float(current_days.get('days', 3)) if current_days.get('tf') == active_tf else 3.0
        else:
            cur = float(current_days or 3)
        # The effective window is max(store, SERVE_WINDOW_DAYS default) —
        # double from THAT, or the store creeps below the default forever.
        cur = max(cur, float(SERVE_WINDOW_DAYS.get(active_tf) or 0))
        new_days = min(cur * 2 + 1, 365)
        if new_days > cur:
            logging.info(f"Scrollback: data-limited, growing loaded days {cur} -> {new_days} ({active_tf})")
            return {'days': new_days, 'tf': active_tf}

        raise PreventUpdate
    except PreventUpdate:
        raise
    except Exception as e:
        logging.error(f"Error in handle_panning: {e}")
        raise PreventUpdate

# NOTE: no State('main-chart', 'figure') — pulling the figure back to the
# server on EVERY relayout event (each pan step / scroll tick) re-uploaded
# multiple MB of JSON per gesture and made panning feel sluggish. The pie
# trace positions are cached in DATA_CACHE by update_graph instead.
@app.callback(
    [Output('main-chart', 'figure', allow_duplicate=True),
     Output('pan-state', 'data', allow_duplicate=True)],
    Input('settled-relayout', 'data'),
    prevent_initial_call=True
)
def update_pie_charts_on_pan(relayout_data):
    try:
        if not relayout_data:
            raise PreventUpdate
            
        # Sometimes relayoutData has 'xaxis.range' as a list instead of 'xaxis.range[0]'
        x0 = None
        x1 = None
        if 'xaxis.range[0]' in relayout_data and 'xaxis.range[1]' in relayout_data:
            x0 = float(relayout_data['xaxis.range[0]'])
            x1 = float(relayout_data['xaxis.range[1]'])
        elif 'xaxis.range' in relayout_data:
            x0 = float(relayout_data['xaxis.range'][0])
            x1 = float(relayout_data['xaxis.range'][1])
            
        if x0 is None or x1 is None:
            # User might have clicked autorange (reset), we return the pan_state but we can't update pies
            if 'xaxis.autorange' in relayout_data:
                return dash.no_update, json.dumps({"panned": False, "time": 0})
            raise PreventUpdate
            
        # We record any panning action
        new_pan_state = json.dumps({"panned": True, "time": time.time(), "x0": x0, "x1": x1})
            
        df = DATA_CACHE.get('df')
        pie_indices = DATA_CACHE.get('pie_indices') or []

        if df is None or not pie_indices:
            raise PreventUpdate

        # Filter to visible range
        df_vis = df[(df['x_idx'] >= x0) & (df['x_idx'] <= x1)]

        patched_fig = dash.Patch()

        # The trace pool is fixed at build time; use however many pie traces the
        # figure actually has as the slot count so build_chart and the pan patch
        # stay in the same coordinate space regardless of zoom-level pie drift.
        n_slots = len(pie_indices)

        # Same equal-width, candle-aligned geometry build_chart used, so a pan
        # never reintroduces uneven spacing or unequal bars-per-pie.
        layout = pie_layout(df_vis, n_slots, x0, x1)

        for k, idx in enumerate(pie_indices):
            slot = layout[k] if k < len(layout) else None
            if slot is not None:
                patched_fig['data'][idx]['values'] = [slot["buy"], slot["sell"]]
                patched_fig['data'][idx]['marker'] = {'colors': ["rgba(38,166,154,0.7)", "rgba(239,83,80,0.7)"]}
                patched_fig['data'][idx]['hoverinfo'] = "text"
                patched_fig['data'][idx]['hovertext'] = (
                    f"{slot['label']}<br>Buy: {slot['buy']:,.0f}<br>Sell: {slot['sell']:,.0f}")
                patched_fig['data'][idx]['domain'] = {'x': slot["d_x"], 'y': slot["d_y"]}
            else:
                # Hidden slot: zero the domain so it disappears.
                patched_fig['data'][idx]['values'] = [0, 0]
                patched_fig['data'][idx]['marker'] = {'colors': ["rgba(100,100,100,0.5)", "rgba(100,100,100,0.5)"]}
                patched_fig['data'][idx]['hoverinfo'] = "none"
                patched_fig['data'][idx]['domain'] = {'x': [0, 0], 'y': [0, 0]}
                
        logging.info(f"Patch successful for {len(pie_indices)} pies.")
        return patched_fig, new_pan_state
        
    except PreventUpdate:
        raise
    except Exception as e:
        import traceback
        with open('pie_err.log', 'w') as f:
            f.write(traceback.format_exc())
            f.write('\nRelayout Data: ' + str(relayout_data))
        raise PreventUpdate


if __name__ == '__main__':
    # Overridable so the app can run on a machine where 8050 is taken, or be
    # reached from another host:  PORT=8060 HOST=0.0.0.0 python -m app
    # DASH_DEBUG=0 turns off the auto-reloader (which otherwise starts a second
    # process — keep it on for development, off when running it as a service).
    app.run(
        debug=os.environ.get("DASH_DEBUG", "1") != "0",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8050")),
    )
