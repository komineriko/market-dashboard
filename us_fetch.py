"""米国株オプションのデータ取得。すべて yfinance から取る。

  日足          まとめて1リクエストで取る（銘柄ごとに叩くと数が多すぎる）
  決算予定      銘柄ごと。取れなかった銘柄はスクリーニングから外す
  オプション板  行使価格別の気配・建玉が無料で取れるのはここだけ

FMPは使わない。無料枠の上限が既存のダッシュボードで先に消費されており、
実データで試すと日足は全銘柄 429、決算カレンダーは0件で返った。

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

REQUEST_SLEEP = float(os.environ.get("US_REQUEST_SLEEP", "0.15"))
CHAIN_SLEEP = float(os.environ.get("US_CHAIN_SLEEP", "0.25"))


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# 立会日
# ---------------------------------------------------------------------------

def _eastern_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001 - tzdataが無い環境では夏時間を無視する
        return datetime.now(timezone.utc) - timedelta(hours=5)


def board_date_from_clock() -> date:
    """直近の「終わった立会日」。

    日足の配信は引けから数時間遅れることがあるので、日足の最終日をそのまま
    基準日にすると残存日数が1日ずれる。板は引け値で出ているので、
    時計から立会日を決めて板に合わせる。
    """
    import us_report as ur         # 米国の休場日はレポート側に持っている
    now = _eastern_now()
    d = now.date()
    if now.hour < 16 or d.weekday() >= 5 or d in ur.US_HOLIDAYS:
        d -= timedelta(days=1)
        while d.weekday() >= 5 or d in ur.US_HOLIDAYS:
            d -= timedelta(days=1)
    return d


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
# 日足
# ---------------------------------------------------------------------------

def _chunks(xs: Sequence[str], n: int) -> List[List[str]]:
    return [list(xs[i:i + n]) for i in range(0, len(xs), n)]


def _rows_for(df, symbol: str, multi: bool) -> List[ui.Bar]:
    """yfinance の DataFrame から 1銘柄分の日足を取り出す。

    複数銘柄をまとめて取ると列が (銘柄, 項目) の2段になり、1銘柄だと1段になる。
    バージョンによってどちらで返るかが変わるので、両方を受け付ける。
    """
    try:
        sub = df[symbol] if multi else df
    except (KeyError, IndexError):
        return []
    out: List[ui.Bar] = []
    for idx, row in sub.iterrows():
        h, l, c = _num(row.get("High")), _num(row.get("Low")), _num(row.get("Close"))
        if h is None or l is None or c is None:
            continue
        out.append(ui.Bar(str(idx)[:10], h, l, c))
    out.sort(key=lambda b: b.date)
    return out


def fetch_bars_many(symbols: Sequence[str], period: str = "2y"
                    ) -> Dict[str, List[ui.Bar]]:
    """日足をまとめて取る。

    銘柄ごとに叩くと 60本超のリクエストになり、弾かれやすい。
    配当調整はかけない（素の終値。参考にした元レポートと揃える）。
    """
    import yfinance as yf
    out: Dict[str, List[ui.Bar]] = {}
    for chunk in _chunks(list(symbols), 20):
        try:
            df = yf.download(chunk, period=period, interval="1d",
                             group_by="ticker", auto_adjust=False,
                             threads=True, progress=False)
        except Exception as e:  # noqa: BLE001
            log(f"WARN: 日足のまとめ取得に失敗: {e}")
            continue
        if df is None or len(df) == 0:
            log(f"WARN: 日足が空で返りました: {chunk}")
            continue
        multi = hasattr(df.columns, "nlevels") and df.columns.nlevels > 1
        for sym in chunk:
            bars = _rows_for(df, sym, multi)
            if bars:
                out[sym] = bars
        time.sleep(REQUEST_SLEEP)
    return out


def fetch_bars(symbol: str, period: str = "2y") -> List[ui.Bar]:
    return fetch_bars_many([symbol], period).get(symbol, [])


def _earnings_from_yf(symbol: str, today: date) -> Optional[str]:
    """yfinance から次回決算日。見つからなければ None。"""
    import yfinance as yf
    tk = yf.Ticker(symbol)
    # まず確定済みの予定表
    try:
        cal = tk.calendar
    except Exception:  # noqa: BLE001
        cal = None
    cands: List[date] = []
    if isinstance(cal, dict):
        v = cal.get("Earnings Date")
        for x in (v if isinstance(v, (list, tuple)) else [v]):
            d = _as_date(x)
            if d:
                cands.append(d)
    # 次に過去＋将来の一覧
    try:
        df = tk.get_earnings_dates(limit=12)
    except Exception:  # noqa: BLE001
        df = None
    if df is not None and len(df):
        for idx in df.index:
            d = _as_date(idx)
            if d:
                cands.append(d)
    future = sorted(d for d in cands if d >= today)
    return future[0].isoformat() if future else None


def _as_date(x) -> Optional[date]:
    """pandas の Timestamp・datetime・date・文字列のどれでも date にする。

    Timestamp は datetime の、datetime は date のサブクラスなので
    isinstance(x, date) だけで通すと Timestamp がそのまま返り、
    あとで date と比較したときに pandas が例外を投げる（実際に踏んだ）。
    """
    if x is None:
        return None
    try:
        if x != x:                           # NaT / NaN は自分と等しくない
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(x, "to_pydatetime"):          # pandas Timestamp
        try:
            x = x.to_pydatetime()
        except (ValueError, TypeError):
            return None
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    try:
        return date.fromisoformat(str(x)[:10])
    except (ValueError, TypeError):
        return None


def fetch_earnings_map(symbols: Sequence[str]) -> Tuple[Dict[str, str], List[str]]:
    """{銘柄: 次回決算日} と、決算日が分からなかった銘柄の一覧。

    分からない銘柄は呼び出し側でスクリーニングから外す。「決算をまたぐ限月は
    除外」は満たせるかどうかが分からない時点で守れていないので、
    不明なまま候補に出すほうが危ない。
    """
    today = datetime.now(timezone.utc).date()
    out: Dict[str, str] = {}
    unknown: List[str] = []
    for sym in symbols:
        if sym in out:
            continue
        try:
            d = _earnings_from_yf(sym, today)
        except Exception as e:  # noqa: BLE001
            log(f"WARN: {sym} の決算日を取得できませんでした: {e}")
            d = None
        time.sleep(REQUEST_SLEEP)
        if d:
            out[sym] = d
        else:
            unknown.append(sym)
    return out, unknown


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

ETF_SYMBOLS = frozenset((
    "SPY", "QQQ", "IWM", "DIA", "SMH", "XLK", "XLF", "XLE", "XLV",
    "TQQQ", "GLD", "SLV", "USO", "TLT", "EEM", "EWJ", "HYG", "ARKK",
))


@dataclass
class FetchReport:
    asof: Optional[date] = None
    ok: List[str] = None
    no_bars: List[str] = None
    no_chain: List[str] = None
    earnings_blocked: List[str] = None
    earnings_unknown: List[str] = None
    earnings_available: bool = True
    bars_lag_days: int = 0          # 板より日足が何営業日ぶん古いか

    def __post_init__(self):
        for f in ("ok", "no_bars", "no_chain", "earnings_blocked",
                  "earnings_unknown"):
            if getattr(self, f) is None:
                setattr(self, f, [])


def load_universe(symbols: Sequence[str] = UNIVERSE,
                  asof: Optional[date] = None,
                  holdings: Optional[List[str]] = None,
                  ) -> Tuple[List[uo.Underlying], FetchReport]:
    rep = FetchReport()
    held_set = set(holdings) if holdings is not None else None

    bars_map = fetch_bars_many(symbols)
    usable = [s for s in symbols if len(bars_map.get(s, [])) >= 60]
    rep.no_bars = [s for s in symbols if s not in usable]

    # ETFには決算が無い。常に「不明」になるので、決算で外す対象から除く。
    earnings, unknown = fetch_earnings_map([s for s in usable if s not in ETF_SYMBOLS])
    rep.earnings_available = bool(earnings)
    rep.earnings_unknown = unknown

    board_date = asof or board_date_from_clock()
    rep.asof = board_date
    # 日足が板より古いかを記録する。古いままテクニカルを出すこと自体は
    # 構わないが、黙ってやると「いつの数字か」が分からなくなる。
    newest_bar = max((b[-1].date for b in bars_map.values() if b), default=None)
    if newest_bar and newest_bar < board_date.isoformat():
        rep.bars_lag_days = (board_date - date.fromisoformat(newest_bar)).days

    out: List[uo.Underlying] = []
    for sym in usable:
        bars = bars_map[sym]
        if bars[-1].close < uo.MIN_UNDERLYING:
            continue
        if sym in unknown:
            # 決算日が分からない銘柄は、またぐかどうかを判定できないので外す。
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
