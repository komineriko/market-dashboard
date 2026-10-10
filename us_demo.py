"""合成データ。通信せずにレンダリングと判定を通しで確認するためのもの。

銘柄ごとに「どの区分に出てほしいか」を決め打ちで作る。乱数は固定種。
"""

from __future__ import annotations

import math
import random
from datetime import date, timedelta
from typing import List, Optional

import sq_analytics as sa
import us_indicators as ui
import us_options as uo


def _bars(seed: int, n: int, start: float, drift: float, vol: float,
          finish: int = 0) -> List[ui.Bar]:
    """日足。finish 本だけ最後を素直な片方向にして、モメンタムを立たせる。

    finish が正なら上げ、負なら下げ。
    """
    rng = random.Random(seed)
    closes = [start]
    tail = abs(finish)
    for i in range(n - 1):
        if tail and i >= n - 1 - tail:
            step = abs(rng.gauss(drift, vol)) + vol * 0.5
            r = step if finish > 0 else -step
        else:
            r = rng.gauss(drift, vol)
        closes.append(closes[-1] * math.exp(r))
    out: List[ui.Bar] = []
    d = date(2026, 1, 2)
    for c in closes:
        wiggle = abs(rng.gauss(0, vol)) * c
        out.append(ui.Bar(d.isoformat(), c + wiggle, c - wiggle, c))
        d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
    return out


def _bars_at(seed: int, n: int, start: float, vol: float, target_dev_atr: float,
             finish: int = 0) -> List[ui.Bar]:
    """50日線からの乖離が target_dev_atr（ATR換算）になるようドリフトを合わせる。

    ドリフトを直接決め打ちすると、乱数の引き次第で銘柄が全部「伸び切り」に
    寄ってしまい、区分ごとの挙動を確認できない。
    """
    lo, hi = -0.006, 0.006
    bars = _bars(seed, n, start, 0.0, vol, finish)
    for _ in range(40):
        mid = (lo + hi) / 2
        bars = _bars(seed, n, start, mid, vol, finish)
        closes = [b.close for b in bars]
        s50, a = ui.sma(closes, 50), ui.atr(bars)
        dev = (closes[-1] - s50) / a
        if dev < target_dev_atr:
            lo = mid
        else:
            hi = mid
        if abs(dev - target_dev_atr) < 0.02:
            break
    return bars


def _chain(spot: float, asof: date, dte: int, iv: float, step: float,
           width: int = 14, oi: int = 3000, spread: float = 0.02) -> uo.Expiry:
    """Black-76 で値段をつけた板。スマイルは下に厚くする。"""
    t = max(dte, 0.5) / 365.0
    base = round(spot / step) * step
    rows: List[uo.StrikeQuote] = []
    for i in range(-width, width + 1):
        k = base + i * step
        if k <= 0:
            continue
        skew = 1.0 + max(0.0, (spot - k) / spot) * 1.2      # 下ほどIVが高い
        v = iv * skew
        row = uo.StrikeQuote(strike=k)
        for is_call in (True, False):
            p = sa.bs_price(spot, k, t, v, is_call)
            if p < 0.05:
                continue
            half = max(p * spread, 0.01)
            q = uo.Quote(bid=round(p - half, 2), ask=round(p + half, 2),
                         last=round(p, 2),
                         volume=int(oi / 4),
                         oi=int(oi * math.exp(-((k - spot) / (spot * 0.08)) ** 2))
                         + max(1, oi // 20))
            if is_call:
                row.call = q
            else:
                row.put = q
        rows.append(row)
    return uo.Expiry(expiry=(asof + timedelta(days=dte)).isoformat(), rows=rows)


def build(asof: Optional[date] = None) -> List[uo.Underlying]:
    asof = asof or date(2026, 10, 9)
    out: List[uo.Underlying] = []

    def add(sym, seed, start, dev_atr, vol, finish, ivs, step, earnings=None,
            spread=0.02, oi=3000, held=True, back=None):
        bars = _bars_at(seed, 160, start, vol, dev_atr, finish)
        # 日足の最終日を基準日に合わせる
        bars = [ui.Bar(b.date, b.high, b.low, b.close) for b in bars]
        bars[-1] = ui.Bar(asof.isoformat(), bars[-1].high, bars[-1].low, bars[-1].close)
        spot = bars[-1].close
        exps = [_chain(spot, asof, dte, iv, step, oi=oi, spread=spread)
                for dte, iv in ivs]
        backs = [_chain(spot, asof, dte, iv, step, oi=oi, spread=spread)
                 for dte, iv in (back or [])]
        out.append(uo.Underlying(symbol=sym, bars=bars, expiries=exps,
                                 back_expiries=backs,
                                 earnings=earnings, held=held))

    # IVが割安で上抜け直後、まだ伸び切っていない → コール買い
    add("ALFA", 11, 120.0, 1.4, 0.016, 4, [(3, 0.17), (10, 0.18), (15, 0.19)], 2.5)
    # モメンタムが立っていてIVが割高 → ブルプット / P売り
    add("BETA", 23, 88.0, 1.1, 0.019, 4, [(4, 0.46), (11, 0.45), (15, 0.44)], 2.5)
    # 伸び切ってIVが割高 → カバードコール
    add("GAMM", 37, 210.0, 2.6, 0.014, 0, [(3, 0.34), (10, 0.34), (15, 0.33)], 5.0)
    # 満期までに決算 → 全区分から除外
    add("EPSI", 41, 64.0, 1.2, 0.018, 4, [(9, 0.45)], 2.5,
        earnings=(asof + timedelta(days=5)).isoformat())
    # スプレッドが広すぎる → 流動性で落ちる
    add("ZETA", 53, 45.0, 1.2, 0.018, 4, [(10, 0.46)], 2.5, spread=0.30)
    # 建玉が薄い → 流動性で落ちる
    add("OMEG", 67, 30.0, 1.2, 0.018, 4, [(10, 0.46)], 1.0, oi=100)
    # 下げに転じてIVが割安 → プット買い
    add("DELT", 71, 150.0, -1.2, 0.017, -4, [(3, 0.20), (10, 0.21), (15, 0.22)], 2.5)
    # 勢いが落ちてIVが割高 → ベアコール
    add("THET", 83, 95.0, -1.0, 0.019, -4, [(4, 0.47), (11, 0.46), (15, 0.45)], 2.5)
    # レンジで手前のIVが高い（順ザヤ）→ カレンダー
    add("KAPP", 97, 180.0, 0.2, 0.013, 0, [(7, 0.42), (14, 0.38)], 5.0,
        back=[(35, 0.32), (49, 0.30)])
    return out
