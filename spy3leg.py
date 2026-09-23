#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
レバポ（SPY / XSP 14DTE 3脚Put戦略）計算エンジン

外部通信を一切行わない純粋な計算層。実際のオプションチェーン（MF-Boost
Option Info などから書き出したCSV）を受け取り、次を行う。

  ① Call Δ ≈ +0.90 の行使価格 K1 の Put を 1 枚 Long（Put Δ ≈ −0.10, OTM）
  ② Call Δ ≈ +0.15 の行使価格 K2 の Put を 1 枚 Short（Put Δ ≈ −0.85, ITM）
  ③ K1 < K3 < K2 の Put を 1 枚 Long。K3 は固定デルタではなく、
     1セットの満期最大損失 < 上限（既定 $2,500）を満たす行使価格を総当たりで決める。

採用している慣例（レポートに明示する内容）:
  * 価格は Bid/Ask の Mid。約定の悪い側（Long=Ask, Short=Bid）でも併記する。
  * 満期損益は行使価格だけで決まる区分線形関数なので、折れ点で厳密に評価する。
  * シナリオ再評価はヨーロピアン Black-Scholes（金利 r・配当利回り q）。
    各脚の σ は Mid から逆算し、エントリー時点で理論値 = Mid になるよう合わせる
    （逆算できない脚だけチェーンのIV列を使う）。SPY はアメリカンなので、
    深いITMの②はこの近似より早期行使価値の分だけ高く、その差は別途フラグで示す。
  * IVシフトは全脚に同じ vol point を足す（sticky strike の平行移動）。
    スキューの形の変化はこの再評価に入っていない。
  * 残存 T = 暦日 / 365。Theta は 1暦日あたり、Vega は 1 vol point あたり。
"""

from __future__ import annotations

import csv
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

MULTIPLIER = 100          # SPY / XSP とも 1枚 = 100倍
DEFAULT_MAX_LOSS = 2500.0
DEFAULT_R = 0.04
DEFAULT_Q = 0.012

SPOT_MOVES = (0.10, 0.05, 0.02, 0.0, -0.02, -0.05, -0.10)
IV_SHIFTS = (-10.0, -5.0, 0.0, 2.0, 5.0, 10.0, 20.0)   # vol points
EXIT_DTES = (10, 7, 5, 3, 1, 0)


# ---------------------------------------------------------------------------
# Black-Scholes（連続配当利回り q）
# ---------------------------------------------------------------------------

def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(s: float, k: float, t: float, r: float, q: float, sigma: float) -> Tuple[float, float]:
    v = sigma * math.sqrt(t)
    d1 = (math.log(s / k) + (r - q + 0.5 * sigma * sigma) * t) / v
    return d1, d1 - v


def bs_price(s: float, k: float, t: float, r: float, q: float, sigma: float, is_call: bool) -> float:
    if t <= 0 or sigma <= 0:
        return max((s - k) if is_call else (k - s), 0.0)
    d1, d2 = _d1_d2(s, k, t, r, q, sigma)
    dq, dr = math.exp(-q * t), math.exp(-r * t)
    if is_call:
        return s * dq * norm_cdf(d1) - k * dr * norm_cdf(d2)
    return k * dr * norm_cdf(-d2) - s * dq * norm_cdf(-d1)


@dataclass
class Greeks:
    delta: float = 0.0
    gamma: float = 0.0
    vega: float = 0.0    # 1 vol point あたり
    theta: float = 0.0   # 1暦日あたり

    def scaled(self, w: float) -> "Greeks":
        return Greeks(self.delta * w, self.gamma * w, self.vega * w, self.theta * w)

    def __add__(self, o: "Greeks") -> "Greeks":
        return Greeks(self.delta + o.delta, self.gamma + o.gamma,
                      self.vega + o.vega, self.theta + o.theta)


def bs_greeks(s: float, k: float, t: float, r: float, q: float, sigma: float, is_call: bool) -> Greeks:
    if t <= 0 or sigma <= 0:
        itm = (s > k) if is_call else (s < k)
        return Greeks(delta=(1.0 if is_call else -1.0) if itm else 0.0)
    d1, d2 = _d1_d2(s, k, t, r, q, sigma)
    dq, dr = math.exp(-q * t), math.exp(-r * t)
    sq = math.sqrt(t)
    gamma = dq * norm_pdf(d1) / (s * sigma * sq)
    vega = s * dq * norm_pdf(d1) * sq / 100.0
    common = -s * dq * norm_pdf(d1) * sigma / (2 * sq)
    if is_call:
        delta = dq * norm_cdf(d1)
        theta = common - r * k * dr * norm_cdf(d2) + q * s * dq * norm_cdf(d1)
    else:
        delta = -dq * norm_cdf(-d1)
        theta = common + r * k * dr * norm_cdf(-d2) - q * s * dq * norm_cdf(-d1)
    return Greeks(delta, gamma, vega, theta / 365.0)


def implied_vol(price: float, s: float, k: float, t: float, r: float, q: float,
                is_call: bool, lo: float = 1e-4, hi: float = 5.0) -> Optional[float]:
    """二分法。価格が無裁定の範囲外なら None。"""
    if t <= 0 or price <= 0:
        return None
    if not (bs_price(s, k, t, r, q, lo, is_call) < price < bs_price(s, k, t, r, q, hi, is_call)):
        return None
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if bs_price(s, k, t, r, q, mid, is_call) < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-7:
            break
    return 0.5 * (lo + hi)


# ---------------------------------------------------------------------------
# チェーン
# ---------------------------------------------------------------------------

@dataclass
class Quote:
    bid: Optional[float] = None
    ask: Optional[float] = None
    mid: Optional[float] = None
    delta: Optional[float] = None
    iv: Optional[float] = None       # 小数（0.18 = 18%）
    gamma: Optional[float] = None
    vega: Optional[float] = None
    theta: Optional[float] = None

    @property
    def price(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None and self.ask >= self.bid > 0:
            return 0.5 * (self.bid + self.ask)
        return self.mid

    @property
    def spread(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None:
            return self.ask - self.bid
        return None


@dataclass
class Row:
    strike: float
    call: Quote = field(default_factory=Quote)
    put: Quote = field(default_factory=Quote)


@dataclass
class Chain:
    expiration: str
    dte: int
    rows: Dict[float, Row]

    def strikes(self) -> List[float]:
        return sorted(self.rows)


# ヘッダの別名（小文字・空白/記号除去後で照合）
_ALIASES = {
    "expiration": ("expiration", "expiry", "exp", "expirationdate", "満期", "満期日", "限月"),
    "dte": ("dte", "daystoexpiration", "残存日数"),
    "strike": ("strike", "k", "strikeprice", "行使価格", "権利行使価格"),
    "type": ("type", "cp", "putcall", "callput", "optiontype", "種別"),
    "spot": ("spot", "underlying", "underlyingprice", "原資産価格"),
    "bid": ("bid",), "ask": ("ask",), "mid": ("mid", "mark"),
    "delta": ("delta",), "iv": ("iv", "impliedvolatility", "impliedvol"),
    "gamma": ("gamma",), "vega": ("vega",), "theta": ("theta",),
}
_FIELDS = ("bid", "ask", "mid", "delta", "iv", "gamma", "vega", "theta")


def _norm(h: str) -> str:
    return "".join(ch for ch in h.strip().lower() if ch.isalnum() or ord(ch) > 127)


def _canon(header: str) -> Optional[Tuple[Optional[str], str]]:
    """'Put IV' → ('put','iv')、'Strike' → (None,'strike')。"""
    h = _norm(header)
    side = None
    for prefix, sd in (("call", "call"), ("put", "put"), ("c", "call"), ("p", "put")):
        rest = h[len(prefix):]
        if h.startswith(prefix) and any(rest in al for k, al in _ALIASES.items() if k in _FIELDS):
            side, h = sd, rest
            break
    for key, aliases in _ALIASES.items():
        if h in aliases:
            return side, key
    return None


def _num(v: str) -> Optional[float]:
    v = (v or "").strip().replace(",", "").replace("$", "")
    if v in ("", "-", "--", "N/A", "n/a", "nan"):
        return None
    pct = v.endswith("%")
    try:
        x = float(v.rstrip("%"))
    except ValueError:
        return None
    return x / 100.0 if pct else x


def load_chains(path: str) -> Tuple[List[Chain], Optional[float]]:
    """CSV を満期ごとの Chain に。横持ち（Call/Put 列が並ぶ）と縦持ち（type 列）の両対応。"""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
        cols = [_canon(h) for h in header]
        data = list(reader)

    spot = None
    by_exp: Dict[str, Dict[float, Row]] = {}
    dtes: Dict[str, int] = {}
    for rec in data:
        if not any(x.strip() for x in rec):
            continue
        vals: Dict[Tuple[Optional[str], str], str] = {}
        for c, v in zip(cols, rec):
            if c:
                vals[c] = v
        k = _num(vals.get((None, "strike"), ""))
        if k is None:
            continue
        exp = vals.get((None, "expiration"), "").strip() or "?"
        d = _num(vals.get((None, "dte"), ""))
        if d is not None:
            dtes[exp] = int(round(d))
        s = _num(vals.get((None, "spot"), ""))
        if s:
            spot = s
        row = by_exp.setdefault(exp, {}).setdefault(k, Row(k))
        typ = vals.get((None, "type"), "").strip().lower()
        for (side, key), v in vals.items():
            if key not in _FIELDS:
                continue
            if side is None:
                if typ.startswith("c"):
                    side = "call"
                elif typ.startswith("p"):
                    side = "put"
                else:
                    continue
            x = _num(v)
            if x is not None and key == "iv" and x > 3.0:   # 18.5 → 0.185
                x /= 100.0
            setattr(getattr(row, side), key, x)

    chains = []
    for exp, rows in by_exp.items():
        dte = dtes.get(exp)
        if dte is None:
            try:
                dte = (date.fromisoformat(exp) - date.today()).days
            except ValueError:
                dte = -1
        chains.append(Chain(exp, dte, rows))
    return sorted(chains, key=lambda c: c.dte), spot


def select_expiry(chains: Sequence[Chain], target_dte: int = 14) -> Chain:
    """目標DTEに最も近い満期。等距離なら長い方（14DTE未満に入らないように）。"""
    return min(chains, key=lambda c: (abs(c.dte - target_dte), -c.dte))


def implied_spot(chain: Chain, r: float, q: float) -> Optional[float]:
    """Put-Call パリティ C − P = S e^{-qT} − K e^{-rT} の ATM 近傍中央値。"""
    t = max(chain.dte, 0) / 365.0
    pts = []
    for row in chain.rows.values():
        c, p = row.call.price, row.put.price
        if c is not None and p is not None:
            pts.append((abs(c - p), (c - p + row.strike * math.exp(-r * t)) * math.exp(q * t)))
    if not pts:
        return None
    pts.sort()
    return statistics.median(s for _, s in pts[:5])


def strike_for_call_delta(chain: Chain, target: float) -> Optional[float]:
    """Call Δ が target に最も近い行使価格。Put 側に Put Δ しか無い行は 1+PutΔ で代用。"""
    best = None
    for k, row in chain.rows.items():
        d = row.call.delta
        if d is None and row.put.delta is not None:
            d = 1.0 + row.put.delta
        if d is None or row.put.price is None:
            continue
        key = abs(d - target)
        if best is None or key < best[0]:
            best = (key, k)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# 3脚ストラクチャ
# ---------------------------------------------------------------------------

@dataclass
class Leg:
    strike: float
    qty: int                # +1 = Long, −1 = Short
    quote: Quote
    sigma: float            # 再評価に使う σ（Mid から逆算）


@dataclass
class Structure:
    legs: List[Leg]
    spot: float
    dte: int
    r: float
    q: float

    # --- 価格 ---
    def debit(self, fill: str = "mid") -> float:
        """1株あたりの支払い（負ならクレジット）。fill='natural' は Long=Ask / Short=Bid。"""
        tot = 0.0
        for lg in self.legs:
            px = lg.quote.price
            if fill == "natural":
                px = (lg.quote.ask if lg.qty > 0 else lg.quote.bid) or px
            tot += lg.qty * px
        return tot

    # --- 満期損益 ---
    def payoff(self, s: float, fill: str = "mid") -> float:
        """満期時の 1セット損益（$、multiplier 込み）。"""
        intrinsic = sum(lg.qty * max(lg.strike - s, 0.0) for lg in self.legs)
        return (intrinsic - self.debit(fill)) * MULTIPLIER

    def _grid(self) -> List[float]:
        ks = sorted({lg.strike for lg in self.legs})
        return [0.0] + ks + [ks[-1] * 2]

    def max_loss(self, fill: str = "mid") -> float:
        """区分線形なので折れ点の最小値が最大損失（正の $ で返す）。"""
        return -min(self.payoff(s, fill) for s in self._grid())

    def max_profit(self, fill: str = "mid") -> Tuple[float, float]:
        """(下側 S→0 での利益, 上側 S≥K2 での利益)。"""
        g = self._grid()
        return self.payoff(g[0], fill), self.payoff(g[-1], fill)

    def breakevens(self, fill: str = "mid") -> List[float]:
        g = self._grid()
        out = []
        for a, b in zip(g, g[1:]):
            pa, pb = self.payoff(a, fill), self.payoff(b, fill)
            if pa == 0:
                out.append(a)
            elif pa * pb < 0:
                out.append(a + (b - a) * pa / (pa - pb))
        return out

    # --- Greeks ---
    def chain_greeks(self) -> Optional[Greeks]:
        """チェーンに載っている Greeks の合計（$ ではなく 1セット×multiplier 単位）。欠損があれば None。"""
        tot = Greeks()
        for lg in self.legs:
            qt = lg.quote
            if None in (qt.delta, qt.gamma, qt.vega, qt.theta):
                return None
            tot = tot + Greeks(qt.delta, qt.gamma, qt.vega, qt.theta).scaled(lg.qty * MULTIPLIER)
        return tot

    def model_greeks(self, s: Optional[float] = None, dte: Optional[float] = None,
                     iv_shift: float = 0.0) -> Greeks:
        s = self.spot if s is None else s
        t = (self.dte if dte is None else dte) / 365.0
        tot = Greeks()
        for lg in self.legs:
            sig = max(lg.sigma + iv_shift / 100.0, 0.01)
            tot = tot + bs_greeks(s, lg.strike, t, self.r, self.q, sig, False).scaled(lg.qty * MULTIPLIER)
        return tot

    # --- 再評価 ---
    def value(self, s: float, dte: float, iv_shift: float = 0.0) -> float:
        t = max(dte, 0.0) / 365.0
        v = 0.0
        for lg in self.legs:
            sig = max(lg.sigma + iv_shift / 100.0, 0.01)
            v += lg.qty * bs_price(s, lg.strike, t, self.r, self.q, sig, False)
        return v * MULTIPLIER

    def pnl(self, spot_move: float, iv_shift: float, exit_dte: float) -> float:
        """エントリー（理論値 = Mid）からの損益 $。"""
        return self.value(self.spot * (1 + spot_move), exit_dte, iv_shift) - self.value(self.spot, self.dte)

    def attribution(self, spot_move: float, iv_shift: float, exit_dte: float) -> Dict[str, float]:
        """時間 → 価格 → IV の順に一つずつ動かした逐次再評価の分解（合計 = 全体損益）。"""
        s1 = self.spot * (1 + spot_move)
        v0 = self.value(self.spot, self.dte)
        v_t = self.value(self.spot, exit_dte)
        v_ts = self.value(s1, exit_dte)
        v_tsv = self.value(s1, exit_dte, iv_shift)
        return {"theta": v_t - v0, "delta_gamma": v_ts - v_t, "vega": v_tsv - v_ts, "total": v_tsv - v0}

    # --- 実務リスク ---
    def short_extrinsic(self) -> List[Tuple[float, float]]:
        """Short 脚の時間価値（1株あたり）。SPY の早期割当ての目安。"""
        return [(lg.strike, lg.quote.price - max(lg.strike - self.spot, 0.0))
                for lg in self.legs if lg.qty < 0]


def _leg(chain: Chain, k: float, qty: int, spot: float, r: float, q: float) -> Leg:
    qt = chain.rows[k].put
    t = max(chain.dte, 0) / 365.0
    sig = implied_vol(qt.price, spot, k, t, r, q, False) if qt.price else None
    if sig is None:
        sig = qt.iv if qt.iv else 0.20
    return Leg(k, qty, qt, sig)


def build(chain: Chain, k1: float, k2: float, k3: float, spot: float,
          r: float = DEFAULT_R, q: float = DEFAULT_Q) -> Structure:
    legs = [_leg(chain, k1, +1, spot, r, q), _leg(chain, k2, -1, spot, r, q), _leg(chain, k3, +1, spot, r, q)]
    return Structure(legs, spot, chain.dte, r, q)


@dataclass
class Candidate:
    k3: float
    st: Structure
    debit: float
    debit_natural: float
    max_loss: float
    max_loss_natural: float
    profit_down: float
    profit_up: float
    breakevens: List[float]
    greeks: Greeks            # チェーン値が揃えばチェーン、無ければモデル
    greeks_source: str
    feasible: bool
    below_atm: bool


def scan_third_leg(chain: Chain, k1: float, k2: float, spot: float,
                   max_loss: float = DEFAULT_MAX_LOSS,
                   r: float = DEFAULT_R, q: float = DEFAULT_Q) -> List[Candidate]:
    out = []
    for k3 in chain.strikes():
        if not (k1 < k3 < k2) or chain.rows[k3].put.price is None:
            continue
        st = build(chain, k1, k2, k3, spot, r, q)
        g = st.chain_greeks()
        src = "chain"
        if g is None:
            g, src = st.model_greeks(), "model"
        ml = st.max_loss()
        down, up = st.max_profit()
        out.append(Candidate(k3, st, st.debit() * MULTIPLIER, st.debit("natural") * MULTIPLIER,
                             ml, st.max_loss("natural"), down, up, st.breakevens(), g, src,
                             ml < max_loss, k3 <= spot))
    return out


def choose_third_leg(cands: Sequence[Candidate]) -> Optional[Candidate]:
    """条件を満たす候補のうち、ATM 以下で Net Vega が最大のもの。ATM以下に無ければ全体から。"""
    ok = [c for c in cands if c.feasible]
    pool = [c for c in ok if c.below_atm] or ok
    return max(pool, key=lambda c: (c.greeks.vega, c.k3)) if pool else None


# ---------------------------------------------------------------------------
# OPEX / マクロイベント分類（バックテスト用）
# ---------------------------------------------------------------------------

def third_friday(year: int, month: int) -> date:
    d = date(year, month, 15)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def opex_flags(entry: date, exit_: date,
               holidays: Iterable[date] = ()) -> Dict[str, bool]:
    """保有期間 (entry, exit] に対する OPEX フラグ。祝日の第3金曜は前営業日に繰り上げる。"""
    hol = set(holidays)
    flags = {"monthly_entry": False, "monthly_cross": False,
             "quarterly_entry": False, "quarterly_cross": False}
    y, m = entry.year, entry.month
    while True:
        tf = third_friday(y, m)
        while tf in hol:
            tf -= timedelta(days=1)
        if tf > exit_:
            break
        quarterly = m in (3, 6, 9, 12)
        if tf == entry:
            flags["monthly_entry"] = True
            flags["quarterly_entry"] |= quarterly
        elif entry < tf <= exit_:
            flags["monthly_cross"] = True
            flags["quarterly_cross"] |= quarterly
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return flags


def opex_regime(flags: Dict[str, bool]) -> str:
    """排他的な1ラベル。四半期が月次より優先、Entry が Cross より優先。"""
    for key, label in (("quarterly_entry", "Quarterly OPEX Entry"),
                       ("quarterly_cross", "Quarterly OPEX Cross"),
                       ("monthly_entry", "Monthly OPEX Entry"),
                       ("monthly_cross", "Monthly OPEX Cross")):
        if flags[key]:
            return label
    return "Normal"


def macro_flags(entry: date, exit_: date, events: Dict[str, Iterable[date]]) -> Dict[str, bool]:
    """events = {'FOMC': [...], 'CPI': [...], 'NFP': [...], 'PCE': [...]}。保有期間 (entry, exit] に含むか。"""
    return {name: any(entry < d <= exit_ for d in ds) for name, ds in events.items()}


# ---------------------------------------------------------------------------
# 評価指標
# ---------------------------------------------------------------------------

def metrics(pnls: Sequence[float], trim_top: float = 0.05) -> Dict[str, Optional[float]]:
    n = len(pnls)
    if n == 0:
        return {"n": 0}
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    mean = statistics.fmean(pnls)
    sd = statistics.stdev(pnls) if n > 1 else 0.0
    downside = math.sqrt(sum(min(x, 0.0) ** 2 for x in pnls) / n)
    eq = peak = dd = 0.0
    for x in pnls:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    cut = int(math.floor(n * trim_top))
    trimmed = sorted(pnls)[: n - cut] if cut else list(pnls)
    return {
        "n": n,
        "avg": mean,
        "median": statistics.median(pnls),
        "win_rate": len(wins) / n,
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
        "max_drawdown": dd,
        "avg_winner": statistics.fmean(wins) if wins else None,
        "avg_loser": statistics.fmean(losses) if losses else None,
        "expected_value": mean,
        "sharpe": mean / sd if sd else None,           # 1トレードあたり（年率化しない）
        "sortino": mean / downside if downside else None,
        "ev_ex_top5": statistics.fmean(trimmed) if trimmed else None,
        "trimmed_n": cut,
    }


# ---------------------------------------------------------------------------
# レポート
# ---------------------------------------------------------------------------

def _f(x: Optional[float], fmt: str = "{:,.2f}") -> str:
    return "—" if x is None else fmt.format(x)


def report(chain: Chain, spot: float, max_loss: float = DEFAULT_MAX_LOSS,
           r: float = DEFAULT_R, q: float = DEFAULT_Q, source: str = "") -> str:
    k1 = strike_for_call_delta(chain, 0.90)
    k2 = strike_for_call_delta(chain, 0.15)
    L: List[str] = []
    L.append(f"# レバポ 14DTE — {chain.expiration}（{chain.dte} DTE）")
    L.append("")
    if source:
        L.append(f"データ: {source}")
    L.append(f"Spot {spot:,.2f} / r {r:.2%} / q {q:.2%} / 最大損失上限 ${max_loss:,.0f}")
    L.append("")
    if k1 is None or k2 is None or not k1 < k2:
        L.append("①②を決定できませんでした（Delta 列または Put 価格が不足）。")
        return "\n".join(L)

    def leg_line(tag, k, side):
        row = chain.rows[k]
        return (f"| {tag} | {k:g} | {_f(row.call.delta, '{:+.3f}')} | {_f(row.put.delta, '{:+.3f}')} | "
                f"{_f(row.put.bid)} | {_f(row.put.ask)} | {_f(row.put.price)} | "
                f"{_f(row.put.iv, '{:.1%}')} | {side} |")

    L.append("## ①② 確定")
    L.append("")
    L.append("| 脚 | K | Call Δ | Put Δ | Put Bid | Put Ask | Mid | Put IV | 売買 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    L.append(leg_line("①", k1, "Long 1"))
    L.append(leg_line("②", k2, "Short 1"))
    L.append("")

    cands = scan_third_leg(chain, k1, k2, spot, max_loss, r, q)
    best = choose_third_leg(cands)
    L.append("## ③ 候補（K1 < K3 < K2 の全行使価格）")
    L.append("")
    L.append("| K3 | Put Δ | Net Debit $ | Max Loss $ (Mid) | Max Loss $ (Natural) | 下側利益 S→0 $ | "
             "上側 $ | 損益分岐 | Net Δ | Net Γ | Net Vega $/pt | Net Θ $/日 | 条件 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for c in cands:
        g = c.greeks
        mark = ("✅" if c.feasible else "✗") + (" ◀ 採用" if best and c.k3 == best.k3 else "")
        L.append(f"| {c.k3:g} | {_f(chain.rows[c.k3].put.delta, '{:+.3f}')} | {c.debit:,.0f} | "
                 f"{c.max_loss:,.0f} | {c.max_loss_natural:,.0f} | {c.profit_down:,.0f} | {c.profit_up:,.0f} | "
                 f"{', '.join(f'{b:,.2f}' for b in c.breakevens)} | {g.delta:+.1f} | {g.gamma:+.2f} | "
                 f"{g.vega:+.2f} | {g.theta:+.2f} | {mark} |")
    L.append("")
    if cands:
        L.append(f"Greeks の出所: {cands[0].greeks_source}（chain = チェーン掲載値、model = Mid逆算σのBS）。"
                 " Δ・Γ・Vega・Θ はいずれも 1セット×100 の $ 単位。")
        L.append("")
    if best is None:
        L.append(f"**Max Loss < ${max_loss:,.0f} を満たす ③ はこの満期にありません。**")
        return "\n".join(L)

    st = best.st
    L.append("## 採用ストラクチャ")
    L.append("")
    L.append(f"- ① Long  {k1:g}P / ② Short {k2:g}P / ③ Long {best.k3:g}P")
    L.append(f"- Net Debit ${best.debit:,.0f}（Natural ${best.debit_natural:,.0f}）")
    L.append(f"- Max Loss ${best.max_loss:,.0f}（Natural ${best.max_loss_natural:,.0f}）"
             f" — 満期 S が [{k1:g}, {best.k3:g}] の平坦区間で発生")
    L.append(f"- 下側最大利益（S→0）${best.profit_down:,.0f} / 上側（S≥{k2:g}）${best.profit_up:,.0f}")
    L.append(f"- 損益分岐 {', '.join(f'{b:,.2f}' for b in best.breakevens)}")
    g, mg = best.greeks, st.model_greeks()
    L.append(f"- Net Vega {g.vega:+.2f} $/vol pt（モデル {mg.vega:+.2f}）→ "
             + ("**Net Vega Long**" if g.vega > 0 else "**Net Vega Short（仮説と逆）**"))
    L.append(f"- Net Delta {g.delta:+.1f} / Gamma {g.gamma:+.2f} / Theta {g.theta:+.2f} $/日")
    for k, ext in st.short_extrinsic():
        L.append(f"- ② {k:g}P の時間価値 ${ext:.2f}/株"
                 + (" — **早期割当てリスク高**（SPY はアメリカン。XSP なら無し）" if ext < 0.10 else ""))
    L.append("")

    L.append("## シナリオ再評価（1セット $、エントリーからの損益）")
    for d in EXIT_DTES:
        if d > chain.dte:
            continue
        L.append("")
        L.append(f"### Exit {d} DTE" + ("（満期）" if d == 0 else "") + (" — 重要候補" if d == 7 else ""))
        L.append("")
        L.append("| Spot＼IV | " + " | ".join(f"{v:+g}pt" for v in IV_SHIFTS) + " |")
        L.append("|---|" + "---|" * len(IV_SHIFTS))
        for m in SPOT_MOVES:
            cells = [f"{st.pnl(m, v, d):,.0f}" for v in IV_SHIFTS]
            L.append(f"| {m:+.0%} | " + " | ".join(cells) + " |")

    L.append("")
    L.append("## P/L 分解（Exit 7 DTE、逐次再評価: 時間→価格→IV）")
    L.append("")
    L.append("| Spot | IV | Theta | Delta+Gamma | Vega | 合計 |")
    L.append("|---|---|---|---|---|---|")
    for m in (0.02, 0.0, -0.02, -0.05):
        for v in (0.0, 5.0, 10.0):
            a = st.attribution(m, v, min(7, chain.dte))
            L.append(f"| {m:+.0%} | {v:+g}pt | {a['theta']:,.0f} | {a['delta_gamma']:,.0f} | "
                     f"{a['vega']:,.0f} | {a['total']:,.0f} |")
    L.append("")
    L.append("注: 満期損益は厳密。途中時点はヨーロピアンBS・平行IVシフトの近似"
             "（スキュー変化・早期行使価値は含まない）。")
    return "\n".join(L)


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="レバポ（SPY/XSP 14DTE 3脚Put）: ①②の確定と③の総当たり")
    ap.add_argument("csv", help="オプションチェーンCSV（MF-Boost 等から書き出したもの）")
    ap.add_argument("--spot", type=float, help="原資産価格（省略時はCSVの spot 列かパリティから推定）")
    ap.add_argument("--dte", type=int, default=14)
    ap.add_argument("--max-loss", type=float, default=DEFAULT_MAX_LOSS)
    ap.add_argument("--r", type=float, default=DEFAULT_R)
    ap.add_argument("--q", type=float, default=DEFAULT_Q, help="配当利回り（XSP なら SPX の配当利回り）")
    ap.add_argument("--source", default="")
    ap.add_argument("-o", "--out")
    a = ap.parse_args(argv)

    chains, csv_spot = load_chains(a.csv)
    if not chains:
        print("チェーンを読み込めませんでした。")
        return 1
    chain = select_expiry(chains, a.dte)
    spot = a.spot or csv_spot or implied_spot(chain, a.r, a.q)
    if not spot:
        print("原資産価格が分かりません。--spot を指定してください。")
        return 1
    text = report(chain, spot, a.max_loss, a.r, a.q, a.source)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
