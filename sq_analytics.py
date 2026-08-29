#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日経225オプション SQ予測レポート 分析エンジン

外部通信を一切行わない純粋な計算層。行使価格ごとの建玉・清算値段を受け取り、
レポートに必要な指標をすべて算出する。

採用している慣例（レポートの「前提と限界」に明示する内容）:
  * 金利・配当は 0 とし、フォワード F を基準にした Black-76 で評価する。
    日経225オプションはヨーロピアンなので早期行使プレミアムは無い。
  * 残存 T は暦日 / 365（元レポートが 1σ = F × IV × √(24/365) で算出しているのと同じ）。
  * IV はデータ提供元の列を使わず、清算値段から自前で逆算する。
    列の有無に依存せず、フォワード・デルタ・ガンマとの整合が取れるため。
  * Net GEX は「ディーラー = +CALL建玉 − PUT建玉」の慣例仮定。
    符号はこの仮定に依存し、仮定が崩れる局面では反転しうる。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

# 日経225オプション 1枚 = 指数 1pt あたり 1,000円
CONTRACT_MULTIPLIER = 1000
OKU = 1e8  # 億

# 実効デルタ・ガンマの計算対象から外す極小プレミアムの閾値（円）
JUNK_PREMIUM = 20.0


# ---------------------------------------------------------------------------
# Black-76（フォワード基準・ディスカウント無し）
# ---------------------------------------------------------------------------

def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(f: float, k: float, t: float, sigma: float) -> Tuple[float, float]:
    v = sigma * math.sqrt(t)
    d1 = (math.log(f / k) + 0.5 * sigma * sigma * t) / v
    return d1, d1 - v


def bs_price(f: float, k: float, t: float, sigma: float, is_call: bool) -> float:
    """フォワード基準のオプション理論価格。"""
    if t <= 0 or sigma <= 0:
        intrinsic = (f - k) if is_call else (k - f)
        return max(intrinsic, 0.0)
    d1, d2 = _d1_d2(f, k, t, sigma)
    if is_call:
        return f * norm_cdf(d1) - k * norm_cdf(d2)
    return k * norm_cdf(-d2) - f * norm_cdf(-d1)


def bs_delta(f: float, k: float, t: float, sigma: float, is_call: bool) -> float:
    """フォワードに対するデルタ。PUT は負値。"""
    if t <= 0 or sigma <= 0:
        if is_call:
            return 1.0 if f > k else 0.0
        return -1.0 if f < k else 0.0
    d1, _ = _d1_d2(f, k, t, sigma)
    return norm_cdf(d1) if is_call else norm_cdf(d1) - 1.0


def bs_gamma(f: float, k: float, t: float, sigma: float) -> float:
    """CALL/PUT 共通のガンマ。"""
    if t <= 0 or sigma <= 0:
        return 0.0
    d1, _ = _d1_d2(f, k, t, sigma)
    return norm_pdf(d1) / (f * sigma * math.sqrt(t))


def bs_vega(f: float, k: float, t: float, sigma: float) -> float:
    """IV 1.0（=100pt）あたりのベガ。1pt あたりは 1/100。"""
    if t <= 0 or sigma <= 0:
        return 0.0
    d1, _ = _d1_d2(f, k, t, sigma)
    return f * norm_pdf(d1) * math.sqrt(t)


def implied_vol(price: float, f: float, k: float, t: float, is_call: bool,
                lo: float = 0.005, hi: float = 5.0) -> Optional[float]:
    """清算値段から IV を逆算する。無裁定境界の外・解なしの場合は None。"""
    if price is None or price <= 0 or t <= 0 or f <= 0 or k <= 0:
        return None
    intrinsic = max((f - k) if is_call else (k - f), 0.0)
    if price <= intrinsic + 1e-9:
        return None
    upper = f if is_call else k  # 価格の理論上限
    if price >= upper:
        return None
    p_lo = bs_price(f, k, t, lo, is_call)
    p_hi = bs_price(f, k, t, hi, is_call)
    if not (p_lo <= price <= p_hi):
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if bs_price(f, k, t, mid, is_call) < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-7:
            break
    return 0.5 * (lo + hi)


# ---------------------------------------------------------------------------
# カレンダー
# ---------------------------------------------------------------------------

# 2026〜2027年の日本の祝日（SQまでの営業日数算出用。SQ前後の期間のみ効く）
JP_HOLIDAYS = {
    date(2026, 1, 1), date(2026, 1, 12), date(2026, 2, 11), date(2026, 2, 23),
    date(2026, 3, 20), date(2026, 4, 29), date(2026, 5, 3), date(2026, 5, 4),
    date(2026, 5, 5), date(2026, 5, 6), date(2026, 7, 20), date(2026, 8, 11),
    date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23), date(2026, 10, 12),
    date(2026, 11, 3), date(2026, 11, 23),
    date(2027, 1, 1), date(2027, 1, 11), date(2027, 2, 11), date(2027, 2, 23),
    date(2027, 3, 22), date(2027, 4, 29), date(2027, 5, 3), date(2027, 5, 4),
    date(2027, 5, 5), date(2027, 7, 19), date(2027, 8, 11), date(2027, 9, 20),
    date(2027, 9, 23), date(2027, 10, 11), date(2027, 11, 3), date(2027, 11, 23),
}


def sq_date_for(year: int, month: int) -> date:
    """SQ日 = 第2金曜日。祝日の場合は前営業日に繰り上がるが、近年は該当なし。"""
    d = date(year, month, 1)
    fridays = 0
    while True:
        if d.weekday() == 4:
            fridays += 1
            if fridays == 2:
                break
        d += timedelta(days=1)
    while d in JP_HOLIDAYS:
        d -= timedelta(days=1)
    return d


def next_business_day(d: date) -> date:
    """d の翌営業日（土日・祝日を飛ばす）。"""
    n = d + timedelta(days=1)
    while n.weekday() >= 5 or n in JP_HOLIDAYS:
        n += timedelta(days=1)
    return n


def business_days_between(start: date, end: date) -> int:
    """start の翌日から end までの営業日数（SQ日を含む）。"""
    n = 0
    d = start + timedelta(days=1)
    while d <= end:
        if d.weekday() < 5 and d not in JP_HOLIDAYS:
            n += 1
        d += timedelta(days=1)
    return n


def parse_contract_month(label: str) -> Tuple[int, int]:
    """"26-09" / "202609" → (2026, 9)。"""
    digits = [c for c in label if c.isdigit()]
    s = "".join(digits)
    if len(s) == 4:      # 26-09
        return 2000 + int(s[:2]), int(s[2:])
    if len(s) == 6:      # 202609
        return int(s[:4]), int(s[4:])
    raise ValueError(f"限月の形式を解釈できません: {label!r}")


# ---------------------------------------------------------------------------
# 板データ
# ---------------------------------------------------------------------------

@dataclass
class StrikeRow:
    strike: float
    call_price: Optional[float] = None
    put_price: Optional[float] = None
    call_oi: int = 0
    put_oi: int = 0
    call_volume: int = 0
    put_volume: int = 0


@dataclass
class Chain:
    """1限月ぶんのオプション板スナップショット。"""
    contract_month: str
    rows: Dict[float, StrikeRow] = field(default_factory=dict)

    def add(self, row: StrikeRow) -> None:
        self.rows[row.strike] = row

    @property
    def strikes(self) -> List[float]:
        return sorted(self.rows)

    def oi(self, side: str) -> Dict[float, int]:
        key = "call_oi" if side == "call" else "put_oi"
        return {k: getattr(r, key) for k, r in self.rows.items()}

    def price(self, side: str) -> Dict[float, Optional[float]]:
        key = "call_price" if side == "call" else "put_price"
        return {k: getattr(r, key) for k, r in self.rows.items()}

    def window(self) -> Tuple[float, float]:
        s = self.strikes
        return (s[0], s[-1]) if s else (0.0, 0.0)

    def total_oi(self, side: str) -> int:
        return sum(self.oi(side).values())


# ---------------------------------------------------------------------------
# フォワードと IV カーブ
# ---------------------------------------------------------------------------

@dataclass
class ForwardResult:
    forward: float
    samples: Dict[float, float]          # 行使価格 → その行使価格でのパリティ値
    spread: float                        # 採用したサンプルの最大-最小
    reference: List[Tuple[float, float]] # レポート掲載用の2点


def implied_forward(chain: Chain, spot_hint: float) -> Optional[ForwardResult]:
    """プット・コール・パリティ F = K + C - P から、ATM近傍の中央値でフォワードを決める。"""
    samples: Dict[float, float] = {}
    for k, r in chain.rows.items():
        if r.call_price and r.put_price and r.call_price > 0 and r.put_price > 0:
            samples[k] = k + r.call_price - r.put_price
    if not samples:
        return None
    # ATM近傍（ヒントの±8%）に限定する。深いOTMは片側の気配が薄くパリティが崩れる。
    near = {k: v for k, v in samples.items() if abs(k - spot_hint) <= 0.08 * spot_hint}
    used = near or samples
    vals = sorted(used.values())
    mid = vals[len(vals) // 2] if len(vals) % 2 else 0.5 * (vals[len(vals) // 2 - 1] + vals[len(vals) // 2])
    # 参考掲載用: ATM直上と、そこから5,000円ほど下の行使価格
    ks = sorted(used)
    ref: List[Tuple[float, float]] = []
    for target in (spot_hint + 500, spot_hint - 4500):
        if ks:
            k = min(ks, key=lambda x: abs(x - target))
            if all(abs(k - kk) > 1e-9 for kk, _ in ref):
                ref.append((k, used[k]))
    return ForwardResult(forward=mid, samples=used, spread=max(vals) - min(vals), reference=ref)


@dataclass
class IVCurve:
    forward: float
    t: float
    call_iv: Dict[float, float]
    put_iv: Dict[float, float]
    otm_iv: Dict[float, float]      # OTM側を採用した「板のIV」
    broken: int                     # |ivC - ivP| > 3pt の本数
    evaluated: int                  # 両側でIVが立った本数
    no_price_call_oi: int           # 値なし建玉（CALL）
    no_price_put_oi: int            # 値なし建玉（PUT）

    def at(self, k: float) -> Optional[float]:
        """OTMカーブを線形補間して任意の行使価格のIVを返す。"""
        return _interp(self.otm_iv, k)

    def atm_iv(self) -> Optional[float]:
        return self.at(self.forward)


def _interp(curve: Dict[float, float], x: float) -> Optional[float]:
    if not curve:
        return None
    ks = sorted(curve)
    if x <= ks[0]:
        return curve[ks[0]]
    if x >= ks[-1]:
        return curve[ks[-1]]
    for a, b in zip(ks, ks[1:]):
        if a <= x <= b:
            if b == a:
                return curve[a]
            w = (x - a) / (b - a)
            return curve[a] * (1 - w) + curve[b] * w
    return None


def build_iv_curve(chain: Chain, forward: float, t: float) -> IVCurve:
    call_iv: Dict[float, float] = {}
    put_iv: Dict[float, float] = {}
    otm: Dict[float, float] = {}
    broken = 0
    evaluated = 0
    no_price_call_oi = 0
    no_price_put_oi = 0

    for k, r in sorted(chain.rows.items()):
        if r.call_oi and not (r.call_price and r.call_price > 0):
            no_price_call_oi += r.call_oi
        if r.put_oi and not (r.put_price and r.put_price > 0):
            no_price_put_oi += r.put_oi

        ic = implied_vol(r.call_price, forward, k, t, True) if r.call_price else None
        ip = implied_vol(r.put_price, forward, k, t, False) if r.put_price else None
        if ic is not None:
            call_iv[k] = ic
        if ip is not None:
            put_iv[k] = ip
        if ic is not None and ip is not None:
            evaluated += 1
            if abs(ic - ip) * 100 > 3.0:
                broken += 1
        # OTM側を採用（K<F は PUT、K>F は CALL）。片側しか無ければある方を使う。
        pick = ip if k < forward else ic
        if pick is None:
            pick = ic if ip is None else ip
        if pick is not None:
            otm[k] = pick

    return IVCurve(forward=forward, t=t, call_iv=call_iv, put_iv=put_iv, otm_iv=otm,
                   broken=broken, evaluated=evaluated,
                   no_price_call_oi=no_price_call_oi, no_price_put_oi=no_price_put_oi)


# ---------------------------------------------------------------------------
# スキュー（モネネス正規化 / デルタ正規化）
# ---------------------------------------------------------------------------

MONEYNESS_GRID = (1.05, 1.02, 1.00, 0.98, 0.95, 0.92, 0.90, 0.85, 0.80, 0.75)


def moneyness_smile(curve: IVCurve, grid: Sequence[float] = MONEYNESS_GRID) -> Dict[float, Optional[float]]:
    """固定モネネス m = K/F でのIV。現値が動いても比較できる正規化された量。"""
    return {m: curve.at(curve.forward * m) for m in grid}


def strike_for_delta(curve: IVCurve, target_delta: float, is_call: bool) -> Optional[Tuple[float, float]]:
    """スマイルを織り込んだうえで、指定デルタになる行使価格とそのIVを求める。"""
    f, t = curve.forward, curve.t
    if not curve.otm_iv or t <= 0:
        return None
    lo, hi = 0.10 * f, 3.0 * f

    def delta_at(k: float) -> Optional[float]:
        sigma = curve.at(k)
        if sigma is None or sigma <= 0:
            return None
        return bs_delta(f, k, t, sigma, is_call)

    # CALL: K が上がるとデルタは減る / PUT: K が上がるとデルタ（負）は減る
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        d = delta_at(mid)
        if d is None:
            return None
        if is_call:
            if d > target_delta:
                lo = mid
            else:
                hi = mid
        else:
            if d > target_delta:
                lo = mid
            else:
                hi = mid
        if hi - lo < 0.5:
            break
    k = 0.5 * (lo + hi)
    sigma = curve.at(k)
    return (k, sigma) if sigma is not None else None


DELTA_GRID = (
    ("put", -0.25), ("put", -0.15), ("put", -0.10), ("put", -0.05),
    ("call", 0.25), ("call", 0.10),
)


def delta_smile(curve: IVCurve) -> Dict[str, Optional[Tuple[float, float]]]:
    out: Dict[str, Optional[Tuple[float, float]]] = {}
    for side, d in DELTA_GRID:
        label = f"{side}{abs(int(round(d * 100)))}d"
        out[label] = strike_for_delta(curve, d, side == "call")
    return out


@dataclass
class SkewMetrics:
    atm_iv: Optional[float]
    put25_iv: Optional[float]
    call25_iv: Optional[float]
    put25_strike: Optional[float]
    call25_strike: Optional[float]
    rr25: Optional[float]   # 25dPut IV − 25dCall IV（pt）
    bf25: Optional[float]   # (25dP + 25dC)/2 − ATM IV（pt）


def skew_metrics(curve: IVCurve) -> SkewMetrics:
    atm = curve.atm_iv()
    p = strike_for_delta(curve, -0.25, False)
    c = strike_for_delta(curve, 0.25, True)
    p_iv = p[1] if p else None
    c_iv = c[1] if c else None
    rr = (p_iv - c_iv) * 100 if (p_iv is not None and c_iv is not None) else None
    bf = None
    if p_iv is not None and c_iv is not None and atm is not None:
        bf = ((p_iv + c_iv) / 2 - atm) * 100
    return SkewMetrics(
        atm_iv=atm * 100 if atm is not None else None,
        put25_iv=p_iv * 100 if p_iv is not None else None,
        call25_iv=c_iv * 100 if c_iv is not None else None,
        put25_strike=p[0] if p else None,
        call25_strike=c[0] if c else None,
        rr25=rr, bf25=bf,
    )


# ---------------------------------------------------------------------------
# ガンマ・エクスポージャー（GEX）
# ---------------------------------------------------------------------------

@dataclass
class GexProfile:
    levels: List[Tuple[float, float]]   # (指数水準, Net GEX 億円)
    at_forward: float
    flip: Optional[float]
    regime: str                         # "ロングガンマ" / "ショートガンマ"


def net_gex_at(chain: Chain, curve: IVCurve, level: float) -> float:
    """
    指数が level にあるときの Net GEX（億円）。

    定義: Σ[γ_call·OI_call − γ_put·OI_put] × 1,000 × level² × 0.01
    「1%動いたときにディーラーのデルタが何円分動くか」を表す。
    行使価格ごとのIVは当日カーブを固定して用いる（スティッキーストライク）。
    """
    if curve.t <= 0:
        return 0.0
    total = 0.0
    for k, r in chain.rows.items():
        sigma = curve.at(k)
        if sigma is None or sigma <= 0:
            continue
        g = bs_gamma(level, k, curve.t, sigma)
        total += g * (r.call_oi - r.put_oi)
    return total * CONTRACT_MULTIPLIER * level * level * 0.01 / OKU


def gex_profile(chain: Chain, curve: IVCurve, forward: float,
                span: float = 0.10, step: float = 500.0) -> GexProfile:
    lo = _round_to(forward * (1 - span), step)
    hi = _round_to(forward * (1 + span), step)
    levels: List[Tuple[float, float]] = []
    x = lo
    while x <= hi + 1e-9:
        levels.append((x, net_gex_at(chain, curve, x)))
        x += step
    at_f = net_gex_at(chain, curve, forward)

    # フリップ = Net GEX が符号反転する水準。細かく二分探索する。
    flip: Optional[float] = None
    for (x1, y1), (x2, y2) in zip(levels, levels[1:]):
        if y1 == 0:
            flip = x1
            break
        if y1 * y2 < 0:
            a, b = x1, x2
            for _ in range(60):
                m = 0.5 * (a + b)
                if net_gex_at(chain, curve, m) * y1 > 0:
                    a = m
                else:
                    b = m
            flip = 0.5 * (a + b)
            break
    return GexProfile(levels=levels, at_forward=at_f, flip=flip,
                      regime="ロングガンマ" if at_f >= 0 else "ショートガンマ")


def _round_to(x: float, step: float) -> float:
    return round(x / step) * step


# ---------------------------------------------------------------------------
# max_pain / 壁 / ピン候補
# ---------------------------------------------------------------------------

@dataclass
class MaxPain:
    strike: float
    payout_oku: float
    slope_pct: float   # max_pain から ±5% 動いたときの支払総額の増加率（引力の強さ）


def max_pain(chain: Chain, min_oi: int = 50) -> Optional[MaxPain]:
    """SQ値がどこに来ればオプション買い手への支払総額が最小になるか。"""
    rows = [(k, r) for k, r in chain.rows.items() if (r.call_oi + r.put_oi) >= min_oi]
    if not rows:
        return None
    candidates = sorted(k for k, _ in rows)

    def payout(settle: float) -> float:
        total = 0.0
        for k, r in rows:
            if settle > k:
                total += r.call_oi * (settle - k)
            elif settle < k:
                total += r.put_oi * (k - settle)
        return total * CONTRACT_MULTIPLIER / OKU

    best = min(candidates, key=payout)
    p0 = payout(best)
    if p0 <= 0:
        return MaxPain(strike=best, payout_oku=p0, slope_pct=0.0)
    side = 0.5 * (payout(best * 1.05) + payout(best * 0.95))
    return MaxPain(strike=best, payout_oku=p0, slope_pct=(side - p0) / p0 * 100)


@dataclass
class Wall:
    strike: float
    oi: int
    price: Optional[float]
    nominal_oku: float
    score: float
    side: str


def wall_map(chain: Chain, curve: IVCurve, forward: float, half_width: float = 375.0,
             top: int = 6, grid: float = 500.0) -> Dict[str, List[Wall]]:
    """
    ±375円で集計した OTM の壁。スコアは 0〜100 の相対指標で、
      建玉の厚み(50%) + 名目金額(30%) + 現値からの近さ(20%)
    の合成。名目金額は現値との距離で機械的に動くため、前日比の判断には使わない。

    集計の中心は 500円刻みの行使価格に限り、上位に隣接ピークが並ばないよう
    ±half_width 以内の重複は高スコア側だけを残す。
    """
    out: Dict[str, List[Wall]] = {"upper": [], "lower": []}
    for side in ("upper", "lower"):
        is_call = side == "upper"
        cands: List[Wall] = []
        for center in chain.strikes:
            if center % grid != 0:
                continue
            if is_call and center <= forward:
                continue
            if not is_call and center >= forward:
                continue
            oi = 0
            nominal = 0.0
            for k, r in chain.rows.items():
                if abs(k - center) <= half_width:
                    n = r.call_oi if is_call else r.put_oi
                    p = r.call_price if is_call else r.put_price
                    oi += n
                    if p:
                        nominal += n * p * CONTRACT_MULTIPLIER
            if oi <= 0:
                continue
            row = chain.rows[center]
            cands.append(Wall(strike=center, oi=oi,
                              price=row.call_price if is_call else row.put_price,
                              nominal_oku=nominal / OKU, score=0.0, side=side))
        if not cands:
            continue
        max_oi = max(c.oi for c in cands) or 1
        max_nom = max(c.nominal_oku for c in cands) or 1.0
        for c in cands:
            prox = math.exp(-abs(c.strike - forward) / (0.05 * forward))
            c.score = 100 * (0.5 * c.oi / max_oi + 0.3 * c.nominal_oku / max_nom + 0.2 * prox)
        cands.sort(key=lambda c: c.score, reverse=True)
        picked: List[Wall] = []
        for c in cands:
            if any(abs(c.strike - p.strike) <= half_width for p in picked):
                continue   # 同じ塊を二重に数えない
            picked.append(c)
            if len(picked) >= top:
                break
        out[side] = picked
    return out


@dataclass
class Condor:
    """建玉の壁が作る想定レンジ。lower / upper を名前で持ち、順序の取り違えを防ぐ。"""
    lower: Optional[float]
    upper: Optional[float]

    @property
    def width(self) -> Optional[float]:
        if self.lower is None or self.upper is None:
            return None
        return self.upper - self.lower


def iron_condor(walls: Dict[str, List[Wall]]) -> Condor:
    """最上位スコアの OTM PUT 壁 = 下限（床）、OTM CALL 壁 = 上限（蓋）。"""
    up = walls.get("upper") or []
    lo = walls.get("lower") or []
    return Condor(lower=lo[0].strike if lo else None,
                  upper=up[0].strike if up else None)


def pin_candidates(chain: Chain, forward: float, top: int = 4,
                   span: float = 0.10) -> List[Tuple[float, float, int]]:
    """
    CALL+PUT の名目金額が最も大きい行使価格。残存が長い間は作用しない参考値。

    現値から span 以内の行使価格に限る。深いITMは本質価値だけで名目が膨らみ、
    ピンの引力とは無関係にランキングを占領してしまうため。
    """
    out = []
    for k, r in chain.rows.items():
        if abs(k - forward) > span * forward:
            continue
        nominal = 0.0
        if r.call_price:
            nominal += r.call_oi * r.call_price * CONTRACT_MULTIPLIER
        if r.put_price:
            nominal += r.put_oi * r.put_price * CONTRACT_MULTIPLIER
        if nominal > 0:
            out.append((k, nominal / OKU, r.call_oi + r.put_oi))
    out.sort(key=lambda x: x[1], reverse=True)
    return out[:top]


# ---------------------------------------------------------------------------
# PUTブックの実効ヘッジ量
# ---------------------------------------------------------------------------

@dataclass
class PutBook:
    oi: int
    effective_delta: float          # Σ |δ| × 建玉（枚）
    vega_oku: float                 # IV 1pt あたり（億円）
    premium_oku: float              # 理論プレミアム総額（億円）
    junk_oi: int                    # 20円未満の建玉
    junk_ratio: float
    per_strike_delta: Dict[float, float]   # 行使価格 → 当日デルタ


def put_book(chain: Chain, curve: IVCurve, forward: float) -> PutBook:
    total_delta = 0.0
    total_vega = 0.0
    total_prem = 0.0
    junk = 0
    deltas: Dict[float, float] = {}
    oi_sum = 0
    for k, r in sorted(chain.rows.items()):
        if r.put_oi <= 0:
            continue
        sigma = curve.at(k)
        if sigma is None or sigma <= 0:
            continue
        oi_sum += r.put_oi
        d = bs_delta(forward, k, curve.t, sigma, False)
        deltas[k] = d
        total_delta += abs(d) * r.put_oi
        total_vega += bs_vega(forward, k, curve.t, sigma) / 100.0 * r.put_oi * CONTRACT_MULTIPLIER
        total_prem += bs_price(forward, k, curve.t, sigma, False) * r.put_oi * CONTRACT_MULTIPLIER
        if r.put_price is not None and r.put_price < JUNK_PREMIUM:
            junk += r.put_oi
    return PutBook(oi=oi_sum, effective_delta=total_delta, vega_oku=total_vega / OKU,
                   premium_oku=total_prem / OKU, junk_oi=junk,
                   junk_ratio=(junk / oi_sum * 100) if oi_sum else 0.0,
                   per_strike_delta=deltas)


@dataclass
class DeltaDecomposition:
    total_change: float
    position: float          # 行使価格別の建玉増減 × 当日デルタ
    spot_and_time: float     # 残差（現値移動＋時間経過）
    contributions: List[Tuple[float, int, int, int, float, float]]
    # (行使価格, 前日建玉, 当日建玉, 差, 当日デルタ, 実効δ寄与)


def decompose_effective_delta(today: Chain, prev: Chain, book: PutBook,
                              prev_effective: float) -> DeltaDecomposition:
    """
    実効デルタの増減を「新規に建てられた分」と「現値が動いて既存玉が立ち上がった分」に分ける。
    元レポートが最重要視している分解。ポジション要因だけが恒久的な防御力。
    """
    contribs: List[Tuple[float, int, int, int, float, float]] = []
    position = 0.0
    strikes = set(today.rows) | set(prev.rows)
    for k in sorted(strikes):
        t_oi = today.rows[k].put_oi if k in today.rows else 0
        p_oi = prev.rows[k].put_oi if k in prev.rows else 0
        diff = t_oi - p_oi
        if diff == 0:
            continue
        d = book.per_strike_delta.get(k)
        if d is None:
            continue
        # 建玉が減れば防御力も減るので、符号はそのまま乗せる
        contrib = diff * abs(d)
        position += contrib
        contribs.append((k, p_oi, t_oi, diff, d, contrib))
    contribs.sort(key=lambda x: abs(x[5]), reverse=True)
    total = book.effective_delta - prev_effective
    return DeltaDecomposition(total_change=total, position=position,
                              spot_and_time=total - position, contributions=contribs)


MONEYNESS_BUCKETS = (
    ("ATM−5%", 0.95, 1.10),
    ("−5〜−10%", 0.90, 0.95),
    ("−10〜−20%", 0.80, 0.90),
    ("−20%以遠", 0.0, 0.80),
)


def put_buckets(chain: Chain, curve: IVCurve, forward: float) -> List[Tuple[str, int, float]]:
    """モネネスバケット別の建玉と実効デルタ。境界が現値と一緒に動くので前日比は再分類を含む。"""
    out = []
    for label, lo, hi in MONEYNESS_BUCKETS:
        oi = 0
        eff = 0.0
        for k, r in chain.rows.items():
            if r.put_oi <= 0:
                continue
            m = k / forward
            if lo <= m < hi:
                oi += r.put_oi
                sigma = curve.at(k)
                if sigma:
                    eff += abs(bs_delta(forward, k, curve.t, sigma, False)) * r.put_oi
        out.append((label, oi, eff))
    return out


# ---------------------------------------------------------------------------
# SQ参考帯（リスク中立分布の抽出）
# ---------------------------------------------------------------------------

def _cdf_from_calls(points: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """
    Breeden-Litzenberger: ∂C/∂K = F(K) − 1 より、CALL価格の傾きから累積分布を得る。
    実勢価格は隣接行使価格間で単調性が崩れるため、[0,1] にクリップして単調化する。
    """
    pts = sorted(points)
    if len(pts) < 3:
        return []
    raw: List[Tuple[float, float]] = []
    for (k1, c1), (k2, c2) in zip(pts, pts[1:]):
        if k2 <= k1:
            continue
        slope = (c2 - c1) / (k2 - k1)
        cdf = min(1.0, max(0.0, 1.0 + slope))
        raw.append((0.5 * (k1 + k2), cdf))
    # 単調非減少に整える
    out: List[Tuple[float, float]] = []
    running = 0.0
    for k, v in raw:
        running = max(running, v)
        out.append((k, running))
    return out


def _quantile(cdf: Sequence[Tuple[float, float]], q: float) -> Optional[float]:
    if not cdf:
        return None
    for (k1, v1), (k2, v2) in zip(cdf, cdf[1:]):
        if v1 <= q <= v2:
            if v2 == v1:
                return k1
            w = (q - v1) / (v2 - v1)
            return k1 + w * (k2 - k1)
    if q < cdf[0][1]:
        return cdf[0][0]
    return cdf[-1][0]


@dataclass
class SqBands:
    center: float                 # フォワード由来（本レポートの採用値）
    sigma_abs: Optional[float]    # 1σ（円）
    band50: Tuple[Optional[float], Optional[float]]
    band80: Tuple[Optional[float], Optional[float]]
    prob_in_condor: Optional[float]
    median_premium: Optional[float]   # 実勢価格から抽出した中央値
    median_iv: Optional[float]        # スマイル再構成価格から抽出した中央値
    divergence: Optional[float]       # 手法クロスチェック（プレミアム法 − IV法）
    reliable: bool                    # 乖離が ±500円 以内か


def sq_bands(chain: Chain, curve: IVCurve, forward: float,
             condor: "Condor") -> SqBands:
    atm = curve.atm_iv()
    sigma_abs = forward * atm * math.sqrt(curve.t) if (atm and curve.t > 0) else None

    # プレミアム法: 実勢のCALL価格をそのまま使う
    market_pts = [(k, r.call_price) for k, r in chain.rows.items()
                  if r.call_price and r.call_price > 0]
    cdf_market = _cdf_from_calls(market_pts)

    # IV法: スマイルから理論CALL価格を再構成し、同じ手続きで分布を取る
    cdf_smile: List[Tuple[float, float]] = []
    if atm and curve.t > 0 and curve.otm_iv:
        grid: List[Tuple[float, float]] = []
        k = _round_to(forward * 0.55, 50.0)
        hi = _round_to(forward * 1.55, 50.0)
        while k <= hi:
            sigma = curve.at(k)
            if sigma and sigma > 0:
                grid.append((k, bs_price(forward, k, curve.t, sigma, True)))
            k += 50.0
        cdf_smile = _cdf_from_calls(grid)

    med_prem = _quantile(cdf_market, 0.5)
    med_iv = _quantile(cdf_smile, 0.5)
    div = (med_prem - med_iv) if (med_prem is not None and med_iv is not None) else None

    band50 = (_quantile(cdf_smile, 0.25), _quantile(cdf_smile, 0.75))
    band80 = (_quantile(cdf_smile, 0.10), _quantile(cdf_smile, 0.90))

    prob = None
    if cdf_smile and condor.lower and condor.upper:
        c_lo = _interp(dict(cdf_smile), condor.lower)
        c_up = _interp(dict(cdf_smile), condor.upper)
        if c_lo is not None and c_up is not None:
            prob = max(0.0, c_up - c_lo) * 100

    return SqBands(center=forward, sigma_abs=sigma_abs, band50=band50, band80=band80,
                   prob_in_condor=prob, median_premium=med_prem, median_iv=med_iv,
                   divergence=div, reliable=(div is not None and abs(div) <= 500))


# ---------------------------------------------------------------------------
# 前日比（共通窓ベース）
# ---------------------------------------------------------------------------

@dataclass
class WindowCompare:
    lo: float
    hi: float
    count: int
    call_prev: int
    call_today: int
    put_prev: int
    put_today: int
    pcr_prev: Optional[float]
    pcr_today: Optional[float]
    call_centroid_prev: Optional[float]
    call_centroid_today: Optional[float]
    put_centroid_prev: Optional[float]
    put_centroid_today: Optional[float]
    put_tail_60k: Tuple[int, int]
    put_tail_53k: Tuple[int, int]
    call_changes: List[Tuple[float, int, int, int]]
    put_changes: List[Tuple[float, int, int, int]]


def _centroid(oi: Dict[float, int]) -> Optional[float]:
    total = sum(oi.values())
    if total <= 0:
        return None
    return sum(k * n for k, n in oi.items()) / total


def compare_window(today: Chain, prev: Chain, threshold: int = 100) -> Optional[WindowCompare]:
    """
    捕捉窓が日々ずれるため、共通の行使価格（積集合）だけで前日比を取る。
    これをやらないと「窓の外に出た玉」が増減に見えてしまう。
    """
    common = sorted(set(today.rows) & set(prev.rows))
    if not common:
        return None

    def side(chain: Chain, key: str) -> Dict[float, int]:
        return {k: getattr(chain.rows[k], key) for k in common}

    c_t, c_p = side(today, "call_oi"), side(prev, "call_oi")
    p_t, p_p = side(today, "put_oi"), side(prev, "put_oi")
    ct, cp = sum(c_t.values()), sum(c_p.values())
    pt, pp = sum(p_t.values()), sum(p_p.values())

    call_changes = [(k, c_p[k], c_t[k], c_t[k] - c_p[k]) for k in common
                    if abs(c_t[k] - c_p[k]) >= threshold]
    put_changes = [(k, p_p[k], p_t[k], p_t[k] - p_p[k]) for k in common
                   if abs(p_t[k] - p_p[k]) >= threshold]
    call_changes.sort(key=lambda x: x[3], reverse=True)
    put_changes.sort(key=lambda x: x[3], reverse=True)

    return WindowCompare(
        lo=common[0], hi=common[-1], count=len(common),
        call_prev=cp, call_today=ct, put_prev=pp, put_today=pt,
        pcr_prev=(pp / cp) if cp else None, pcr_today=(pt / ct) if ct else None,
        call_centroid_prev=_centroid(c_p), call_centroid_today=_centroid(c_t),
        put_centroid_prev=_centroid(p_p), put_centroid_today=_centroid(p_t),
        put_tail_60k=(sum(n for k, n in p_p.items() if k <= 60000),
                      sum(n for k, n in p_t.items() if k <= 60000)),
        put_tail_53k=(sum(n for k, n in p_p.items() if k <= 53000),
                      sum(n for k, n in p_t.items() if k <= 53000)),
        call_changes=call_changes, put_changes=put_changes,
    )


def bucket_compare(today: Chain, prev: Chain, forward: float, side: str,
                   half_width: float = 375.0, span: float = 0.06,
                   top: int = 9) -> List[Tuple[float, int, int, int]]:
    """現値近傍の ±375円 バケットでの前日比。単一行使価格のノイズを潰して見る。"""
    key = "call_oi" if side == "call" else "put_oi"
    centers = [k for k in sorted(set(today.rows) & set(prev.rows))
               if abs(k - forward) <= span * forward and k % 500 == 0]
    rows: List[Tuple[float, int, int, int]] = []
    for c in centers:
        t = sum(getattr(r, key) for k, r in today.rows.items() if abs(k - c) <= half_width)
        p = sum(getattr(r, key) for k, r in prev.rows.items() if abs(k - c) <= half_width)
        rows.append((c, p, t, t - p))
    rows.sort(key=lambda x: x[3], reverse=True)
    return rows[:top]


# ---------------------------------------------------------------------------
# 中期レンジ（複数限月の合成）
# ---------------------------------------------------------------------------

@dataclass
class CompositeRange:
    ceiling: Optional[Tuple[float, int]]
    floor: Optional[Tuple[float, int]]
    upper: List[Tuple[float, int]]
    lower: List[Tuple[float, int]]
    per_month: List[Tuple[str, int, int, Optional[float]]]


def composite_range(chains: Sequence[Chain], grid: float = 250.0,
                    top: int = 5) -> CompositeRange:
    """複数限月のネット建玉（CALL − PUT）を合算し、天井と床を出す。"""
    net: Dict[float, int] = {}
    per_month: List[Tuple[str, int, int, Optional[float]]] = []
    for ch in chains:
        c_tot = ch.total_oi("call")
        p_tot = ch.total_oi("put")
        per_month.append((ch.contract_month, c_tot, p_tot,
                          (p_tot / c_tot) if c_tot else None))
        for k, r in ch.rows.items():
            kk = _round_to(k, grid)
            net[kk] = net.get(kk, 0) + r.call_oi - r.put_oi
    if not net:
        return CompositeRange(None, None, [], [], per_month)
    items = sorted(net.items(), key=lambda x: x[1], reverse=True)
    upper = [(k, v) for k, v in items if v > 0][:top]
    lower = sorted([(k, v) for k, v in items if v < 0], key=lambda x: x[1])[:top]
    return CompositeRange(
        ceiling=upper[0] if upper else None,
        floor=lower[0] if lower else None,
        upper=upper, lower=lower, per_month=per_month,
    )
