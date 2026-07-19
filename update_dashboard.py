#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
米国株マーケットダッシュボード 自動更新スクリプト (v3: 無料プラン確定版)

検証の結果、FMP無料プランでは以下が「Premium Query Parameter」として
アクセス不可であることが判明した:
  - セクターETF (XLC, XLY, ...) 全銘柄
  - 一部の個別銘柄 (BRK-B, LLY, AVGO, MA, PG, HD, MRK, CRM)
  - NASDAQ100指数 (^NDX)
  - 一部コモディティ (原油CLUSD, 天然ガスNGUSD)
  - news/general-latest (一般ニュース)

そのため本バージョンでは、無料プランで確実に取得できる指数4種・個別株21銘柄・
コモディティ2種・為替2種・10年国債利回りのみをAPIから取得し、
「セクター」と「ニュース」はAPI呼び出しをやめ、取得済みの21銘柄データから
自前で算出する参考値（業種別平均騰落率・値動きトップ3）に置き換えている。
FMPの有料プランにアップグレードすれば元のETF/ニュースベースに戻せる。

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
HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
REQUEST_SLEEP = 0.2

# 指数(NASDAQ100はFinnhub経由でQQQ ETFを代理指標として使用)
INDEX_SYMBOLS = [
    ("^GSPC", "S&P500"),
    ("^DJI", "NYダウ"),
    ("^RUT", "ラッセル2000"),
    ("^VIX", "VIX"),
    ("^N225", "日経225"),
    ("^KS11", "KOSPI"),
]

# メガキャップ29銘柄(Finnhub未設定時はFMPで確認済みの21銘柄のみにフォールバック)
HEATMAP_SYMBOLS_FULL = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B", "LLY",
    "AVGO", "JPM", "V", "UNH", "XOM", "MA", "JNJ", "PG", "HD", "MRK", "COST",
    "ABBV", "CVX", "KO", "PEP", "WMT", "BAC", "CRM", "NFLX", "ADBE",
]
HEATMAP_SYMBOLS_FMP_ONLY = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    "JPM", "V", "UNH", "XOM", "JNJ", "COST", "ABBV", "CVX",
    "KO", "PEP", "WMT", "BAC", "NFLX", "ADBE",
]

# セクターETF(Finnhub経由で実データ取得。未設定時はSECTOR_STOCK_MAPで代用)
SECTOR_ETF_SYMBOLS = [
    ("XLC", "通信サービス"), ("XLY", "一般消費財"), ("XLP", "生活必需品"),
    ("XLE", "エネルギー"), ("XLF", "金融"), ("XLV", "ヘルスケア"),
    ("XLI", "資本財"), ("XLB", "素材"), ("XLRE", "不動産"),
    ("XLK", "テクノロジー"), ("XLU", "公共事業"),
]

# セクター参考値の算出に使う業種グルーピング(Finnhub未設定時のフォールバック用)
SECTOR_STOCK_MAP = {
    "テクノロジー": ["AAPL", "MSFT", "NVDA", "ADBE"],
    "通信サービス": ["GOOGL", "META", "NFLX"],
    "一般消費財": ["AMZN", "TSLA"],
    "金融": ["JPM", "V", "BAC"],
    "ヘルスケア": ["UNH", "JNJ", "ABBV"],
    "エネルギー": ["XOM", "CVX"],
    "生活必需品": ["COST", "KO", "PEP", "WMT"],
}

# 無料プランで確認済みのコモディティ(原油/天然ガスは非対応のため除外)
COMMODITY_SYMBOLS = [
    ("GCUSD", "金 (Gold)"), ("SIUSD", "銀 (Silver)"),
]

FOREX_SYMBOLS = [("USDJPY", "USD/JPY"), ("EURUSD", "EUR/USD")]

MARKET_CAP_HINTS_B = {
    "AAPL": 4895, "MSFT": 2980, "GOOGL": 4287, "AMZN": 2688, "NVDA": 5023,
    "META": 1687, "TSLA": 1469, "BRK-B": 1064, "LLY": 1102, "AVGO": 1781,
    "JPM": 919, "V": 700, "UNH": 385, "XOM": 605, "MA": 487, "JNJ": 602,
    "PG": 353, "HD": 347, "MRK": 315, "COST": 419, "ABBV": 450, "CVX": 366,
    "KO": 365, "PEP": 191, "WMT": 915, "BAC": 436, "CRM": 141, "NFLX": 313,
    "ADBE": 94,
}
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY")



def _get(path, params, retries=3):
    params = dict(params)
    params["apikey"] = API_KEY
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(f"{BASE}/{path}", params=params, timeout=20)
            time.sleep(REQUEST_SLEEP)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            last_err = e
            if status in (429, 402) and attempt < retries - 1:
                wait = 2 * (attempt + 1)
                print(f"WARN: {path} で{status}。{wait}秒待って再試行 ({attempt+1}/{retries})", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
    raise last_err


def fetch_eod_series(symbol, days=90):
    to_date = datetime.now(timezone.utc).date()
    from_date = to_date - timedelta(days=days)
    try:
        rows = _get("historical-price-eod/light", {
            "symbol": symbol, "from": from_date.isoformat(), "to": to_date.isoformat(),
        })
    except requests.exceptions.HTTPError as e:
        body = ""
        try:
            body = e.response.text[:200] if e.response is not None else ""
        except Exception:
            pass
        print(f"WARN: {symbol} のEODデータ取得に失敗: {e} body={body}", file=sys.stderr)
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
    if len(series) < 2:
        return None, None, None
    latest = series[-1]["price"]
    prev = series[-2]["price"]
    chg_pct = round((latest - prev) / prev * 100, 2) if prev else None
    window = series[-50:] if len(series) >= 50 else series
    sma50 = sum(x["price"] for x in window) / len(window)
    return latest, chg_pct, sma50


def fetch_finnhub_quote(symbol, retries=3):
    """Finnhub /quote エンドポイント。現在値・前日比%を返す。失敗時は(None, None)。"""
    if not FINNHUB_KEY:
        return None, None
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(
                "https://finnhub.io/api/v1/quote",
                params={"symbol": symbol, "token": FINNHUB_KEY},
                timeout=20,
            )
            time.sleep(0.15)
            r.raise_for_status()
            data = r.json()
            price = data.get("c")
            pct = data.get("dp")
            if price is None or price == 0:
                print(f"WARN: Finnhub {symbol} のデータが空でした: {data}", file=sys.stderr)
                return None, None
            return round(float(price), 2), round(float(pct), 2) if pct is not None else None
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            last_err = e
            if status == 429 and attempt < retries - 1:
                wait = 2 * (attempt + 1)
                print(f"WARN: Finnhub {symbol} で429。{wait}秒待って再試行", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"WARN: Finnhub {symbol} 取得失敗: {e}", file=sys.stderr)
            return None, None
        except Exception as e:
            print(f"WARN: Finnhub {symbol} 取得失敗: {e}", file=sys.stderr)
            return None, None
    return None, None


def fetch_treasury_10y():
    try:
        rows = _get("treasury-rates", {})
    except requests.exceptions.HTTPError as e:
        print(f"WARN: treasury-ratesの取得に失敗: {e}", file=sys.stderr)
        return None, None
    if not isinstance(rows, list) or not rows:
        return None, None
    latest = rows[0]
    return latest.get("year10"), latest.get("date")


def fetch_alpaca_news(limit=5, symbols=None):
    """Alpaca Market Data APIのNews(無料プラン含む)。
    symbolsを指定すると、その銘柄に関連する記事に絞り込む。
    ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY が未設定、または
    サブスクリプションで許可されていない場合は空リストを返す。"""
    key_id = os.environ.get("ALPACA_API_KEY_ID")
    secret = os.environ.get("ALPACA_API_SECRET_KEY")
    if not key_id or not secret:
        print("INFO: ALPACA_API_KEY_ID/SECRET未設定のためAlpacaニュースはスキップ", file=sys.stderr)
        return []
    params = {"limit": limit, "sort": "desc"}
    if symbols:
        params["symbols"] = ",".join(symbols)
    try:
        r = requests.get(
            "https://data.alpaca.markets/v1beta1/news",
            params=params,
            headers={"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret},
            timeout=20,
        )
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        print(f"WARN: Alpaca news取得に失敗: {e}", file=sys.stderr)
        return []
    articles = payload.get("news", []) if isinstance(payload, dict) else []
    items = []
    for a in articles[:limit]:
        title = a.get("headline", "")
        if not title:
            continue
        summary = str(a.get("summary") or "").strip()
        if len(summary) > 130:
            summary = summary[:130].rstrip() + "…"
        items.append({
            "pub": a.get("source", "Alpaca"),
            "title": title,
            "desc": summary,
            "url": a.get("url", ""),
        })
    return items


def fetch_yahoo_news(limit=5, symbols=None):
    """yfinance(Yahoo Financeの非公式ラッパー)からニュースを取得。
    失敗しても例外は投げず、空リストを返す(呼び出し側でmoversにフォールバック)。"""
    try:
        import yfinance as yf
    except Exception as e:
        print(f"WARN: yfinanceのimportに失敗: {e}", file=sys.stderr)
        return []

    symbols_to_try = symbols if symbols else ["^GSPC", "AAPL", "MSFT", "NVDA"]
    seen_titles = set()
    items = []
    for sym in symbols_to_try:
        try:
            raw_list = yf.Ticker(sym).news or []
        except Exception as e:
            print(f"WARN: yfinance news取得失敗({sym}): {e}", file=sys.stderr)
            continue
        for raw in raw_list:
            content = raw.get("content") if isinstance(raw.get("content"), dict) else raw
            title = content.get("title") or raw.get("title")
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)
            provider = content.get("provider")
            pub = provider.get("displayName") if isinstance(provider, dict) else (raw.get("publisher") or "Yahoo Finance")
            canon = content.get("canonicalUrl")
            url = canon.get("url") if isinstance(canon, dict) else raw.get("link", "")
            summary = content.get("summary") or content.get("description") or ""
            summary = str(summary).strip()
            if len(summary) > 130:
                summary = summary[:130].rstrip() + "…"
            items.append({"pub": pub, "title": title, "desc": summary, "url": url})
            if len(items) >= limit:
                break
        if len(items) >= limit:
            break
    return items[:limit]


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

    # NASDAQ100はFMP無料プランで非対応のため、Finnhub経由でQQQ(ETF)を代理指標として追加
    if FINNHUB_KEY:
        qqq_price, qqq_pct = fetch_finnhub_quote("QQQ")
        if qqq_price is not None:
            indices.insert(1, {
                "name": "NASDAQ100(QQQ)", "symbol": "QQQ",
                "price": qqq_price,
                "chg": round(qqq_price * qqq_pct / 100, 2) if qqq_pct is not None else 0,
                "pct": qqq_pct,
            })

    # KOSPI(^KS11)がFMPで取得できなかった場合、Finnhub経由で韓国ETF(EWY)を代理指標として追加
    if FINNHUB_KEY and not any(i["symbol"] == "^KS11" for i in indices):
        ewy_price, ewy_pct = fetch_finnhub_quote("EWY")
        if ewy_price is not None:
            indices.append({
                "name": "韓国(KOSPI代替:EWY)", "symbol": "EWY",
                "price": ewy_price,
                "chg": round(ewy_price * ewy_pct / 100, 2) if ewy_pct is not None else 0,
                "pct": ewy_pct,
            })

    # ---- heatmap ----
    heatmap = []
    breadth_up = 0
    breadth_total = 0
    if FINNHUB_KEY:
        for sym in HEATMAP_SYMBOLS_FULL:
            price, pct = fetch_finnhub_quote(sym)
            if pct is None:
                print(f"WARN: {sym}(ヒートマップ/Finnhub) のデータが取得できませんでした（スキップ）", file=sys.stderr)
                continue
            cap_b = MARKET_CAP_HINTS_B.get(sym, 100.0)
            tier = 1 if cap_b >= 2000 else (2 if cap_b >= 800 else 3)
            heatmap.append({"t": sym, "pct": pct, "cap": round(cap_b, 1), "tier": tier})
            breadth_total += 1
            if pct > 0:
                breadth_up += 1
    else:
        for sym in HEATMAP_SYMBOLS_FMP_ONLY:
            series = fetch_eod_series(sym, days=20)
            _, chg_pct, _ = latest_and_change(series)
            if chg_pct is None:
                print(f"WARN: {sym}(ヒートマップ) のデータが取得できませんでした（スキップ）", file=sys.stderr)
                continue
            cap_b = MARKET_CAP_HINTS_B.get(sym, 100.0)
            tier = 1 if cap_b >= 2000 else (2 if cap_b >= 800 else 3)
            heatmap.append({"t": sym, "pct": chg_pct, "cap": round(cap_b, 1), "tier": tier})
            breadth_total += 1
            if chg_pct > 0:
                breadth_up += 1

    heatmap_pct = {h["t"]: h["pct"] for h in heatmap}

    # ---- sectors: Finnhub経由でセクターETFの実データを取得。未設定時は21銘柄からの参考値 ----
    sectors = []
    if FINNHUB_KEY:
        for sym, name in SECTOR_ETF_SYMBOLS:
            price, pct = fetch_finnhub_quote(sym)
            if pct is None:
                print(f"WARN: {sym}(セクターETF/Finnhub) のデータが取得できませんでした（スキップ）", file=sys.stderr)
                continue
            sectors.append({"name": name, "ticker": sym, "pct": pct})
    if not sectors:
        for name, members in SECTOR_STOCK_MAP.items():
            vals = [heatmap_pct[m] for m in members if m in heatmap_pct]
            if not vals:
                continue
            avg = round(sum(vals) / len(vals), 2)
            sectors.append({"name": name, "ticker": f"{len(vals)}銘柄平均", "pct": avg})
    sectors.sort(key=lambda s: s["pct"], reverse=True)

    # ---- movers (ニュース取得に失敗した場合のフォールバック用) ----
    sorted_heatmap = sorted(heatmap, key=lambda h: h["pct"], reverse=True)
    movers = {
        "gainers": [{"t": h["t"], "pct": h["pct"]} for h in sorted_heatmap[:3]],
        "losers": [{"t": h["t"], "pct": h["pct"]} for h in sorted_heatmap[-3:][::-1]],
    }

    # ---- news: 値動き上位銘柄に関連するニュースを優先。
    #      Alpaca(銘柄絞り込み) → Alpaca(一般) → Yahoo Finance → 失敗時はmoversで代替表示 ----
    mover_symbols = [m["t"] for m in movers["gainers"]] + [m["t"] for m in movers["losers"]]
    news = fetch_alpaca_news(5, symbols=mover_symbols)
    news_source = "alpaca-movers"
    if not news:
        news = fetch_alpaca_news(5)
        news_source = "alpaca-general"
    if not news:
        news = fetch_yahoo_news(5, symbols=mover_symbols)
        news_source = "yahoo"
    if not news:
        news_source = "none"
        print("WARN: ニュースが0件のため、フロント側でmoversにフォールバック表示されます", file=sys.stderr)
    else:
        print(f"INFO: ニュースは{news_source}経由で{len(news)}件取得", file=sys.stderr)

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
        "movers": movers,
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

    print(f"OK: {fetched_at} で更新（指数{len(indices)}件・セクター参考値{len(sectors)}件・"
          f"ヒートマップ{len(heatmap)}件・ニュース{len(news)}件, センチメント={score}[{label}]）")


if __name__ == "__main__":
    main()
