#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
朝チェック用データの組み立て。

update_dashboard.py から呼ばれ、DASHBOARD_DATA["morningCheck"] に入る辞書を作る。
判定（縮小・撤退・買い）そのものは index.html の JavaScript が、
ブラウザで手入力された値（日経VI・PC ratio・地政学リスク）と合わせて行う。
ここでは「自動で取れる値」と「取れなかった理由」だけを用意する。

取れなかった値は None のまま渡し、推測で埋めない。
"""
import csv
import io
import sys
from datetime import date, datetime, timedelta

import requests

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"


def _warn(msg):
    print(f"WARN: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 日経225
# ---------------------------------------------------------------------------

def fetch_nikkei_ohlc():
    """yfinance の ^N225 全期間の日足(始値・高値・安値・終値)。
    戻り値: (rows, source) 。rows は日付昇順の dict のリスト。取れなければ ([], None)。"""
    try:
        import yfinance as yf
        hist = yf.Ticker("^N225").history(period="max", auto_adjust=False)
        if hist is None or hist.empty:
            _warn("yfinance ^N225 が空でした")
            return [], None
        rows = []
        for idx, r in hist.iterrows():
            try:
                c = float(r["Close"])
                o = float(r["Open"])
                h = float(r["High"])
                l = float(r["Low"])
            except (TypeError, ValueError):
                continue
            if c != c or o != o or h != h or l != l:  # NaN
                continue
            rows.append({"date": idx.date().isoformat(), "o": o, "h": h, "l": l, "c": c})
        rows.sort(key=lambda x: x["date"])
        return rows, "yfinance ^N225"
    except Exception as e:  # noqa: BLE001 - 取れなければFMP終値のみにフォールバック
        _warn(f"yfinance ^N225 の取得に失敗: {e}")
        return [], None


def rows_from_close_series(series):
    """FMP の終値系列 [{date, price}] を OHLC 形式に変換（始値・高値・安値は None）。"""
    return [{"date": s["date"], "o": None, "h": None, "l": None, "c": float(s["price"])}
            for s in series]


def summarize_nikkei(rows, source, recent_n=5):
    """日経225の日足から、朝チェックに必要な値をまとめる。

    - 直近 recent_n 営業日の始値・終値・前日比・陰線判定
    - 終値ベースの史上最高値(取得期間内)と、日中高値ベースの最高値(OHLCがある場合のみ)
    - 最新終値の、それぞれからの下落率
    """
    if len(rows) < 2:
        return None
    recent = []
    start = max(1, len(rows) - recent_n)
    for i in range(start, len(rows)):
        cur, prev = rows[i], rows[i - 1]
        chg = cur["c"] - prev["c"]
        has_open = cur["o"] is not None
        recent.append({
            "date": cur["date"],
            "o": round(cur["o"], 2) if has_open else None,
            "h": round(cur["h"], 2) if cur["h"] is not None else None,
            "l": round(cur["l"], 2) if cur["l"] is not None else None,
            "c": round(cur["c"], 2),
            "chg": round(chg, 2),
            "chgPct": round(chg / prev["c"] * 100, 2) if prev["c"] else None,
            "range": round(cur["h"] - cur["l"], 2) if cur["h"] is not None and cur["l"] is not None else None,
            # 陰線 = 終値 < 始値（ローソク足）。始値が無ければ None（JS側で代用定義に切替）
            "bearish": (cur["c"] < cur["o"]) if has_open else None,
        })
    best_c = max(rows, key=lambda r: r["c"])
    last_close = rows[-1]["c"]
    ath_close = {"value": round(best_c["c"], 2), "date": best_c["date"],
                 "ddPct": round((last_close / best_c["c"] - 1) * 100, 2)}
    ath_high = None
    with_high = [r for r in rows if r["h"] is not None]
    if with_high:
        best_h = max(with_high, key=lambda r: r["h"])
        ath_high = {"value": round(best_h["h"], 2), "date": best_h["date"],
                    "ddPct": round((last_close / best_h["h"] - 1) * 100, 2)}
    return {
        "source": source,
        "hasOpen": all(r["bearish"] is not None for r in recent),
        "periodFrom": rows[0]["date"],
        "periodTo": rows[-1]["date"],
        "recent": recent,
        "lastClose": round(last_close, 2),
        "athClose": ath_close,
        "athHigh": ath_high,
    }


def fetch_nikkei_vi_auto():
    """日経VIの自動取得（yfinanceの2シンボルを試すだけ）。
    sq_fetch.py と同じ方法だが、これまでの履歴(sq_history.json)では一度も取れていない。
    取れなければ None。ページ側の手入力で補う。"""
    try:
        import yfinance as yf
        for symbol in ("^N225VI", "^NKVI"):
            hist = yf.Ticker(symbol).history(period="5d")
            if hist is not None and not hist.empty:
                last = hist.iloc[-1]
                return {"value": round(float(last["Close"]), 2),
                        "date": hist.index[-1].date().isoformat(),
                        "source": f"yfinance {symbol}（終値）"}
    except Exception as e:  # noqa: BLE001
        _warn(f"日経VIの自動取得に失敗: {e}")
    return None


# ---------------------------------------------------------------------------
# 債券
# ---------------------------------------------------------------------------

def fetch_fred_last(series_id):
    """FREDの公開CSVから最新の有効値を返す: {"value","date"}。取れなければ None。
    FREDの系列: DAAA = Moody's Seasoned Aaa Corporate Bond Yield,
                DBAA = Moody's Seasoned Baa Corporate Bond Yield（いずれも日次・%）。"""
    try:
        r = requests.get(FRED_CSV, params={"id": series_id}, timeout=30)
        r.raise_for_status()
        reader = csv.reader(io.StringIO(r.text))
        next(reader, None)  # ヘッダ
        last = None
        for row in reader:
            if len(row) < 2:
                continue
            try:
                last = {"date": row[0], "value": float(row[1])}
            except ValueError:
                continue  # 欠損は "." で入る
        return last
    except Exception as e:  # noqa: BLE001
        _warn(f"FRED {series_id} の取得に失敗: {e}")
        return None


def build_credit_spread():
    baa = fetch_fred_last("DBAA")
    aaa = fetch_fred_last("DAAA")
    if not baa or not aaa:
        return None
    # 日付が揃っていない場合は古い方に合わせず、そのまま差を出しつつ日付を両方残す
    return {
        "baa": baa["value"], "baaDate": baa["date"],
        "aaa": aaa["value"], "aaaDate": aaa["date"],
        "spread": round(baa["value"] - aaa["value"], 2),
        "source": "FRED DBAA − DAAA（Moody's 長期社債利回り）",
    }


def build_treasury(treasury_rows):
    """FMP treasury-rates の先頭行から2年・10年を取り出す。"""
    if not isinstance(treasury_rows, list) or not treasury_rows:
        return None
    r = treasury_rows[0]
    y2, y10 = r.get("year2"), r.get("year10")
    if y2 is None and y10 is None:
        return None
    return {
        "date": r.get("date"), "y2": y2, "y10": y10,
        "slope": round(y10 - y2, 2) if (y2 is not None and y10 is not None) else None,
        "source": "FMP treasury-rates",
    }


# ---------------------------------------------------------------------------
# 日本の営業日カレンダー（月末月初・MSQの判定用）
# ---------------------------------------------------------------------------

def jp_market_holidays(start_year, end_year):
    """東証の休場日(YYYY-MM-DD)のリスト。
    jpholiday があれば祝日を使う。無ければ祝日は考慮できない。
    年末年始(12/31, 1/1-1/3)は祝日ではないが東証は休場なので加える。
    戻り値: (日付リスト, 祝日を考慮できたか)"""
    days = set()
    known = False
    try:
        import jpholiday
        known = True
        d = date(start_year, 1, 1)
        end = date(end_year, 12, 31)
        while d <= end:
            if jpholiday.is_holiday(d):
                days.add(d.isoformat())
            d += timedelta(days=1)
    except ImportError:
        _warn("jpholiday が未インストールのため、祝日は考慮されません（土日のみ）")
    for y in range(start_year, end_year + 1):
        days.add(f"{y}-12-31")
        for dd in (1, 2, 3):
            days.add(f"{y}-01-{dd:02d}")
    return sorted(days), known


# ---------------------------------------------------------------------------
# 組み立て
# ---------------------------------------------------------------------------

def build_morning_check(nikkei_rows, nikkei_source, vix, treasury_rows, now=None):
    """DASHBOARD_DATA["morningCheck"] を作る。
    vix: {"value","date","pct"} または None。"""
    now = now or datetime.utcnow()
    nikkei = summarize_nikkei(nikkei_rows, nikkei_source) if nikkei_rows else None
    holidays, known = jp_market_holidays(now.year - 1, now.year + 2)
    return {
        "nikkei": nikkei,
        "nikkeiVi": fetch_nikkei_vi_auto(),
        "vix": vix,
        "treasury": build_treasury(treasury_rows),
        "credit": build_credit_spread(),
        "calendar": {"holidays": holidays, "holidaysKnown": known},
    }
