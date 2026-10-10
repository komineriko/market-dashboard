"""米国株オプションのスクリーニング。

通信はしない。板（チェーン）と日足を渡すと、4つの戦略の候補を返す。

  A. コール買い        — 上に伸びる前提。IVが割安なときだけ
  B. ブルプット        — 下がらない前提。クレジットを取り、損失は限定
  C. キャッシュセキュアードP売り — 下がったら買ってもいい水準で売る
  D. カバードコール    — 持っている株に上限をつけてプレミアムを取る

ギリシャ指標はフォワード基準（Black-76、r=0）で、IVは板の気配値から自前で
逆算する。データ提供元のIV列は流動性の薄い行で壊れることがあるので使わない。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

import sq_analytics as sa
import us_indicators as ui

MULTIPLIER = 100                # 米国株オプションは1枚=100株
TRADING_DAYS = 252

# --- 流動性の足切り ---------------------------------------------------------
MIN_OI = 200                    # 建玉。これ未満は気配が形だけのことが多い
MIN_PRICE = 0.10                # 20セント未満は手数料負けしやすい
MAX_SPREAD_PCT = 10.0           # (ask-bid)/mid。これより広いと往復で負ける
IV_SAMPLE_MAX_SPREAD_PCT = 60.0 # IVの基準値を取るときに許す広さ（売買はしない）
MIN_UNDERLYING = 10.0

# --- 期間 -------------------------------------------------------------------
MIN_DTE = 5                     # 数日〜2週間の取引を想定
MAX_DTE = 16                    # 週次の窓。2週間＋数日
# 月限（第3金曜）だけはこの距離まで通す。月限は直後に当たると次が35日先に
# なるので、週次の窓を広げる形だと入る日と入らない日ができてしまう。
# 建玉はたいてい月限がいちばん厚いので、常に1本は候補に入るようにする。
MONTHLY_MAX_DTE = 37

# --- 判定のしきい値 ---------------------------------------------------------
TH_IV_CHEAP = 0.85              # IV ÷ MADボラ。これ以下で「買い有利」
TH_IV_RICH = 1.00               # これ以上で「売り有利」
TH_IV_ABSURD = 3.0              # これを超えたら候補から外す（下の注記）
TH_OVEREXTENDED_ATR = 4.0       # 50日線からの乖離。これ以上は伸び切り
CC_MIN_DEV_ATR = 1.5            # カバコを出す乖離の下限
TH_GC_APPROACH_ATR = 0.5        # DIFがDEAの下でも、この差以内なら接近扱い
TH_MOMENTUM_OK = 3              # モメンタム改善スコア（5点満点）。強気側の下限
TH_MOMENTUM_WEAK = 1            # 弱気側の上限。これ以下なら勢いが落ちている
MIN_CREDIT_RATIO = 0.20         # クレジットスプレッドの受取 ÷ 最大損失

# --- カレンダースプレッド -----------------------------------------------
CAL_MAX_DEV_ATR = 1.5           # これ以上トレンドが出ていると動いて負ける
CAL_MIN_TERM_RATIO = 1.05       # 手前のIV ÷ 後ろのIV。手前が高いほど有利
CAL_BACK_MIN_DTE = 25           # 後ろの限月
CAL_BACK_MAX_DTE = 60


# ---------------------------------------------------------------------------
# 板
# ---------------------------------------------------------------------------

@dataclass
class Quote:
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    volume: int = 0
    oi: int = 0

    @property
    def mid(self) -> Optional[float]:
        """気配の仲値。両側が立っていないときは値を作らない。

        last に落とすと、建玉ゼロの行使価格に残っている何日も前の約定値を
        現在値として読んでしまう。実データで、V の 385P が bid/ask 無しの
        last=12.7（実勢は7程度）、T・VZ・TMUS では ATM IV が 170〜230% と
        出た。値段の安い銘柄ほど古い約定値のずれがIVに増幅されて効く。
        """
        if self.bid is not None and self.ask is not None and self.ask >= self.bid > 0:
            return (self.bid + self.ask) / 2.0
        return None

    @property
    def spread_pct(self) -> Optional[float]:
        m = self.mid
        if m and self.bid is not None and self.ask is not None and self.ask > self.bid > 0:
            return (self.ask - self.bid) / m * 100.0
        return None

    def tradable(self, side_is_buy: bool) -> bool:
        """買うなら ask、売るなら bid が立っていること。"""
        if self.oi < MIN_OI:
            return False
        m = self.mid
        if m is None or m < MIN_PRICE:
            return False
        sp = self.spread_pct
        if sp is None or sp > MAX_SPREAD_PCT:
            return False
        need = self.ask if side_is_buy else self.bid
        return bool(need and need > 0)


@dataclass
class StrikeQuote:
    strike: float
    call: Quote = field(default_factory=Quote)
    put: Quote = field(default_factory=Quote)


@dataclass
class Expiry:
    expiry: str                      # YYYY-MM-DD
    rows: List[StrikeQuote]

    def dte(self, asof: date) -> int:
        return (date.fromisoformat(self.expiry) - asof).days

    def t(self, asof: date) -> float:
        """年換算の残存。満期当日でもゼロ割りしないよう半日分を下限にする。"""
        return max(self.dte(asof), 0.5) / 365.0

    def row(self, strike: float) -> Optional[StrikeQuote]:
        for r in self.rows:
            if abs(r.strike - strike) < 1e-6:
                return r
        return None


@dataclass
class Underlying:
    symbol: str
    bars: List[ui.Bar]
    expiries: List[Expiry]
    earnings: Optional[str] = None          # 次回決算日 YYYY-MM-DD
    held: bool = False                      # 現物を持っているか
    ref_price: Optional[float] = None       # 板から逆算した現値（下の注記）
    back_expiries: List[Expiry] = field(default_factory=list)   # カレンダーの後ろ足

    @property
    def spot(self) -> float:
        """いまの株価として使う値。

        日足の配信は引けから数時間遅れることがあり、板のほうが1営業日新しい
        ことがある（実測: 引けの4時間45分後でも前日分までしか来ていなかった）。
        そのまま日足の終値を現値として使うと、板が織り込んでいる株価と
        ずれたままデルタも損益分岐も出してしまう。
        パリティから逆算したフォワードは実際の直近終値をほぼ復元するので、
        取れるときはそちらを優先する。
        """
        return self.ref_price if self.ref_price else self.bars[-1].close

    @property
    def last_bar_close(self) -> float:
        return self.bars[-1].close


# ---------------------------------------------------------------------------
# フォワードと IV
# ---------------------------------------------------------------------------

def implied_forward(exp: Expiry, spot: float, asof: date) -> float:
    """プット・コール・パリティ F = K + C − P のATM近傍中央値。

    金利・配当を別途引っ張らずに済み、実際に建てられる気配から出るので
    理論値よりそのときの板に忠実になる。気配が揃わなければ現値を返す。
    """
    cands: List[float] = []
    near = sorted(exp.rows, key=lambda r: abs(r.strike - spot))[:5]
    for r in near:
        c, p = r.call.mid, r.put.mid
        if c and p and r.call.tradable(True) and r.put.tradable(True):
            cands.append(r.strike + c - p)
    if not cands:
        return spot
    cands.sort()
    mid = len(cands) // 2
    f = cands[mid] if len(cands) % 2 else (cands[mid - 1] + cands[mid]) / 2.0
    # パリティが現値から大きく外れるのは気配の崩れ。採らない。
    return f if abs(f / spot - 1.0) < 0.05 else spot


def iv_of(q: Quote, forward: float, strike: float, t: float, is_call: bool) -> Optional[float]:
    m = q.mid
    if m is None:
        return None
    return sa.implied_vol(m, forward, strike, t, is_call)


def atm_iv(exp: Expiry, forward: float, t: float) -> Optional[float]:
    """ATM近傍のIV。CALL/PUT両方から取って中央値にする。"""
    vals: List[float] = []
    for r in sorted(exp.rows, key=lambda r: abs(r.strike - forward))[:4]:
        for q, is_call in ((r.call, True), (r.put, False)):
            sp = q.spread_pct
            # 売買はしないので足切りは緩くてよいが、極端に広い気配は
            # 仲値そのものが当てにならないので基準値には使わない。
            if sp is None or sp > IV_SAMPLE_MAX_SPREAD_PCT:
                continue
            v = iv_of(q, forward, r.strike, t, is_call)
            if v and 0.01 < v < 4.0:
                vals.append(v)
    if not vals:
        return None
    vals.sort()
    mid = len(vals) // 2
    v = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0
    return v * 100.0


# ---------------------------------------------------------------------------
# GEX とウォール
# ---------------------------------------------------------------------------

@dataclass
class Gex:
    call_wall: Optional[float]
    call_wall_up: Optional[float]   # 現値より上だけで見た最も厚い行使価格
    put_wall_down: Optional[float]  # 現値より下だけで見た最も厚い行使価格
    put_wall: Optional[float]
    call_oi_peak: Optional[float]
    put_oi_peak: Optional[float]
    flip: Optional[float]            # 現値より下で合算GEXが符号を変える水準
    at_spot: float                   # 現値での合算GEX（ドル）
    by_strike: List[Tuple[float, float, float]]   # (strike, call GEX, put GEX)


def _strike_ivs(expiries: Sequence[Expiry], spot: float, asof: date
                ) -> List[Tuple[float, float, float, bool, int]]:
    """(strike, t, iv, is_call, oi) の並び。GEXの材料。"""
    out = []
    for exp in expiries:
        t = exp.t(asof)
        f = implied_forward(exp, spot, asof)
        for r in exp.rows:
            for q, is_call in ((r.call, True), (r.put, False)):
                if q.oi <= 0:
                    continue
                v = iv_of(q, f, r.strike, t, is_call)
                if v and 0.01 < v < 4.0:
                    out.append((r.strike, t, v, is_call, q.oi))
    return out


def gex_at(material: Sequence[Tuple[float, float, float, bool, int]], level: float) -> float:
    """株価が level のときの合算GEX（1%動いたときのディーラーのヘッジ量、ドル）。

    ディーラー = コール買い持ち／プット売り持ち、という慣例の仮定を置いている。
    実際の建玉の向きは公開されていないので、符号は目安でしかない。
    """
    total = 0.0
    for k, t, v, is_call, oi in material:
        g = sa.bs_gamma(level, k, t, v)
        total += (g if is_call else -g) * oi
    return total * MULTIPLIER * level * level * 0.01


def build_gex(expiries: Sequence[Expiry], spot: float, asof: date,
              span: float = 0.25, steps: int = 60) -> Gex:
    material = _strike_ivs(expiries, spot, asof)
    if not material:
        return Gex(None, None, None, None, None, None, None, 0.0, [])

    per: Dict[float, List[float]] = {}
    oi: Dict[float, List[int]] = {}
    for k, t, v, is_call, n in material:
        g = sa.bs_gamma(spot, k, t, v) * n * MULTIPLIER * spot * spot * 0.01
        slot = per.setdefault(k, [0.0, 0.0])
        cnt = oi.setdefault(k, [0, 0])
        if is_call:
            slot[0] += g
            cnt[0] += n
        else:
            slot[1] += g
            cnt[1] += n

    by_strike = sorted((k, v[0], -v[1]) for k, v in per.items())
    call_wall = max(by_strike, key=lambda x: x[1])[0] if by_strike else None
    above = [x for x in by_strike if x[0] > spot]
    call_wall_up = max(above, key=lambda x: x[1])[0] if above else None
    below = [x for x in by_strike if x[0] < spot]
    put_wall_down = min(below, key=lambda x: x[2])[0] if below else None
    put_wall = min(by_strike, key=lambda x: x[2])[0] if by_strike else None
    call_oi_peak = max(oi.items(), key=lambda kv: kv[1][0])[0] if oi else None
    put_oi_peak = max(oi.items(), key=lambda kv: kv[1][1])[0] if oi else None

    at_spot = gex_at(material, spot)
    flip = None
    lo = spot * (1 - span)
    prev_level, prev_val = spot, at_spot
    for i in range(1, steps + 1):
        level = spot - (spot - lo) * i / steps
        val = gex_at(material, level)
        if (prev_val > 0) != (val > 0):
            # 線形に内挿して交点を出す
            if val != prev_val:
                flip = prev_level + (level - prev_level) * (prev_val / (prev_val - val))
            else:
                flip = level
            break
        prev_level, prev_val = level, val

    return Gex(call_wall, call_wall_up, put_wall_down, put_wall,
               call_oi_peak, put_oi_peak, flip, at_spot, by_strike)


# ---------------------------------------------------------------------------
# 銘柄ごとのテクニカル
# ---------------------------------------------------------------------------

@dataclass
class Technicals:
    symbol: str
    spot: float
    sma50: Optional[float]
    atr: Optional[float]
    dev_pct: Optional[float]         # 50日線からの乖離（%）
    dev_atr: Optional[float]         # 同（ATR換算）
    rsi: Optional[float]
    macd: Optional[ui.Macd]
    momentum: Optional[ui.Momentum]
    mad_vol: Optional[float]
    hv20ex: Optional[float]

    @property
    def gc_state(self) -> str:
        m = self.macd
        if not m:
            return "—"
        if m.gc_bars_ago is not None:
            zero = "ゼロ上" if m.gc_above_zero else "ゼロ下"
            return f"GC {m.gc_bars_ago}本前・{zero}" if m.gc_bars_ago else f"GC 直近・{zero}"
        if m.gap_atr is not None and -TH_GC_APPROACH_ATR <= m.gap_atr < 0 \
                and m.hist_prev is not None and m.hist > m.hist_prev:
            return f"GC接近 {m.gap_atr:+.2f}"
        if m.dead_cross_recent:
            return "DC 直近"
        return "DIF>DEA" if m.hist > 0 else "DIF<DEA"

    @property
    def signal(self) -> Optional[str]:
        """MACDが示している向き。"up" / "down" / None のどれか1つ。

        交差済みならその向き。まだ交差していなければ、近づいている向き。
        交差を接近より優先するのが肝心で、これを分けて持つと
        「上抜けた直後にヒストが細り始めた」銘柄が上にも下にも出てしまう
        （実データで NVDA が GC 13本前なのにプット買いに並んだ）。
        """
        m = self.macd
        if not m:
            return None
        if m.gc_bars_ago is not None:
            return "up"
        if m.dc_bars_ago is not None:
            return "down"
        if m.gap_atr is None or m.hist_prev is None:
            return None
        if -TH_GC_APPROACH_ATR <= m.gap_atr < 0 and m.hist > m.hist_prev:
            return "up"
        if 0 < m.gap_atr <= TH_GC_APPROACH_ATR and m.hist < m.hist_prev:
            return "down"
        return None

    @property
    def gc_ok(self) -> bool:
        """上抜け済み、または上抜け目前。"""
        return self.signal == "up"

    @property
    def dc_state(self) -> str:
        m = self.macd
        if not m:
            return "—"
        if m.dc_bars_ago is not None:
            zero = "ゼロ下" if m.dc_below_zero else "ゼロ上"
            return f"DC {m.dc_bars_ago}本前・{zero}" if m.dc_bars_ago else f"DC 直近・{zero}"
        if m.gap_atr is not None and 0 < m.gap_atr <= TH_GC_APPROACH_ATR \
                and m.hist_prev is not None and m.hist < m.hist_prev:
            return f"DC接近 {m.gap_atr:+.2f}"
        return "DIF>DEA" if m.hist > 0 else "DIF<DEA"

    @property
    def dc_ok(self) -> bool:
        """下抜け済み、または下抜け目前。gc_ok の鏡像。"""
        return self.signal == "down"

    @property
    def macd_state(self) -> str:
        """一覧に出す1つの文字列。交差している側の説明を選ぶ。"""
        m = self.macd
        if not m:
            return "—"
        if m.gc_bars_ago is not None:
            return self.gc_state
        if m.dc_bars_ago is not None:
            return self.dc_state
        # どちらも無ければ、近づいているほうを出す
        return self.gc_state if m.hist > 0 else self.dc_state

    @property
    def hist_bottomed(self) -> bool:
        """MACDヒストの谷越え。下げの勢いは残るが細り始めている。"""
        m = self.macd
        return bool(m and m.hist < 0 and m.hist_prev is not None
                    and m.hist > m.hist_prev)

    @property
    def hist_rolled_over(self) -> bool:
        """MACDヒストの山越え。勢いは正だが細り始めている。"""
        m = self.macd
        return bool(m and m.hist > 0 and m.hist_prev is not None and m.hist < m.hist_prev)


def technical_gate(tech: Technicals) -> bool:
    """板を取りに行く価値があるか。

    4区分のうち、テクニカルだけで判定できる条件の論理和。どれも通らない銘柄は
    板を取っても候補にならないので、取得前にここで落とす。銘柄リストが
    数百本になると、板と決算の取得が全体の時間のほとんどを占めるため。

    IVの条件（IV/MAD）は板が無いと判定できないので、ここには入れない。
    """
    if tech.dev_atr is None or tech.momentum is None:
        return False
    long_call = tech.dev_atr < TH_OVEREXTENDED_ATR and tech.gc_ok
    bull_put = tech.momentum.score >= TH_MOMENTUM_OK and tech.gc_ok
    csp = tech.momentum.score >= TH_MOMENTUM_OK
    covered = tech.hist_rolled_over or tech.dev_atr >= CC_MIN_DEV_ATR
    long_put = tech.dev_atr > -TH_OVEREXTENDED_ATR and tech.dc_ok
    bear_call = tech.momentum.score <= TH_MOMENTUM_WEAK and tech.dc_ok
    calendar = abs(tech.dev_atr) <= CAL_MAX_DEV_ATR
    return bool(long_call or bull_put or csp or covered
                or long_put or bear_call or calendar)


def reference_price(u: Underlying, asof: date) -> float:
    """板から逆算した現値。取れなければ日足の終値。

    キャリーの分だけフォワードは現値より上だが、2週間で 0.1% 程度しかない。
    日足が1営業日遅れたときのずれ（実測 0.5%）よりはるかに小さい。
    """
    exps = usable_expiries(u, asof) or [e for e in u.expiries if e.dte(asof) >= 0]
    close = u.bars[-1].close
    if not exps:
        return close
    return implied_forward(sorted(exps, key=lambda e: e.dte(asof))[0], close, asof)


def technicals(u: Underlying) -> Technicals:
    closes = [b.close for b in u.bars]
    a = ui.atr(u.bars)
    s50 = ui.sma(closes, 50)
    m = ui.macd(closes, a)
    dev_pct = (closes[-1] / s50 - 1.0) * 100.0 if s50 else None
    dev_atr = (closes[-1] - s50) / a if (s50 and a) else None
    return Technicals(
        symbol=u.symbol, spot=closes[-1], sma50=s50, atr=a,
        dev_pct=dev_pct, dev_atr=dev_atr, rsi=ui.rsi(closes), macd=m,
        momentum=ui.momentum_score(u.bars, m),
        mad_vol=ui.mad_vol(closes), hv20ex=ui.hv_excl_gap(closes),
    )


def iv_ratio(iv: Optional[float], tech: Technicals) -> Optional[float]:
    if iv is None or not tech.mad_vol:
        return None
    return iv / tech.mad_vol


def iv_is_sane(ratio: Optional[float]) -> bool:
    """IVが実現ボラに対して極端すぎないか。

    実現ボラの3倍を超えるIVは、まず「取り逃がされているイベント」か
    気配の異常を疑うべきで、うまみではない。決算は別途外しているので、
    それでも残る極端な値は説明がつかない。

    これを入れないと、プレミアムを受け取る側のスクリーニングで
    いちばん怪しい銘柄がいちばん上に来る。年率利回りで並べている以上、
    異常な気配ほど上位に押し上げられてしまうため。
    （実データで、実現ボラの8.5倍のIVを持つ銘柄がP売りの1位に出た）
    """
    return ratio is not None and ratio <= TH_IV_ABSURD


# ---------------------------------------------------------------------------
# 候補
# ---------------------------------------------------------------------------

@dataclass
class Leg:
    action: str              # "買" / "売"
    kind: str                # "C" / "P"
    strike: float
    price: float             # MID
    bid: Optional[float]
    ask: Optional[float]
    delta: float
    iv: float                # %
    oi: int
    spread_pct: Optional[float]


@dataclass
class Candidate:
    strategy: str
    symbol: str
    expiry: str
    dte: int
    spot: float
    legs: List[Leg]
    metrics: Dict[str, Optional[float]]
    notes: List[str] = field(default_factory=list)
    tech: Optional[Technicals] = None
    gex: Optional[Gex] = None
    rank: float = 0.0


def third_friday(year: int, month: int) -> date:
    """その月の第3金曜。米国オプションの月限。"""
    first = date(year, month, 1)
    first_friday = first + timedelta(days=(4 - first.weekday()) % 7)
    return first_friday + timedelta(days=14)


def is_monthly(d: date) -> bool:
    return d == third_friday(d.year, d.month)


def in_screen_window(dte: int, expiry: date) -> bool:
    """候補として採る満期か。週次は MAX_DTE まで、月限は MONTHLY_MAX_DTE まで。"""
    if dte < MIN_DTE:
        return False
    if dte <= MAX_DTE:
        return True
    return dte <= MONTHLY_MAX_DTE and is_monthly(expiry)


def in_fetch_window(dte: int, expiry: date) -> bool:
    """板を取りに行く満期か。

    MIN_DTE より手前も取る。ウォールを「その満期までの全満期を合算」で
    出すので、当週の建玉を落とすと壁が実勢より薄く出るため。
    """
    if dte < 0:
        return False
    if dte <= MAX_DTE:
        return True
    return dte <= MONTHLY_MAX_DTE and is_monthly(expiry)


def _crosses_earnings(u: Underlying, asof: date, expiry: str) -> bool:
    if not u.earnings:
        return False
    try:
        e = date.fromisoformat(u.earnings)
    except ValueError:
        return False
    return asof <= e <= date.fromisoformat(expiry)


def usable_expiries(u: Underlying, asof: date) -> List[Expiry]:
    out = []
    for e in u.expiries:
        if not in_screen_window(e.dte(asof), date.fromisoformat(e.expiry)):
            continue
        if _crosses_earnings(u, asof, e.expiry):
            continue
        out.append(e)
    return sorted(out, key=lambda e: e.dte(asof))


def _leg(action: str, kind: str, r: StrikeQuote, q: Quote, forward: float,
         t: float) -> Optional[Leg]:
    is_call = kind == "C"
    v = iv_of(q, forward, r.strike, t, is_call)
    m = q.mid
    if v is None or m is None:
        return None
    return Leg(action=action, kind=kind, strike=r.strike, price=m,
               bid=q.bid, ask=q.ask,
               delta=sa.bs_delta(forward, r.strike, t, v, is_call),
               iv=v * 100.0, oi=q.oi, spread_pct=q.spread_pct)


def _value_if_spot(strike: float, t: float, iv_pct: float, is_call: bool,
                   level: float) -> float:
    """株価が level に届いたときの理論値。IVと残存日数は今のままと仮定する。

    実際には上昇すればIVは下がり、日数が経てば時間価値も減るので、
    この数字は上限寄りの目安でしかない。
    """
    return sa.bs_price(level, strike, t, iv_pct / 100.0, is_call)


# ---------------------------------------------------------------------------
# A. コール買い
# ---------------------------------------------------------------------------

CALL_DELTA = (0.35, 0.60)
PUT_DELTA = (0.35, 0.60)                # 絶対値
BULLPUT_SHORT_DELTA = (0.18, 0.35)      # 絶対値
BEARCALL_SHORT_DELTA = (0.18, 0.35)
CSP_DELTA = (0.12, 0.30)
CC_DELTA = (0.15, 0.35)


def _wall_values(leg: Leg, t: float, gex: Optional[Gex], spot: float
                 ) -> Dict[str, Optional[float]]:
    """CALL WALL / PUT WALL に届いたときの理論値と伸びしろ。"""
    out: Dict[str, Optional[float]] = {
        "at_call_wall": None, "upside_pct": None, "at_put_wall": None,
        "downside_pct": None, "beyond_wall": None, "target": None,
    }
    if not gex:
        return out
    is_call = leg.kind == "C"
    # ウォールが現値より下にあるときは、そこへ「届く」話にならない。
    # 上側だけで見て最も厚い行使価格を目標に置き換え、置き換えたことを残す。
    target = gex.call_wall
    if target is not None and target <= spot:
        target = gex.call_wall_up
        out["beyond_wall"] = 1.0
    if target is not None:
        v = _value_if_spot(leg.strike, t, leg.iv, is_call, target)
        out["target"] = target
        out["at_call_wall"] = v
        out["upside_pct"] = (v / leg.price - 1.0) * 100.0 if leg.price else None
    if gex.put_wall is not None:
        v = _value_if_spot(leg.strike, t, leg.iv, is_call, gex.put_wall)
        out["at_put_wall"] = v
        out["downside_pct"] = (v / leg.price - 1.0) * 100.0 if leg.price else None
    return out


def screen_long_call(u: Underlying, asof: date, tech: Technicals,
                     gex_for: Dict[str, Gex], per_symbol: int = 2) -> List[Candidate]:
    """上に伸びる前提。IVが割安なときだけ買う。

    IVが高いまま買うと、方向が当たってもIVの低下で負ける。元レポートが
    IV/MAD ≤ 0.85 を条件にしているのと同じ理由で、ここも割安を必須にする。
    """
    if tech.dev_atr is None or tech.dev_atr >= TH_OVEREXTENDED_ATR:
        return []
    if not tech.gc_ok:
        return []

    out: List[Candidate] = []
    for exp in usable_expiries(u, asof):
        t, dte = exp.t(asof), exp.dte(asof)
        f = implied_forward(exp, u.spot, asof)
        iv = atm_iv(exp, f, t)
        ratio = iv_ratio(iv, tech)
        if ratio is None or ratio > TH_IV_CHEAP or not iv_is_sane(ratio):
            continue
        gex = gex_for.get(exp.expiry)
        for r in exp.rows:
            if not r.call.tradable(True):
                continue
            leg = _leg("買", "C", r, r.call, f, t)
            if leg is None or not (CALL_DELTA[0] <= leg.delta <= CALL_DELTA[1]):
                continue
            w = _wall_values(leg, t, gex, u.spot)
            be = leg.strike + leg.price
            out.append(Candidate(
                strategy="long_call", symbol=u.symbol, expiry=exp.expiry, dte=dte,
                spot=u.spot, legs=[leg], tech=tech, gex=gex,
                metrics={
                    "cost": leg.price * MULTIPLIER,
                    "breakeven": be,
                    "breakeven_pct": (be / u.spot - 1.0) * 100.0,
                    "atm_iv": iv, "iv_ratio": ratio,
                    **w,
                },
                rank=(w["upside_pct"] or -999.0),
            ))
    out.sort(key=lambda c: c.rank, reverse=True)
    return out[:per_symbol]


# ---------------------------------------------------------------------------
# B. ブルプット・スプレッド
# ---------------------------------------------------------------------------

def screen_bull_put(u: Underlying, asof: date, tech: Technicals,
                    gex_for: Dict[str, Gex], per_symbol: int = 1) -> List[Candidate]:
    """下がらなければ勝ち。クレジットを取り、損失は幅で止める。

    裸のプット売りと違い最大損失が決まっているので、同じ強気でも
    下に大きく飛んだときの傷が浅い。そのぶん受け取りは小さい。
    """
    if not tech.momentum or tech.momentum.score < TH_MOMENTUM_OK:
        return []
    if not tech.gc_ok:
        return []

    out: List[Candidate] = []
    for exp in usable_expiries(u, asof):
        t, dte = exp.t(asof), exp.dte(asof)
        f = implied_forward(exp, u.spot, asof)
        iv = atm_iv(exp, f, t)
        ratio = iv_ratio(iv, tech)
        if ratio is None or ratio < TH_IV_RICH or not iv_is_sane(ratio):
            continue
        gex = gex_for.get(exp.expiry)
        strikes = sorted(r.strike for r in exp.rows)
        for r in exp.rows:
            if r.strike >= u.spot or not r.put.tradable(False):
                continue
            short = _leg("売", "P", r, r.put, f, t)
            if short is None:
                continue
            if not (BULLPUT_SHORT_DELTA[0] <= abs(short.delta) <= BULLPUT_SHORT_DELTA[1]):
                continue
            lower = [k for k in strikes if k < r.strike]
            for k in sorted(lower, reverse=True)[:3]:
                rl = exp.row(k)
                if rl is None or not rl.put.tradable(True):
                    continue
                long_leg = _leg("買", "P", rl, rl.put, f, t)
                if long_leg is None:
                    continue
                width = short.strike - long_leg.strike
                credit = short.price - long_leg.price
                max_loss = width - credit
                if credit <= 0 or max_loss <= 0:
                    continue
                cr = credit / max_loss
                if cr < MIN_CREDIT_RATIO:
                    continue
                be = short.strike - credit
                out.append(Candidate(
                    strategy="bull_put", symbol=u.symbol, expiry=exp.expiry, dte=dte,
                    spot=u.spot, legs=[short, long_leg], tech=tech, gex=gex,
                    metrics={
                        "credit": credit * MULTIPLIER,
                        "width": width,
                        "max_loss": max_loss * MULTIPLIER,
                        "credit_ratio": cr,
                        "breakeven": be,
                        "cushion_pct": (u.spot - be) / u.spot * 100.0,
                        "pop": (1.0 - abs(short.delta)) * 100.0,
                        "atm_iv": iv, "iv_ratio": ratio,
                        "put_wall": gex.put_wall if gex else None,
                    },
                    rank=cr,
                ))
    out.sort(key=lambda c: c.rank, reverse=True)
    return out[:per_symbol]


# ---------------------------------------------------------------------------
# C. キャッシュセキュアードP売り
# ---------------------------------------------------------------------------

def screen_cash_secured_put(u: Underlying, asof: date, tech: Technicals,
                            gex_for: Dict[str, Gex], per_symbol: int = 1
                            ) -> List[Candidate]:
    """下がったら買ってもいい水準でプットを売る。

    割り当てられたら現物を持つことになるので、実質取得単価（行使価格 − 受取）が
    自分で買ってもいい値段かどうかが判断のすべて。必要資金も併記する。
    """
    if not tech.momentum or tech.momentum.score < TH_MOMENTUM_OK:
        return []

    out: List[Candidate] = []
    for exp in usable_expiries(u, asof):
        t, dte = exp.t(asof), exp.dte(asof)
        f = implied_forward(exp, u.spot, asof)
        iv = atm_iv(exp, f, t)
        ratio = iv_ratio(iv, tech)
        if ratio is None or ratio < TH_IV_RICH or not iv_is_sane(ratio):
            continue
        gex = gex_for.get(exp.expiry)
        for r in exp.rows:
            if r.strike >= u.spot or not r.put.tradable(False):
                continue
            leg = _leg("売", "P", r, r.put, f, t)
            if leg is None or not (CSP_DELTA[0] <= abs(leg.delta) <= CSP_DELTA[1]):
                continue
            cost = leg.strike - leg.price
            out.append(Candidate(
                strategy="csp", symbol=u.symbol, expiry=exp.expiry, dte=dte,
                spot=u.spot, legs=[leg], tech=tech, gex=gex,
                metrics={
                    "premium": leg.price * MULTIPLIER,
                    "effective_cost": cost,
                    "cushion_pct": (u.spot - cost) / u.spot * 100.0,
                    "annual_pct": leg.price / leg.strike * (365.0 / max(dte, 1)) * 100.0,
                    "capital": leg.strike * MULTIPLIER,
                    "pop": (1.0 - abs(leg.delta)) * 100.0,
                    "atm_iv": iv, "iv_ratio": ratio,
                    "put_wall": gex.put_wall if gex else None,
                },
                rank=leg.price / leg.strike * (365.0 / max(dte, 1)) * 100.0,
            ))
    out.sort(key=lambda c: c.rank, reverse=True)
    return out[:per_symbol]


# ---------------------------------------------------------------------------
# D. カバードコール
# ---------------------------------------------------------------------------

def screen_covered_call(u: Underlying, asof: date, tech: Technicals,
                        gex_for: Dict[str, Gex], per_symbol: int = 1
                        ) -> List[Candidate]:
    """持っている株に上限をつけてプレミアムを取る。

    伸び切ったところか勢いが細り始めたところで売る。上に抜けたら株は持って
    いかれるので、「その値段で売ってもいいか」が条件になる。
    """
    if not (tech.hist_rolled_over
            or (tech.dev_atr is not None and tech.dev_atr >= CC_MIN_DEV_ATR)):
        return []

    out: List[Candidate] = []
    for exp in usable_expiries(u, asof):
        t, dte = exp.t(asof), exp.dte(asof)
        f = implied_forward(exp, u.spot, asof)
        iv = atm_iv(exp, f, t)
        ratio = iv_ratio(iv, tech)
        if ratio is None or ratio < TH_IV_RICH or not iv_is_sane(ratio):
            continue
        gex = gex_for.get(exp.expiry)
        for r in exp.rows:
            if r.strike <= u.spot or not r.call.tradable(False):
                continue
            leg = _leg("売", "C", r, r.call, f, t)
            if leg is None or not (CC_DELTA[0] <= leg.delta <= CC_DELTA[1]):
                continue
            called = (leg.strike - u.spot + leg.price) / u.spot * 100.0
            out.append(Candidate(
                strategy="covered_call", symbol=u.symbol, expiry=exp.expiry, dte=dte,
                spot=u.spot, legs=[leg], tech=tech, gex=gex,
                metrics={
                    "premium": leg.price * MULTIPLIER,
                    "annual_pct": leg.price / u.spot * (365.0 / max(dte, 1)) * 100.0,
                    "called_return_pct": called,
                    "downside_buffer_pct": leg.price / u.spot * 100.0,
                    "atm_iv": iv, "iv_ratio": ratio,
                    "call_wall": gex.call_wall if gex else None,
                    "above_call_wall": (
                        None if not gex or gex.call_wall is None
                        else leg.strike >= gex.call_wall),
                },
                rank=called,
            ))
    out.sort(key=lambda c: c.rank, reverse=True)
    return out[:per_symbol]


# ---------------------------------------------------------------------------
# E. プット買い
# ---------------------------------------------------------------------------

def _put_target(leg: Leg, t: float, gex: Optional[Gex], spot: float
                ) -> Dict[str, Optional[float]]:
    """PUT WALL に届いたときの理論値。コール買いの _wall_values の鏡像。"""
    out: Dict[str, Optional[float]] = {
        "target": None, "at_call_wall": None, "upside_pct": None,
        "beyond_wall": None,
    }
    if not gex:
        return out
    target = gex.put_wall
    if target is not None and target >= spot:
        # ウォールが現値より上にあると「下に届く」話にならない。
        target = gex.put_wall_down
        out["beyond_wall"] = 1.0
    if target is not None:
        v = _value_if_spot(leg.strike, t, leg.iv, False, target)
        out["target"] = target
        out["at_call_wall"] = v
        out["upside_pct"] = (v / leg.price - 1.0) * 100.0 if leg.price else None
    return out


def screen_long_put(u: Underlying, asof: date, tech: Technicals,
                    gex_for: Dict[str, Gex], per_symbol: int = 2) -> List[Candidate]:
    """下に向かう前提。IVが割安なときだけ買う。コール買いの鏡像。

    下げは速いぶんIVが上がりやすく、方向が当たればIVでも稼げる。
    それでも入口でIVが高いと、下げても思ったほど増えない。
    """
    if tech.dev_atr is None or tech.dev_atr <= -TH_OVEREXTENDED_ATR:
        return []
    if not tech.dc_ok:
        return []

    out: List[Candidate] = []
    for exp in usable_expiries(u, asof):
        t, dte = exp.t(asof), exp.dte(asof)
        f = implied_forward(exp, u.spot, asof)
        iv = atm_iv(exp, f, t)
        ratio = iv_ratio(iv, tech)
        if ratio is None or ratio > TH_IV_CHEAP or not iv_is_sane(ratio):
            continue
        gex = gex_for.get(exp.expiry)
        for r in exp.rows:
            if not r.put.tradable(True):
                continue
            leg = _leg("買", "P", r, r.put, f, t)
            if leg is None or not (PUT_DELTA[0] <= abs(leg.delta) <= PUT_DELTA[1]):
                continue
            w = _put_target(leg, t, gex, u.spot)
            be = leg.strike - leg.price
            out.append(Candidate(
                strategy="long_put", symbol=u.symbol, expiry=exp.expiry, dte=dte,
                spot=u.spot, legs=[leg], tech=tech, gex=gex,
                metrics={
                    "cost": leg.price * MULTIPLIER,
                    "breakeven": be,
                    "breakeven_pct": (be / u.spot - 1.0) * 100.0,
                    "atm_iv": iv, "iv_ratio": ratio,
                    **w,
                },
                rank=(w["upside_pct"] or -999.0),
            ))
    out.sort(key=lambda c: c.rank, reverse=True)
    return out[:per_symbol]


# ---------------------------------------------------------------------------
# F. ベアコール・スプレッド
# ---------------------------------------------------------------------------

def screen_bear_call(u: Underlying, asof: date, tech: Technicals,
                     gex_for: Dict[str, Gex], per_symbol: int = 1) -> List[Candidate]:
    """上がらなければ勝ち。ブルプットの鏡像。

    上に抜けたときの損失が幅で止まるので、裸のコール売りと違って
    青天井にならない。
    """
    if not tech.momentum or tech.momentum.score > TH_MOMENTUM_WEAK:
        return []
    if not tech.dc_ok:
        return []

    out: List[Candidate] = []
    for exp in usable_expiries(u, asof):
        t, dte = exp.t(asof), exp.dte(asof)
        f = implied_forward(exp, u.spot, asof)
        iv = atm_iv(exp, f, t)
        ratio = iv_ratio(iv, tech)
        if ratio is None or ratio < TH_IV_RICH or not iv_is_sane(ratio):
            continue
        gex = gex_for.get(exp.expiry)
        strikes = sorted(r.strike for r in exp.rows)
        for r in exp.rows:
            if r.strike <= u.spot or not r.call.tradable(False):
                continue
            short = _leg("売", "C", r, r.call, f, t)
            if short is None:
                continue
            if not (BEARCALL_SHORT_DELTA[0] <= short.delta <= BEARCALL_SHORT_DELTA[1]):
                continue
            for k in sorted([k for k in strikes if k > r.strike])[:3]:
                rl = exp.row(k)
                if rl is None or not rl.call.tradable(True):
                    continue
                long_leg = _leg("買", "C", rl, rl.call, f, t)
                if long_leg is None:
                    continue
                width = long_leg.strike - short.strike
                credit = short.price - long_leg.price
                max_loss = width - credit
                if credit <= 0 or max_loss <= 0:
                    continue
                cr = credit / max_loss
                if cr < MIN_CREDIT_RATIO:
                    continue
                be = short.strike + credit
                out.append(Candidate(
                    strategy="bear_call", symbol=u.symbol, expiry=exp.expiry,
                    dte=dte, spot=u.spot, legs=[short, long_leg],
                    tech=tech, gex=gex,
                    metrics={
                        "credit": credit * MULTIPLIER,
                        "width": width,
                        "max_loss": max_loss * MULTIPLIER,
                        "credit_ratio": cr,
                        "breakeven": be,
                        "cushion_pct": (be - u.spot) / u.spot * 100.0,
                        "pop": (1.0 - abs(short.delta)) * 100.0,
                        "atm_iv": iv, "iv_ratio": ratio,
                        "call_wall": gex.call_wall if gex else None,
                    },
                    rank=cr,
                ))
    out.sort(key=lambda c: c.rank, reverse=True)
    return out[:per_symbol]


# ---------------------------------------------------------------------------
# G. カレンダースプレッド
# ---------------------------------------------------------------------------

def usable_back_expiries(u: Underlying, asof: date) -> List[Expiry]:
    """カレンダーの後ろ足。決算をまたぐことは許すが、呼び出し側で印をつける。"""
    out = [e for e in u.back_expiries
           if CAL_BACK_MIN_DTE <= e.dte(asof) <= CAL_BACK_MAX_DTE]
    return sorted(out, key=lambda e: e.dte(asof))


def screen_calendar(u: Underlying, asof: date, tech: Technicals,
                    gex_for: Dict[str, Gex], per_symbol: int = 1) -> List[Candidate]:
    """手前を売って後ろを買う。動かなければ勝ち。

    手前のほうが1日あたりの時間価値の減りが速いので、株価がその場に
    留まるほど得をする。手前のIVが後ろより高いとき（順ザヤ）がいちばん有利。
    トレンドが出ている銘柄は株価が離れていって負けるので外す。

    手前の限月は決算をまたがない。短いほうを売っている間に窓を開けられると
    一番痛い。後ろの限月は決算をまたいでも構わないが、そのときは印をつける
    （イベント分のプレミアムを買っていることになるので、意味が変わる）。
    """
    if tech.dev_atr is None or abs(tech.dev_atr) > CAL_MAX_DEV_ATR:
        return []

    backs = usable_back_expiries(u, asof)
    if not backs:
        return []

    out: List[Candidate] = []
    for front in usable_expiries(u, asof):
        tf, dte = front.t(asof), front.dte(asof)
        ff = implied_forward(front, u.spot, asof)
        iv_f = atm_iv(front, ff, tf)
        if iv_f is None or not iv_is_sane(iv_ratio(iv_f, tech)):
            continue
        for back in backs:
            if back.dte(asof) <= dte:
                continue
            tb = back.t(asof)
            fb = implied_forward(back, u.spot, asof)
            iv_b = atm_iv(back, fb, tb)
            if not iv_b:
                continue
            term = iv_f / iv_b
            if term < CAL_MIN_TERM_RATIO:
                continue
            # 行使価格は現値にいちばん近いところ。両方の限月にあるものだけ。
            cands = sorted((r.strike for r in front.rows), key=lambda k: abs(k - u.spot))
            for k in cands[:3]:
                rf, rb = front.row(k), back.row(k)
                if rf is None or rb is None:
                    continue
                if not rf.call.tradable(False) or not rb.call.tradable(True):
                    continue
                sell = _leg("売", "C", rf, rf.call, ff, tf)
                buy = _leg("買", "C", rb, rb.call, fb, tb)
                if sell is None or buy is None:
                    continue
                debit = buy.price - sell.price
                if debit <= 0:
                    continue
                crosses = _crosses_earnings(u, asof, back.expiry)
                out.append(Candidate(
                    strategy="calendar", symbol=u.symbol, expiry=front.expiry,
                    dte=dte, spot=u.spot, legs=[sell, buy], tech=tech,
                    gex=gex_for.get(front.expiry),
                    metrics={
                        "back_expiry": back.expiry,
                        "back_expiry_dte": back.dte(asof),
                        "debit": debit * MULTIPLIER,
                        "term_ratio": term,
                        "iv_front": iv_f, "iv_back": iv_b,
                        "distance_pct": (k / u.spot - 1.0) * 100.0,
                        "back_crosses_earnings": crosses,
                        "atm_iv": iv_f,
                        "iv_ratio": iv_ratio(iv_f, tech),
                    },
                    notes=(["後ろの限月は決算をまたぐ"] if crosses else []),
                    rank=term,
                ))
    out.sort(key=lambda c: c.rank, reverse=True)
    return out[:per_symbol]


# ---------------------------------------------------------------------------

SCREENS = (
    ("long_call", screen_long_call),
    ("bull_put", screen_bull_put),
    ("csp", screen_cash_secured_put),
    ("covered_call", screen_covered_call),
    ("long_put", screen_long_put),
    ("bear_call", screen_bear_call),
    ("calendar", screen_calendar),
)


def screen_symbol(u: Underlying, asof: date) -> Tuple[Technicals, Dict[str, List[Candidate]]]:
    # 現値は板から取り直す。テクニカルは完成した日足のまま計算する。
    u.ref_price = reference_price(u, asof)
    tech = technicals(u)
    usable = usable_expiries(u, asof)
    # ウォールは「その満期までの全満期を合算」して出す。手前の満期の建玉も
    # ディーラーのヘッジには効いているので、合算しないと壁が実勢より薄く出る。
    gex_for: Dict[str, Gex] = {}
    for exp in usable:
        upto = [e for e in u.expiries if e.dte(asof) <= exp.dte(asof) and e.dte(asof) >= 0]
        gex_for[exp.expiry] = build_gex(upto, u.spot, asof)

    result: Dict[str, List[Candidate]] = {}
    for name, fn in SCREENS:
        if name == "covered_call" and not u.held:
            # 現物を持っていない銘柄はカバードコールの対象外。
            result[name] = []
            continue
        result[name] = fn(u, asof, tech, gex_for)
    return tech, result
