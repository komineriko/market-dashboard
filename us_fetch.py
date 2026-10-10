"""米国株オプションのデータ取得。

  日足          FMP (historical-price-eod/full)。高値・安値が要るので light は使わない
  決算予定      FMP (earnings-calendar)。期間を1回引いて銘柄で索く
  オプション板  yfinance。行使価格別の気配・建玉が無料で取れるのはここだけ

取得できなかったものは黙って埋めず、呼び出し側に None を返して開示させる。
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import us_indicators as ui
import us_options as uo

FMP_BASE = "https://financialmodelingprep.com/stable"
FMP_KEY = os.environ.get("FMP_API_KEY")
REQUEST_SLEEP = float(os.environ.get("US_REQUEST_SLEEP", "0.15"))
CHAIN_SLEEP = float(os.environ.get("US_CHAIN_SLEEP", "0.25"))


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# ユニバース
# ---------------------------------------------------------------------------

# オプションの出来高ランキングは無料では日次で取れないため、流動性のある
# 銘柄を固定で持つ。日々の入れ替わりを追えないのは制約として開示する。
UNIVERSE: Tuple[str, ...] = (
    # メガキャップ・半導体
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD",
    "MU", "INTC", "TSM", "QCOM", "ARM", "MRVL", "SMCI",
    # ソフトウェア・プラットフォーム
    "NFLX", "CRM", "ORCL", "ADBE", "NOW", "PLTR", "SNOW", "CRWD", "PANW",
    "UBER", "ABNB", "SHOP", "COIN", "MSTR",
    # 景気・ディフェンシブ
    "JPM", "BAC", "XOM", "CVX", "LLY", "UNH", "WMT", "COST", "DIS", "BA",
    "CAT", "GE", "F", "T", "PFE",
    # ETF
    "SPY", "QQQ", "IWM", "DIA", "SMH", "XLK", "XLF", "XLE", "XLV",
    "TQQQ", "GLD", "SLV", "USO", "TLT", "EEM", "EWJ", "HYG", "ARKK",
)

HOLDINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "us_holdings.json")


def load_holdings() -> Optional[List[str]]:
    """保有銘柄。ファイルが無ければ None（＝保有は不明）。

    None のときカバードコールは「もし持っていれば」という前提で全銘柄を出す。
    ファイルがあればその銘柄だけに絞る。
    """
    if not os.path.exists(HOLDINGS_PATH):
        return None
    try:
        with open(HOLDINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        log(f"WARN: 保有銘柄ファイルを読めませんでした: {e}")
        return None
    if isinstance(data, dict):
        data = data.get("symbols", [])
    if not isinstance(data, list):
        return None
    return [str(s).upper() for s in data if s]


# ---------------------------------------------------------------------------
# FMP
# ---------------------------------------------------------------------------

def _fmp(path: str, params: Dict[str, object], retries: int = 3):
    import requests
    if not FMP_KEY:
        raise RuntimeError("FMP_API_KEY が設定されていません")
    p = dict(params)
    p["apikey"] = FMP_KEY
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(f"{FMP_BASE}/{path}", params=p, timeout=25)
            time.sleep(REQUEST_SLEEP)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001 - 失敗の種類ごとの扱いは下で決める
            last = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (429, 402) and attempt < retries - 1:
                wait = 2 * (attempt + 1)
                log(f"WARN: {path} が {status}。{wait}秒待って再試行")
                time.sleep(wait)
                continue
            raise
    raise last


def fetch_bars(symbol: str, days: int = 400) -> List[ui.Bar]:
    to_d = datetime.now(timezone.utc).date()
    from_d = to_d - timedelta(days=days)
    try:
        rows = _fmp("historical-price-eod/full",
                    {"symbol": symbol, "from": from_d.isoformat(), "to": to_d.isoformat()})
    except Exception as e:  # noqa: BLE001
        log(f"WARN: {symbol} の日足を取得できませんでした: {e}")
        return []
    if not isinstance(rows, list):
        return []
    out: List[ui.Bar] = []
    for r in rows:
        try:
            out.append(ui.Bar(r["date"], float(r["high"]), float(r["low"]),
                              float(r["close"])))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda b: b.date)
    return out


def fetch_earnings_map(ahead_days: int = 60) -> Dict[str, str]:
    """今日から ahead_days 先までの決算予定を {銘柄: 最初の日付} で返す。"""
    today = datetime.now(timezone.utc).date()
    try:
        rows = _fmp("earnings-calendar",
                    {"from": today.isoformat(),
                     "to": (today + timedelta(days=ahead_days)).isoformat()})
    except Exception as e:  # noqa: BLE001
        log(f"WARN: 決算予定を取得できませんでした: {e}")
        return {}
    out: Dict[str, str] = {}
    if not isinstance(rows, list):
        return out
    for r in rows:
        sym, d = r.get("symbol"), r.get("date")
        if not sym or not d:
            continue
        if sym not in out or d < out[sym]:
            out[sym] = d
    return out


# ---------------------------------------------------------------------------
# オプション板（yfinance）
# ---------------------------------------------------------------------------

def _num(v, default=None):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:          # NaN
        return default
    return f


def chain_from_frames(calls, puts) -> List[uo.StrikeQuote]:
    """yfinance の2つの DataFrame を行使価格でつき合わせる。"""
    rows: Dict[float, uo.StrikeQuote] = {}

    def absorb(df, is_call: bool):
        if df is None:
            return
        for rec in df.to_dict("records"):
            k = _num(rec.get("strike"))
            if k is None or k <= 0:
                continue
            row = rows.setdefault(k, uo.StrikeQuote(strike=k))
            q = uo.Quote(
                bid=_num(rec.get("bid")),
                ask=_num(rec.get("ask")),
                last=_num(rec.get("lastPrice")),
                volume=int(_num(rec.get("volume"), 0) or 0),
                oi=int(_num(rec.get("openInterest"), 0) or 0),
            )
            if is_call:
                row.call = q
            else:
                row.put = q

    absorb(calls, True)
    absorb(puts, False)
    return [rows[k] for k in sorted(rows)]


def fetch_expiries(symbol: str, asof: date, max_dte: int = uo.MAX_DTE
                   ) -> List[uo.Expiry]:
    """max_dte 以内の満期をすべて取る。

    手前の満期も取るのは、ウォールを「その満期までの全満期を合算」で出すため。
    当週の建玉を落とすと壁が実勢より薄く出る。
    """
    import yfinance as yf
    try:
        tk = yf.Ticker(symbol)
        dates = list(tk.options or [])
    except Exception as e:  # noqa: BLE001
        log(f"WARN: {symbol} の満期一覧を取得できませんでした: {e}")
        return []

    out: List[uo.Expiry] = []
    for d in dates:
        try:
            dte = (date.fromisoformat(d) - asof).days
        except ValueError:
            continue
        if dte < 0 or dte > max_dte:
            continue
        try:
            ch = tk.option_chain(d)
            time.sleep(CHAIN_SLEEP)
        except Exception as e:  # noqa: BLE001
            log(f"WARN: {symbol} {d} の板を取得できませんでした: {e}")
            continue
        rows = chain_from_frames(ch.calls, ch.puts)
        if rows:
            out.append(uo.Expiry(expiry=d, rows=rows))
    return out


# ---------------------------------------------------------------------------
# まとめ
# ---------------------------------------------------------------------------

@dataclass
class FetchReport:
    asof: Optional[date] = None
    ok: List[str] = None
    no_bars: List[str] = None
    no_chain: List[str] = None
    earnings_blocked: List[str] = None
    earnings_available: bool = True

    def __post_init__(self):
        for f in ("ok", "no_bars", "no_chain", "earnings_blocked"):
            if getattr(self, f) is None:
                setattr(self, f, [])


def load_universe(symbols: Sequence[str] = UNIVERSE,
                  asof: Optional[date] = None,
                  holdings: Optional[List[str]] = None,
                  ) -> Tuple[List[uo.Underlying], FetchReport]:
    rep = FetchReport()
    earnings = fetch_earnings_map()
    rep.earnings_available = bool(earnings)
    held_set = set(holdings) if holdings is not None else None

    out: List[uo.Underlying] = []
    for sym in symbols:
        bars = fetch_bars(sym)
        if len(bars) < 60:
            rep.no_bars.append(sym)
            continue
        # 板の日付は日足の最終日に合わせる。両者がずれると乖離もIVもずれる。
        board_date = asof or date.fromisoformat(bars[-1].date)
        if rep.asof is None:
            rep.asof = board_date
        if bars[-1].close < uo.MIN_UNDERLYING:
            continue
        exps = fetch_expiries(sym, board_date)
        if not exps:
            rep.no_chain.append(sym)
            continue
        u = uo.Underlying(
            symbol=sym, bars=bars, expiries=exps,
            earnings=earnings.get(sym),
            held=True if held_set is None else (sym in held_set),
        )
        if u.earnings and not uo.usable_expiries(u, board_date):
            rep.earnings_blocked.append(sym)
        out.append(u)
        rep.ok.append(sym)
    return out, rep


# ---------------------------------------------------------------------------
# スナップショット（再現とテスト用）
# ---------------------------------------------------------------------------

def dump_underlying(u: uo.Underlying) -> dict:
    return {
        "symbol": u.symbol, "earnings": u.earnings, "held": u.held,
        "bars": [[b.date, b.high, b.low, b.close] for b in u.bars],
        "expiries": [
            {"expiry": e.expiry,
             "rows": [[r.strike,
                       [r.call.bid, r.call.ask, r.call.last, r.call.volume, r.call.oi],
                       [r.put.bid, r.put.ask, r.put.last, r.put.volume, r.put.oi]]
                      for r in e.rows]}
            for e in u.expiries
        ],
    }


def load_underlying(d: dict) -> uo.Underlying:
    def q(v):
        return uo.Quote(bid=v[0], ask=v[1], last=v[2], volume=int(v[3] or 0),
                        oi=int(v[4] or 0))
    return uo.Underlying(
        symbol=d["symbol"], earnings=d.get("earnings"), held=d.get("held", True),
        bars=[ui.Bar(b[0], b[1], b[2], b[3]) for b in d["bars"]],
        expiries=[uo.Expiry(expiry=e["expiry"],
                            rows=[uo.StrikeQuote(strike=r[0], call=q(r[1]), put=q(r[2]))
                                  for r in e["rows"]])
                  for e in d["expiries"]],
    )
