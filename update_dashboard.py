#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
米国株マーケットダッシュボード 自動更新スクリプト (v2: 無料プラン対応版)

FMPの無料プランはリアルタイムQuote系エンドポイント(quote / batch-quote 等)には
アクセスできず、402 Payment Required が返る。
一方で以下は無料プランでも利用可能:
  - historical-price-eod/light (終値の日次データ)
  - profile (会社プロフィール。時価総額を含む)
  - treasury-rates / news/general-latest

そのため、このバージョンでは全ての価格取得を historical-price-eod/light
(前日終値ベース)に切り替え、当日比・50日移動平均も自前で計算する。

market_dashboard.html 内の
  /* ===AUTO_UPDATE_DATA_START=== */ ... /* ===AUTO_UPDATE_DATA_END=== */
で囲まれた const DASHBOARD_DATA = {...}; ブロックだけを書き換える。
"""
import os
import re
import sys
import json
import time
from datetime import datetime, timezone, timedelta

import requests

API_KEY = os.environ.get("FMP_API_KEY")
if not API_KEY:
    print("ERROR: 環境変数 FMP_API_KEY が設定されていません。", file=sys.stderr)
    sys.exit(1)

BASE = "https://financialmodelingprep.com/stable"
HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_dashboard.html")
REQUEST_SLEEP = 0.2  # 無料プランのレート制限に配慮

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


def _get(path, params):
    params = dict(params)
    params["apikey"] = API_KEY
    r = requests.get(f"{BASE}/{path}", params=params, timeout=20)
    time.sleep(REQUEST_SLEEP)
    r.raise_for_status()
    return r.json()


def fetch_eod_series(symbol, days=90):
    """終値の時系列を日付昇順で返す。各要素は {date, price}"""
    to_date = datetime.now(timezone.utc).date()
    from_date = to_date - timedelta(days=days)
    try:
        rows = _get("historical-price-eod/light", {
            "symbol": symbol, "from": from_date.isoformat(), "to": to_date.isoformat(),
        })
    except requests.exceptions.HTTPError as e:
        print(f"WARN: {symbol} のEODデータ取得に失敗: {e}", file=sys.stderr)
        return []
    if not isinstance(rows, list) or not rows:
        return []
    out = []
    for row in rows:
        price = row.get("price", row.get("close"))
        date = row.get("date")
        if price is None or date is None:
            continue
        out.append({"date": date, "price": float(price)})
    out.sort(key=lambda x: x["date"])
    return out


def latest_and_change(series):
    """直近終値・前日比%・50日移動平均を返す"""
    if len(series) < 2:
        return None, None, None
    latest = series[-1]["price"]
    prev = series[-2]["price"]
    chg_pct = round((latest - prev) / prev * 100, 2) if prev else None
    window = series[-50:] if len(series) >= 50 else series
    sma50 = sum(x["price"] for x in window) / len(window)
    return latest, chg_pct, sma50


def fetch_market_cap(symbol):
    try:
        rows = _get("profile", {"symbol": symbol})
    except requests.exceptions.HTTPError as e:
        print(f"WARN: {symbol} のprofile取得に失敗: {e}", file=sys.stderr)
        return None
    if not isinstance(rows, list) or not rows:
        return None
    row = rows[0]
    cap = row.get("marketCap", row.get("mktCap"))
    return float(cap) if cap is not None else None


def fetch_treasury_10y():
    try:
        rows = _get("treasury-rates", {})
    except requests.exceptions.HTTPError as e:
        print(f"WARN: treasury-ratesの取得に失敗: {e}", file=sys.stderr)
        return None, None
    if not rows:
        return None, None
    latest = rows[0]
    return latest.get("year10"), latest.get("date")


def fetch_news(limit=5):
    try:
        return _get("news/general-latest", {"page": 0, "limit": limit})
    except requests.exceptions.HTTPError as e:
        print(f"WARN: newsの取得に失敗: {e}", file=sys.stderr)
        return []


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def main():
    # ---- indices ----
    indices = []
    index_latest = {}
    for sym, name in INDEX_SYMBOLS:
        series = fetch_eod_series(sym, days=90)
        latest, chg_pct, sma50 = latest_and_change(series)
        if latest is None:
            print(f"WARN: {sym} のデータが取得できませんでした（スキップ）", file=sys.stderr)
            continue
        prev = series[-2]["price"]
        indices.append({
            "name": name, "symbol": sym,
            "price": round(latest, 2),
            "chg": round(latest - prev, 2),
            "pct": chg_pct,
        })
        index_latest[sym] = {"price": latest, "sma50": sma50, "pct": chg_pct}

    # ---- sectors ----
    sectors = []
    for sym, name in SECTOR_SYMBOLS:
        series = fetch_eod_series(sym, days=20)
        _, chg_pct, _ = latest_and_change(series)
        if chg_pct is None:
            print(f"WARN: {sym}(セクター) のデータが取得できませんでした（スキップ）", file=sys.stderr)
            continue
        sectors.append({"name": name, "ticker": sym, "pct": chg_pct})
    sectors.sort(key=lambda s: s["pct"], reverse=True)

    # ---- heatmap (価格 + 時価総額) ----
    heatmap = []
    breadth_up = 0
    breadth_total = 0
    for sym in HEATMAP_SYMBOLS:
        series = fetch_eod_series(sym, days=20)
        _, chg_pct, _ = latest_and_change(series)
        if chg_pct is None:
            print(f"WARN: {sym}(ヒートマップ) のデータが取得できませんでした（スキップ）", file=sys.stderr)
            continue
        cap = fetch_market_cap(sym)
        cap_b = (cap / 1e9) if cap else 100.0  # 取得失敗時は中位ダミー値
        tier = 1 if cap_b >= 2000 else (2 if cap_b >= 800 else 3)
        heatmap.append({"t": sym, "pct": chg_pct, "cap": round(cap_b, 1), "tier": tier})
        breadth_total += 1
        if chg_pct > 0:
            breadth_up += 1

    # ---- misc: commodities + forex + rate ----
    misc = []
    for sym, name in COMMODITY_SYMBOLS:
        series = fetch_eod_series(sym, days=20)
        latest, chg_pct, _ = latest_and_change(series)
        if latest is None:
            print(f"WARN: {sym}(コモディティ) のデータが取得できませんでした（スキップ）", file=sys.stderr)
            continue
        misc.append({"name": name, "price": f"${latest:,.2f}", "pct": chg_pct})
    for sym, name in FOREX_SYMBOLS:
        series = fetch_eod_series(sym, days=20)
        latest, chg_pct, _ = latest_and_change(series)
        if latest is None:
            print(f"WARN: {sym}(為替) のデータが取得できませんでした（スキップ）", file=sys.stderr)
            continue
        prefix = "¥" if sym == "USDJPY" else "$"
        decimals = 2 if sym == "USDJPY" else 4
        misc.append({"name": name, "price": f"{prefix}{latest:,.{decimals}f}", "pct": chg_pct})
    year10, year10_date = fetch_treasury_10y()
    if year10 is not None:
        misc.append({"name": "米10年国債利回り", "price": f"{year10:.2f}%", "pct": None, "note": f"{year10_date}時点"})

    # ---- sentiment ----
    breadth = round(breadth_up / breadth_total * 100) if breadth_total else 50
    sp = index_latest.get("^GSPC")
    vix = index_latest.get("^VIX")
    momentum = 50
    if sp and sp.get("sma50"):
        momentum = round(clamp(50 + (sp["price"] - sp["sma50"]) / sp["sma50"] * 1000, 0, 100))
    vix_component = 50
    if vix and vix.get("sma50"):
        vix_component = round(clamp(50 - (vix["price"] - vix["sma50"]) / vix["sma50"] * 500, 0, 100))
    score = round((breadth + momentum + vix_component) / 3)
    label = "警戒" if score < 40 else ("楽観" if score > 60 else "中立")

    # ---- news ----
    news_raw = fetch_news(5)
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
    fetched_at = now_jst.strftime("%Y年%m月%d日 %H:%M JST 時点（FMP終値データ・自動更新）")

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

    print(f"OK: {fetched_at} で更新（指数{len(indices)}件・セクター{len(sectors)}件・"
          f"ヒートマップ{len(heatmap)}件・ニュース{len(news)}件, センチメント={score}[{label}]）")


if __name__ == "__main__":
    main()
