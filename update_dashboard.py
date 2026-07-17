#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
米国株マーケットダッシュボード 自動更新スクリプト

FMP (Financial Modeling Prep) の stable API から最新データを取得し、
market_dashboard.html 内の
  /* ===AUTO_UPDATE_DATA_START=== */ ... /* ===AUTO_UPDATE_DATA_END=== */
で囲まれた const DASHBOARD_DATA = {...}; ブロックだけを書き換える。
タブ切り替えUI・チャート機能などその他のコードには一切触れない。

環境変数 FMP_API_KEY が必要（GitHub Actions の Secrets 経由で渡す想定）。
"""
import os
import re
import sys
import json
from datetime import datetime, timezone, timedelta

import requests

API_KEY = os.environ.get("FMP_API_KEY")
if not API_KEY:
    print("ERROR: 環境変数 FMP_API_KEY が設定されていません。", file=sys.stderr)
    sys.exit(1)

BASE = "https://financialmodelingprep.com/stable"
HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_dashboard.html")

INDEX_SYMBOLS = [
    ("^GSPC", "S&P500"),
    ("^NDX", "NASDAQ100"),
    ("^DJI", "NYダウ"),
    ("^RUT", "ラッセル2000"),
    ("^VIX", "VIX"),
]

SECTOR_SYMBOLS = [
    ("XLP", "生活必需品"), ("XLV", "ヘルスケア"), ("XLRE", "不動産"),
    ("XLE", "エネルギー"), ("XLB", "素材"), ("XLU", "公共事業"),
    ("XLF", "金融"), ("XLY", "一般消費財"), ("XLI", "資本財"),
    ("XLC", "通信サービス"), ("XLK", "情報技術"),
]

HEATMAP_SYMBOLS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B", "LLY",
    "AVGO", "JPM", "V", "UNH", "XOM", "MA", "JNJ", "PG", "HD", "MRK", "COST",
    "ABBV", "CVX", "KO", "PEP", "WMT", "BAC", "CRM", "NFLX", "ADBE",
]

COMMODITY_SYMBOLS = [
    ("GCUSD", "金 (Gold)"), ("CLUSD", "原油 (WTI)"),
    ("SIUSD", "銀 (Silver)"), ("NGUSD", "天然ガス"),
]

FOREX_SYMBOLS = [("USDJPY", "USD/JPY"), ("EURUSD", "EUR/USD")]


def fetch_batch_quote(symbols):
    url = f"{BASE}/batch-quote"
    r = requests.get(url, params={"symbols": ",".join(symbols), "apikey": API_KEY}, timeout=20)
    r.raise_for_status()
    data = r.json()
    return {row["symbol"]: row for row in data}


def fetch_treasury_10y():
    r = requests.get(f"{BASE}/treasury-rates", params={"apikey": API_KEY}, timeout=20)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        return None, None
    latest = rows[0]
    return latest.get("year10"), latest.get("date")


def fetch_news(limit=5):
    r = requests.get(f"{BASE}/news/general-latest", params={"page": 0, "limit": limit, "apikey": API_KEY}, timeout=20)
    r.raise_for_status()
    return r.json()


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def pct(row):
    return round(float(row.get("changePercentage", 0)), 2)


def main():
    indices_q = fetch_batch_quote([s for s, _ in INDEX_SYMBOLS])
    sectors_q = fetch_batch_quote([s for s, _ in SECTOR_SYMBOLS])
    heatmap_q = fetch_batch_quote(HEATMAP_SYMBOLS)
    commodities_q = fetch_batch_quote([s for s, _ in COMMODITY_SYMBOLS])
    forex_q = fetch_batch_quote([s for s, _ in FOREX_SYMBOLS])
    year10, year10_date = fetch_treasury_10y()
    news_raw = fetch_news(5)

    # ---- indices ----
    indices = []
    for sym, name in INDEX_SYMBOLS:
        row = indices_q.get(sym)
        if not row:
            continue
        indices.append({
            "name": name, "symbol": sym,
            "price": round(float(row["price"]), 2),
            "chg": round(float(row["change"]), 2),
            "pct": pct(row),
        })

    # ---- sentiment ----
    sp500 = indices_q.get("^GSPC", {})
    vix = indices_q.get("^VIX", {})
    breadth_up = sum(1 for s in HEATMAP_SYMBOLS if heatmap_q.get(s) and pct(heatmap_q[s]) > 0)
    breadth = round(breadth_up / len(HEATMAP_SYMBOLS) * 100)

    momentum = 50
    if sp500.get("priceAvg50"):
        momentum = 50 + (float(sp500["price"]) - float(sp500["priceAvg50"])) / float(sp500["priceAvg50"]) * 1000
        momentum = round(clamp(momentum, 0, 100))

    vix_component = 50
    if vix.get("priceAvg50"):
        vix_component = 50 - (float(vix["price"]) - float(vix["priceAvg50"])) / float(vix["priceAvg50"]) * 500
        vix_component = round(clamp(vix_component, 0, 100))

    score = round((breadth + momentum + vix_component) / 3)
    label = "警戒" if score < 40 else ("楽観" if score > 60 else "中立")

    # ---- misc: commodities + forex + rate ----
    misc = []
    for sym, name in COMMODITY_SYMBOLS:
        row = commodities_q.get(sym)
        if not row:
            continue
        misc.append({"name": name, "price": f"${float(row['price']):,.2f}", "pct": pct(row)})
    for sym, name in FOREX_SYMBOLS:
        row = forex_q.get(sym)
        if not row:
            continue
        prefix = "¥" if sym == "USDJPY" else "$"
        decimals = 2 if sym == "USDJPY" else 4
        misc.append({"name": name, "price": f"{prefix}{float(row['price']):,.{decimals}f}", "pct": pct(row)})
    if year10 is not None:
        misc.append({"name": "米10年国債利回り", "price": f"{year10:.2f}%", "pct": None, "note": f"{year10_date}時点"})

    # ---- sectors ----
    sectors = []
    for sym, name in SECTOR_SYMBOLS:
        row = sectors_q.get(sym)
        if not row:
            continue
        sectors.append({"name": name, "ticker": sym, "pct": pct(row)})
    sectors.sort(key=lambda s: s["pct"], reverse=True)

    # ---- heatmap ----
    heatmap = []
    for sym in HEATMAP_SYMBOLS:
        row = heatmap_q.get(sym)
        if not row:
            continue
        cap_b = float(row.get("marketCap") or 0) / 1e9
        tier = 1 if cap_b >= 2000 else (2 if cap_b >= 800 else 3)
        heatmap.append({"t": sym, "pct": pct(row), "cap": round(cap_b, 1), "tier": tier})

    # ---- news (short snippet + link, not full article) ----
    news = []
    for item in news_raw[:5]:
        text = (item.get("text") or "").strip()
        if len(text) > 130:
            text = text[:130].rstrip() + "…"
        news.append({
            "pub": item.get("publisher") or item.get("site") or "",
            "title": item.get("title") or "",
            "desc": text,
            "url": item.get("url") or "",
        })

    jst = timezone(timedelta(hours=9))
    now_jst = datetime.now(jst)
    fetched_at = now_jst.strftime("%Y年%m月%d日 %H:%M JST 時点（FMPデータ・自動更新）")

    data = {
        "fetchedAt": fetched_at,
        "indices": indices,
        "misc": misc,
        "sentiment": {"score": score, "breadth": breadth, "momentum": momentum, "vix": vix_component, "label": label},
        "sectors": sectors,
        "heatmap": heatmap,
        "news": news,
    }

    js_block = (
        "/* ===AUTO_UPDATE_DATA_START=== */\n"
        "const DASHBOARD_DATA = "
        + json.dumps(data, ensure_ascii=False, indent=2)
        + ";\n"
        "/* ===AUTO_UPDATE_DATA_END=== */"
    )

    with open(HTML_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    pattern = re.compile(
        r"/\* ===AUTO_UPDATE_DATA_START=== \*/.*?/\* ===AUTO_UPDATE_DATA_END=== \*/",
        re.S,
    )
    if not pattern.search(html):
        print("ERROR: データ置換用の目印コメントが見つかりません。HTMLを確認してください。", file=sys.stderr)
        sys.exit(1)

    new_html = pattern.sub(lambda m: js_block, html, count=1)

    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(new_html)

    print(f"OK: {fetched_at} のデータで更新しました（センチメントスコア={score} [{label}]）")


if __name__ == "__main__":
    main()
