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

# S&P500 時価総額上位100銘柄(概算。細かい入れ替わりは許容し、取得失敗銘柄は自動でスキップされる)
SP500_TOP100 = [
    "AAPL","MSFT","NVDA","GOOGL","AMZN","META","AVGO","TSLA","BRK-B","JPM",
    "LLY","V","XOM","MA","COST","UNH","JNJ","HD","PG","NFLX",
    "ABBV","BAC","CRM","ORCL","MRK","CVX","KO","PEP","WMT","ADBE",
    "MCD","DIS","ABT","WFC","CSCO","TMO","ACN","LIN","DHR","VZ",
    "NEE","PM","TXN","UNP","RTX","LOW","INTC","AMGN","IBM","SPGI",
    "CAT","GE","HON","NKE","BA","AXP","GS","MS","BLK","SCHW",
    "DE","ELV","SYK","MDT","LMT","BKNG","ADI","PLD","GILD","MMC",
    "T","C","TJX","VRTX","CB","MO","SO","DUK","ADP","REGN",
    "NOW","ZTS","BSX","CME","PGR","EOG","SLB","ETN","AON","ITW",
    "EQIX","APD","CL","FDX","USB","PNC","MU","NSC","WM","ICE",
]

# NASDAQ-100 構成銘柄(概算。細かい入れ替わりは許容)
NASDAQ100_FULL = [
    "AAPL","MSFT","GOOGL","GOOG","AMZN","NVDA","META","TSLA","AVGO","COST",
    "ASML","PEP","AZN","ADBE","NFLX","AMD","CSCO","TMUS","INTU","QCOM",
    "TXN","CMCSA","AMAT","HON","BKNG","VRTX","ISRG","PANW","ADP","SBUX",
    "MU","GILD","LRCX","MDLZ","REGN","ADI","PYPL","SNPS","KLAC","CDNS",
    "MELI","CTAS","MAR","ORLY","CSX","ABNB","PDD","WDAY","CRWD","MRVL",
    "DASH","FTNT","ROP","NXPI","MNST","PCAR","PAYX","ROST","ODFL","KDP",
    "EA","FAST","VRSK","GEHC","IDXX","CPRT","DXCM","EXC","CTSH","XEL",
    "BKR","KHC","TTD","ANSS","ON","CCEP","DDOG","TEAM","ZS","MCHP",
    "GFS","ILMN","WBD","BIIB","CDW","LULU","SIRI","ENPH","ARM","APP",
    "AXON","CEG","FANG","TTWO","WDC","GEN","DLTR","MDB","LCID","CSGP",
]

SENTIMENT_HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sentiment_history.json")



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


def translate_news_items(items):
    """ニュースのtitle/descを日本語に翻訳する。失敗した記事は原文のまま残す。"""
    if not items:
        return items
    try:
        from deep_translator import GoogleTranslator
    except Exception as e:
        print(f"WARN: deep_translatorのimportに失敗、翻訳をスキップ: {e}", file=sys.stderr)
        return items
    translator = GoogleTranslator(source="en", target="ja")
    for item in items:
        try:
            if item.get("title"):
                item["title"] = translator.translate(item["title"][:400])
        except Exception as e:
            print(f"WARN: タイトル翻訳失敗: {e}", file=sys.stderr)
        try:
            if item.get("desc"):
                item["desc"] = translator.translate(item["desc"][:400])
        except Exception as e:
            print(f"WARN: 本文翻訳失敗: {e}", file=sys.stderr)
        time.sleep(0.3)
    return items


def fetch_finnhub_candle(symbol, days_back=280, retries=3):
    """Finnhub /stock/candle。日足の時系列を{date,close}のリスト(日付昇順)で返す。失敗時は[]。"""
    if not FINNHUB_KEY:
        return []
    to_ts = int(time.time())
    from_ts = to_ts - days_back * 86400
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(
                "https://finnhub.io/api/v1/stock/candle",
                params={"symbol": symbol, "resolution": "D", "from": from_ts, "to": to_ts, "token": FINNHUB_KEY},
                timeout=20,
            )
            time.sleep(0.15)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict) or data.get("s") != "ok":
                return []
            closes = data.get("c", [])
            times = data.get("t", [])
            out = [{"date": datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"), "price": float(c)}
                   for t, c in zip(times, closes)]
            out.sort(key=lambda x: x["date"])
            return out
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            last_err = e
            if status == 429 and attempt < retries - 1:
                wait = 2 * (attempt + 1)
                time.sleep(wait)
                continue
            print(f"WARN: Finnhub candle {symbol} 取得失敗: {e}", file=sys.stderr)
            return []
        except Exception as e:
            print(f"WARN: Finnhub candle {symbol} 取得失敗: {e}", file=sys.stderr)
            return []
    return []


def fetch_finnhub_pe(symbol, retries=2):
    """Finnhub /stock/metric。実績PER(peBasicExclExtraTTM系)を返す。失敗時はNone。"""
    if not FINNHUB_KEY:
        return None
    for attempt in range(retries):
        try:
            r = requests.get(
                "https://finnhub.io/api/v1/stock/metric",
                params={"symbol": symbol, "metric": "all", "token": FINNHUB_KEY},
                timeout=20,
            )
            time.sleep(0.15)
            r.raise_for_status()
            data = r.json()
            metric = data.get("metric", {}) if isinstance(data, dict) else {}
            pe = metric.get("peBasicExclExtraTTM") or metric.get("peExclExtraTTM") or metric.get("peNormalizedAnnual")
            return round(float(pe), 1) if pe is not None else None
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status == 429 and attempt < retries - 1:
                time.sleep(2)
                continue
            print(f"WARN: Finnhub metric {symbol} 取得失敗: {e}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"WARN: Finnhub metric {symbol} 取得失敗: {e}", file=sys.stderr)
            return None
    return None


def series_stats(series):
    """candleシリーズから 現在値・前日比%・1ヶ月リターン%・50日/200日移動平均・52週高安 を計算"""
    if len(series) < 2:
        return None
    latest = series[-1]["price"]
    prev = series[-2]["price"]
    daily_pct = round((latest - prev) / prev * 100, 2) if prev else None
    idx_1mo = max(0, len(series) - 22)
    price_1mo_ago = series[idx_1mo]["price"]
    ret_1mo = round((latest - price_1mo_ago) / price_1mo_ago * 100, 2) if price_1mo_ago else None
    sma50 = sum(x["price"] for x in series[-50:]) / len(series[-50:])
    sma200_window = series[-200:] if len(series) >= 200 else series
    sma200 = sum(x["price"] for x in sma200_window) / len(sma200_window)
    year_window = series[-252:] if len(series) >= 252 else series
    high52 = max(x["price"] for x in year_window)
    low52 = min(x["price"] for x in year_window)
    return {
        "latest": latest, "daily_pct": daily_pct, "ret_1mo": ret_1mo,
        "sma50": sma50, "sma200": sma200, "high52": high52, "low52": low52,
        "has200": len(series) >= 200,
    }


def load_sentiment_history():
    try:
        with open(SENTIMENT_HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_sentiment_history(history):
    try:
        with open(SENTIMENT_HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(history[-400:], f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"WARN: sentiment_history.jsonの保存に失敗: {e}", file=sys.stderr)


def lookup_history_score(history, days_ago, today):
    target = today - timedelta(days=days_ago)
    best = None
    best_diff = None
    for row in history:
        try:
            d = datetime.strptime(row["date"], "%Y-%m-%d").date()
        except Exception:
            continue
        diff = abs((d - target).days)
        if diff <= 3 and (best_diff is None or diff < best_diff):
            best = row["score"]
            best_diff = diff
    return best


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
        news = translate_news_items(news)
        print("INFO: ニュースの日本語翻訳を試行しました", file=sys.stderr)

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
    today_date = now_jst.date()
    fetched_at = now_jst.strftime("%Y年%m月%d日 %H:%M JST 時点（FMP終値データ・自動更新）")

    # ---- センチメント履歴(Fear&Greed風のトレンド表示用) ----
    history = load_sentiment_history()
    history.append({"date": today_date.isoformat(), "score": score})
    save_sentiment_history(history)
    sentiment_trend = {
        "prevDay": lookup_history_score(history[:-1], 1, today_date),
        "weekAgo": lookup_history_score(history[:-1], 7, today_date),
        "monthAgo": lookup_history_score(history[:-1], 30, today_date),
        "yearAgo": lookup_history_score(history[:-1], 365, today_date),
    }

    # ---- S&P500 / NASDAQ100 ユニバース(前日比・実績PER・レラティブストレングス) ----
    universes = {"sp500": [], "nasdaq100": []}
    market_breadth = None
    sector_rotation = []

    if FINNHUB_KEY:
        symbol_cache = {}

        def get_symbol_data(sym):
            if sym not in symbol_cache:
                series = fetch_finnhub_candle(sym)
                stats = series_stats(series)
                pe = fetch_finnhub_pe(sym)
                symbol_cache[sym] = (stats, pe)
            return symbol_cache[sym]

        # ベンチマーク: S&P500は^GSPCの1ヶ月リターン、NASDAQ100はQQQの1ヶ月リターン
        gspc_series = fetch_finnhub_candle("SPY")
        gspc_stats = series_stats(gspc_series)
        qqq_series = fetch_finnhub_candle("QQQ")
        qqq_stats = series_stats(qqq_series)
        bench_ret = {
            "sp500": gspc_stats["ret_1mo"] if gspc_stats and gspc_stats.get("ret_1mo") is not None else 0,
            "nasdaq100": qqq_stats["ret_1mo"] if qqq_stats and qqq_stats.get("ret_1mo") is not None else 0,
        }

        for uni_key, symbols in (("sp500", SP500_TOP100), ("nasdaq100", NASDAQ100_FULL)):
            for sym in symbols:
                stats, pe = get_symbol_data(sym)
                if not stats or stats.get("daily_pct") is None:
                    print(f"WARN: {sym}({uni_key}) のデータが取得できませんでした（スキップ）", file=sys.stderr)
                    continue
                rs = None
                if stats.get("ret_1mo") is not None:
                    rs = round(stats["ret_1mo"] - bench_ret[uni_key], 2)
                universes[uni_key].append({
                    "t": sym, "pct": stats["daily_pct"], "pe": pe, "rs": rs,
                })

        # ---- 市場の広がり(S&P500上位100銘柄ベース) ----
        above50 = above200 = newhigh = newlow = advancers = decliners = counted200 = 0
        total = 0
        for sym in SP500_TOP100:
            stats, _ = get_symbol_data(sym)
            if not stats:
                continue
            total += 1
            if stats["daily_pct"] is not None:
                if stats["daily_pct"] > 0:
                    advancers += 1
                elif stats["daily_pct"] < 0:
                    decliners += 1
            if stats["latest"] > stats["sma50"]:
                above50 += 1
            if stats.get("has200"):
                counted200 += 1
                if stats["latest"] > stats["sma200"]:
                    above200 += 1
            if stats["latest"] >= stats["high52"] * 0.999:
                newhigh += 1
            if stats["latest"] <= stats["low52"] * 1.001:
                newlow += 1
        if total > 0:
            market_breadth = {
                "total": total,
                "advancers": advancers, "decliners": decliners,
                "pctAbove50": round(above50 / total * 100, 1),
                "pctAbove200": round(above200 / counted200 * 100, 1) if counted200 else None,
                "newHigh": newhigh, "newLow": newlow,
            }

        # ---- セクターローテーション(直近7営業日 vs その前7営業日、SPY相対) ----
        spy_series = gspc_series
        if spy_series and len(spy_series) >= 15:
            spy_recent7 = spy_series[-1]["price"] / spy_series[-8]["price"] - 1
            spy_prior7 = spy_series[-8]["price"] / spy_series[-15]["price"] - 1
            for sym, name in SECTOR_ETF_SYMBOLS:
                sec_series = fetch_finnhub_candle(sym, days_back=40)
                if len(sec_series) < 15:
                    continue
                sec_recent7 = sec_series[-1]["price"] / sec_series[-8]["price"] - 1
                sec_prior7 = sec_series[-8]["price"] / sec_series[-15]["price"] - 1
                rel_recent = round((sec_recent7 - spy_recent7) * 100, 2)
                rel_prior = round((sec_prior7 - spy_prior7) * 100, 2)
                if rel_recent >= 0 and rel_recent >= rel_prior:
                    quadrant = "leading"
                elif rel_recent >= 0 and rel_recent < rel_prior:
                    quadrant = "weakening"
                elif rel_recent < 0 and rel_recent >= rel_prior:
                    quadrant = "improving"
                else:
                    quadrant = "lagging"
                sector_rotation.append({
                    "name": name, "ticker": sym,
                    "recent": rel_recent, "prior": rel_prior, "quadrant": quadrant,
                })

    data = {
        "fetchedAt": fetched_at,
        "indices": indices,
        "misc": misc,
        "sentiment": {"score": score, "breadth": breadth, "momentum": momentum, "vix": vix_component, "label": label},
        "sentimentTrend": sentiment_trend,
        "sectors": sectors,
        "heatmap": heatmap,
        "movers": movers,
        "news": news,
        "universes": universes,
        "marketBreadth": market_breadth,
        "sectorRotation": sector_rotation,
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
