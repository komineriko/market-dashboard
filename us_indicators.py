"""米国株のテクニカル指標。

オプションのスクリーニングに使う分だけ。外部ライブラリに頼らず素のPythonで
計算する。計算の定義は銘柄をまたいで同じでなければ比較にならないので、
EMA の初期値の置き方まで一通りに決めてある。

検算: NVDA 2026-10-08 で SMA50 221.25 / RSI 54.5 / MACD DIF 4.49 DEA 3.84。
tests/test_us_indicators.py で固定している。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence

TRADING_DAYS = 252
MAD_TO_SIGMA = 1.4826          # 正規分布で MAD を標準偏差に合わせる係数
MIN_PLAUSIBLE_VOL = 5.0        # 年率%。これを下回る実現ボラは配信側の異常


@dataclass(frozen=True)
class Bar:
    date: str
    high: float
    low: float
    close: float
    volume: float = 0.0        # 出来高。銘柄の優先順位づけだけに使う


# ---------------------------------------------------------------------------
# 移動平均
# ---------------------------------------------------------------------------

def sma(values: Sequence[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def ema_series(values: Sequence[float], n: int) -> List[Optional[float]]:
    """EMA の系列。最初の n 本の単純平均を初期値に置く（主要チャートと同じ）。

    初期値を values[0] にすると序盤が歪み、その歪みは MACD のヒストグラムに
    何十本も残る。銘柄ごとに取得できる期間が違うので、ここがぶれると
    銘柄間の比較が成立しない。
    """
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < n:
        return out
    k = 2.0 / (n + 1.0)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def _wilder(values: Sequence[float], n: int) -> List[Optional[float]]:
    """Wilder の平滑化。ATR と RSI が使う。"""
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < n:
        return out
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = (prev * (n - 1) + values[i]) / n
        out[i] = prev
    return out


# ---------------------------------------------------------------------------
# ATR / RSI / MACD
# ---------------------------------------------------------------------------

def true_ranges(bars: Sequence[Bar]) -> List[float]:
    tr: List[float] = []
    for i, b in enumerate(bars):
        if i == 0:
            tr.append(b.high - b.low)
            continue
        pc = bars[i - 1].close
        tr.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    return tr


def atr(bars: Sequence[Bar], n: int = 14, wilder: bool = False) -> Optional[float]:
    """ATR。既定は直近 n 本の真の値幅の単純平均。

    Wilder の平滑化のほうが教科書的だが、こちらは過去の値幅をいつまでも引きずる。
    乖離をATR換算するときは「ここ2〜3週間のふだんの値幅」で割りたいので単純平均を使う。
    参考にした元レポートの値（NVDA 2026-10-08 で乖離 1.7ATR）とも一致する。
    """
    tr = true_ranges(bars)[1:]   # 1本目は前日終値が無く値幅だけなので外す
    if len(tr) < n:
        return None
    if wilder:
        s = _wilder(tr, n)
        return s[-1] if s else None
    return sum(tr[-n:]) / n


def rsi(closes: Sequence[float], n: int = 14) -> Optional[float]:
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = _wilder(gains, n)[-1]
    al = _wilder(losses, n)[-1]
    if ag is None or al is None:
        return None
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)


@dataclass
class Macd:
    dif: float                      # EMA12 − EMA26
    dea: float                      # DIF の9日EMA
    hist: float                     # DIF − DEA
    hist_prev: Optional[float]
    hist_prev2: Optional[float]
    gc_bars_ago: Optional[int]      # 何本前に DIF が DEA を下から上抜けたか
    gc_above_zero: Optional[bool]   # その上抜けがゼロラインの上で起きたか
    gap_atr: Optional[float]        # (DIF − DEA) ÷ ATR。接近の度合い
    dc_bars_ago: Optional[int]      # 何本前に上から下抜けたか
    dc_below_zero: Optional[bool]   # その下抜けがゼロラインの下で起きたか

    @property
    def dead_cross_recent(self) -> bool:
        return self.dc_bars_ago is not None


def macd(closes: Sequence[float], atr_value: Optional[float] = None,
         fast: int = 12, slow: int = 26, signal: int = 9,
         lookback: int = 20) -> Optional[Macd]:
    ef = ema_series(closes, fast)
    es = ema_series(closes, slow)
    dif_full = [None if (a is None or b is None) else a - b for a, b in zip(ef, es)]
    dif = [d for d in dif_full if d is not None]
    if len(dif) < signal + 2:
        return None
    dea_s = ema_series(dif, signal)
    pairs = [(d, s) for d, s in zip(dif, dea_s) if s is not None]
    if len(pairs) < 2:
        return None
    hist = [d - s for d, s in pairs]

    gc_bars_ago = gc_above_zero = None
    dc_bars_ago = dc_below_zero = None
    span = min(lookback, len(pairs) - 1)
    # 探すのは、いまの位置と同じ向きの交差だけ。上抜けたあとに下抜けていれば
    # その上抜けはもう効いていないし、逆も同じ。
    above = hist[-1] > 0
    for back in range(span):
        i = len(pairs) - 1 - back
        if above and gc_bars_ago is None and hist[i] > 0 and hist[i - 1] <= 0:
            gc_bars_ago = back
            gc_above_zero = pairs[i][0] > 0
        if not above and dc_bars_ago is None and hist[i] < 0 and hist[i - 1] >= 0:
            dc_bars_ago = back
            dc_below_zero = pairs[i][0] < 0

    d, s = pairs[-1]
    gap_atr = None
    if atr_value:
        gap_atr = (d - s) / atr_value
    return Macd(
        dif=d, dea=s, hist=hist[-1],
        hist_prev=hist[-2] if len(hist) >= 2 else None,
        hist_prev2=hist[-3] if len(hist) >= 3 else None,
        gc_bars_ago=gc_bars_ago, gc_above_zero=gc_above_zero, gap_atr=gap_atr,
        dc_bars_ago=dc_bars_ago, dc_below_zero=dc_below_zero,
    )


# ---------------------------------------------------------------------------
# 実現ボラティリティ
# ---------------------------------------------------------------------------

def log_returns(closes: Sequence[float]) -> List[float]:
    out = []
    for i in range(1, len(closes)):
        a, b = closes[i - 1], closes[i]
        if a > 0 and b > 0:
            out.append(math.log(b / a))
    return out


def _median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def mad_vol(closes: Sequence[float], n: int = 60) -> Optional[float]:
    """中央絶対偏差から出す実現ボラ（年率%）。

    素の標準偏差は決算ギャップ1本に引きずられる。中央値まわりの絶対偏差なら
    外れ値1本では動かないので、「ふだんどれくらい動く銘柄か」の推定に向く。
    """
    r = log_returns(closes)[-n:]
    if len(r) < max(10, n // 2):
        return None
    med = _median(r)
    mad = _median([abs(x - med) for x in r])
    v = mad * MAD_TO_SIGMA * math.sqrt(TRADING_DAYS) * 100.0
    # 年率5%を下回る実現ボラの銘柄は無い。値が止まっている配信を拾っている。
    # そのまま返すとIV÷実現ボラが何倍にも膨らみ、割高判定が壊れる
    # （実データで、年率3%と出た銘柄の比が25.7になった）。
    return v if v >= MIN_PLAUSIBLE_VOL else None


def hv_excl_gap(closes: Sequence[float], n: int = 20, drop: int = 1) -> Optional[float]:
    """ギャップを除いたヒストリカルボラ（年率%）。

    直近 n 日のうち絶対値が大きい順に drop 本を落としてから標準偏差を取る。
    決算の窓開け1本で20日ボラが倍になるのを避ける。
    """
    r = log_returns(closes)[-n:]
    if len(r) < max(10, n // 2):
        return None
    kept = sorted(r, key=abs)[:max(2, len(r) - drop)]
    m = sum(kept) / len(kept)
    var = sum((x - m) ** 2 for x in kept) / (len(kept) - 1)
    return math.sqrt(var * TRADING_DAYS) * 100.0


# ---------------------------------------------------------------------------
# モメンタム改善スコア
# ---------------------------------------------------------------------------

@dataclass
class Momentum:
    score: int
    detail: List[bool]

    LABELS = (
        "MACDヒストが前日比プラス",
        "MACDヒストが2日連続で上昇",
        "RSIが3営業日前より上",
        "直近10日の安値を割っていない",
        "終値が直近5日レンジの上半分",
    )


def momentum_score(bars: Sequence[Bar], m: Optional[Macd]) -> Momentum:
    closes = [b.close for b in bars]
    checks: List[bool] = []

    checks.append(bool(m and m.hist_prev is not None and m.hist > m.hist_prev))
    checks.append(bool(m and m.hist_prev is not None and m.hist_prev2 is not None
                       and m.hist > m.hist_prev > m.hist_prev2))

    r_now = rsi(closes)
    r_then = rsi(closes[:-3]) if len(closes) > 3 else None
    checks.append(bool(r_now is not None and r_then is not None and r_now > r_then))

    lows = [b.low for b in bars[-10:]]
    checks.append(bool(len(lows) >= 10 and bars[-1].low > min(lows[:-1])))

    win = bars[-5:]
    if len(win) >= 5:
        hi = max(b.high for b in win)
        lo = min(b.low for b in win)
        checks.append(hi > lo and closes[-1] >= lo + (hi - lo) / 2.0)
    else:
        checks.append(False)

    return Momentum(score=sum(1 for c in checks if c), detail=checks)
