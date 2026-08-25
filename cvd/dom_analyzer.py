"""
cvd/dom_analyzer.py - Phase 4: DOM Pressure and Spoofing Analyzer
Analyzes Level 2 market depth data from MongoDB to calculate 
DOM pressure and generate Eric's 1.5x Spoofing signals.
"""

import os

import pandas as pd
from pymongo import MongoClient

# Overridable so the app can run against a MongoDB that is not on this
# machine. Railway (and any container deploy) runs the database as a
# separate service, where "localhost" is the app container itself and
# resolves to nothing. Default unchanged, so local runs are unaffected.
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
DB_NAME = "trading_cvd"
COLLECTION_NAME = "level2_snapshots"

def get_dom_pressure_signals(ticker: str, start_ts: float, end_ts: float):
    """
    Fetch L2 snapshots from MongoDB within [start_ts, end_ts],
    calculate DOM Pressure (Top 5 Bids - Top 5 Asks),
    and detect 1.5x Spoofing signals.
    """
    client = MongoClient(MONGO_URI)
    db = client[DB_NAME]
    col = db[COLLECTION_NAME]

    query = {
        "ticker": ticker.upper(),
        "timestamp": {"$gte": start_ts, "$lte": end_ts}
    }
    
    cursor = col.find(query).sort("timestamp", 1)
    data = list(cursor)
    
    if not data:
        return pd.DataFrame()

    results = []
    
    for row in data:
        timestamp = row["timestamp"]
        bids = row.get("bids", [])
        asks = row.get("asks", [])
        
        # We only care about Top 5 levels for DOM Pressure
        top_bids = bids[:5]
        top_asks = asks[:5]
        
        total_bid_size = sum(b["size"] for b in top_bids)
        total_ask_size = sum(a["size"] for a in top_asks)
        
        dom_pressure = total_bid_size - total_ask_size
        
        # Eric's 1.5x Spoofing Logic
        # (Counter-intuitive: large ask wall = price goes up = Long)
        signal = 0
        if total_ask_size > 1.5 * total_bid_size and total_bid_size > 0:
            signal = 1  # BUY signal
        elif total_bid_size > 1.5 * total_ask_size and total_ask_size > 0:
            signal = -1 # SELL signal
            
        results.append({
            "timestamp": pd.to_datetime(timestamp, unit='s', utc=True),
            "dom_pressure": dom_pressure,
            "signal": signal,
            "best_bid": bids[0]["price"] if bids else None,
            "best_ask": asks[0]["price"] if asks else None,
            "total_bid_size": total_bid_size,
            "total_ask_size": total_ask_size,
        })
        
    df = pd.DataFrame(results)
    if not df.empty:
        df.set_index("timestamp", inplace=True)
        # Convert to NY time
        df.index = df.index.tz_convert("America/New_York")
        
    return df
