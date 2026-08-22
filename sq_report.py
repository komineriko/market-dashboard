#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日経225オプション SQ予測レポート 組み立て層

板データ・現物・前日スナップショットを受け取り、レポート1本ぶんの
データ構造（JSONにできる dict）を返す。HTMLの見た目には関与しない。

文章の生成方針:
  計算した数値から機械的に導ける範囲だけを書く。板から読み取れないこと
  （下げの理由、日中の値動きの形など）は書かず、「未取得」として開示する。
  元レポートがデータ品質の開示に紙面を割いているのと同じ運用にする。
"""

from __future__ import annotations

import math
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import sq_analytics as sa

JST = timezone(timedelta(hours=9))

# 監視ポイントの閾値（元レポートの運用をそのまま定数化したもの）
TH_RR25_PANIC = 6.0
TH_RR25_CALM = 4.0
TH_VI_PANIC = 35.0
TH_VI_CALM = 31.0
TH_CROSS_CHECK = 500.0        # SQ帯を判断に使える乖離の上限（円）
TH_MAXPAIN_SLOPE = 5.0        # これ未満なら max_pain の引力は無視できる
TH_CAL_SPREAD = 2.0           # 期先コンタンゴが本物と見なせる水準（pt）
TH_POSITION_DELTA = 500.0     # 本物の積み増しと見なす新規ヘッジ（枚相当）


def _r(x: Optional[float], nd: int = 0) -> Optional[float]:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(x, nd)


def _fmt(x: Optional[float], nd: int = 0, plus: bool = False) -> str:
    if x is None:
        return "—"
    if round(x, nd) == 0:
        x = 0.0          # 丸めて 0 になる値が "-0" と表示されるのを避ける
    return f"{x:+,.{nd}f}" if plus else f"{x:,.{nd}f}"


# ---------------------------------------------------------------------------
# 限月の決定
# ---------------------------------------------------------------------------

def front_months(base: date, count: int = 3) -> List[str]:
    """基準日から見た当限（SQ未到来の最も近い限月）以降を count 本返す。"""
    y, m = base.year, base.month
    if sa.sq_date_for(y, m) <= base:
        m += 1
        if m > 12:
            y, m = y + 1, 1
    out = []
    for _ in range(count):
        out.append(f"{y % 100:02d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def phase_for(business_days: int) -> str:
    """残営業日からレポートのフェーズを決める。"""
    if business_days > 12:
        return "観測モード"
    if business_days > 5:
        return "収束モード"
    if business_days > 1:
        return "最終週モード"
    return "SQ直前"


# ---------------------------------------------------------------------------
# 1限月ぶんの分析
# ---------------------------------------------------------------------------

class MonthAnalysis:
    """1限月の板から必要な指標を一括で計算して保持する。"""

    def __init__(self, chain: sa.Chain, spot_hint: float, base: date):
        self.chain = chain
        self.month = chain.contract_month
        y, m = sa.parse_contract_month(self.month)
        self.sq = sa.sq_date_for(y, m)
        self.calendar_days = max((self.sq - base).days, 0)
        self.business_days = sa.business_days_between(base, self.sq)
        self.t = max(self.calendar_days, 0) / 365.0

        fwd = sa.implied_forward(chain, spot_hint)
        self.forward = fwd.forward if fwd else spot_hint
        self.parity = fwd
        self.curve = sa.build_iv_curve(chain, self.forward, self.t) if self.t > 0 else None
        self.atm_strike = min(chain.strikes, key=lambda k: abs(k - self.forward)) if chain.strikes else None

        if self.curve:
            self.skew = sa.skew_metrics(self.curve)
            self.moneyness = sa.moneyness_smile(self.curve)
            self.delta_smile = sa.delta_smile(self.curve)
            self.gex = sa.gex_profile(chain, self.curve, self.forward)
            self.walls = sa.wall_map(chain, self.curve, self.forward)
            self.condor = sa.iron_condor(self.walls)
            self.bands = sa.sq_bands(chain, self.curve, self.forward, self.condor)
            self.book = sa.put_book(chain, self.curve, self.forward)
            self.buckets = sa.put_buckets(chain, self.curve, self.forward)
        else:
            self.skew = self.moneyness = self.delta_smile = None
            self.gex = self.walls = self.condor = self.bands = None
            self.book = self.buckets = None
        self.max_pain = sa.max_pain(chain)
        self.pins = sa.pin_candidates(chain, self.forward)

    @property
    def atm_iv(self) -> Optional[float]:
        return self.skew.atm_iv if self.skew else None

    @property
    def sigma_abs(self) -> Optional[float]:
        return self.bands.sigma_abs if self.bands else None


# ---------------------------------------------------------------------------
# 指標のフラット化（監視ポイントと答え合わせで共通に使う）
# ---------------------------------------------------------------------------

def flatten_metrics(a: MonthAnalysis, far: Optional[MonthAnalysis],
                    spot, vi: Optional[float]) -> Dict[str, Optional[float]]:
    cal_spread = None
    if far and a.atm_iv is not None and far.atm_iv is not None:
        cal_spread = a.atm_iv - far.atm_iv     # 正=バックワーデーション / 負=コンタンゴ
    m: Dict[str, Optional[float]] = {
        "forward": _r(a.forward),
        "atm_strike": a.atm_strike,
        "atm_iv": _r(a.atm_iv, 2),
        "far_atm_iv": _r(far.atm_iv, 2) if far else None,
        "cal_spread": _r(cal_spread, 2),
        "sigma_abs": _r(a.sigma_abs),
        "rr25": _r(a.skew.rr25, 2) if a.skew else None,
        "bf25": _r(a.skew.bf25, 2) if a.skew else None,
        "put25_iv": _r(a.skew.put25_iv, 2) if a.skew else None,
        "call25_iv": _r(a.skew.call25_iv, 2) if a.skew else None,
        "gex_at_atm": _r(a.gex.at_forward, 1) if a.gex else None,
        "gex_flip": _r(a.gex.flip) if a.gex else None,
        "gex_regime": a.gex.regime if a.gex else None,
        "put_effective_delta": _r(a.book.effective_delta) if a.book else None,
        "put_premium_oku": _r(a.book.premium_oku, 1) if a.book else None,
        "put_vega_oku": _r(a.book.vega_oku, 2) if a.book else None,
        "put_junk_oi": a.book.junk_oi if a.book else None,
        "put_junk_ratio": _r(a.book.junk_ratio, 1) if a.book else None,
        "max_pain": _r(a.max_pain.strike) if a.max_pain else None,
        "max_pain_slope": _r(a.max_pain.slope_pct, 1) if a.max_pain else None,
        "condor_lower": a.condor.lower if a.condor else None,
        "condor_upper": a.condor.upper if a.condor else None,
        "condor_width": _r(a.condor.width) if a.condor else None,
        "cross_check": _r(a.bands.divergence) if a.bands else None,
        "prob_in_condor": _r(a.bands.prob_in_condor, 1) if a.bands else None,
        "call_oi_total": a.chain.total_oi("call"),
        "put_oi_total": a.chain.total_oi("put"),
        "nikkei_vi": _r(vi, 2),
        "spot_close": _r(spot.close, 2) if spot and spot.close else None,
        "iv_broken": a.curve.broken if a.curve else None,
        "iv_evaluated": a.curve.evaluated if a.curve else None,
        "no_price_call_oi": a.curve.no_price_call_oi if a.curve else None,
        "no_price_put_oi": a.curve.no_price_put_oi if a.curve else None,
    }
    # 捕捉窓は「建玉のある行使価格の範囲」。上場されているだけで建玉ゼロの
    # 行使価格まで含めると窓が実勢より広くなり、前日比の基準がぶれる。
    with_oi = [k for k, r in a.chain.rows.items() if (r.call_oi + r.put_oi) > 0]
    if with_oi:
        m["window_lo"], m["window_hi"] = min(with_oi), max(with_oi)
    else:
        m["window_lo"], m["window_hi"] = a.chain.window()
    m["strike_count"] = len(with_oi) if with_oi else len(a.chain.strikes)
    m["strike_count_listed"] = len(a.chain.strikes)
    return m


# ---------------------------------------------------------------------------
# データ品質の開示
# ---------------------------------------------------------------------------

def build_disclosures(a: MonthAnalysis, prev_metrics: Optional[Dict[str, Any]],
                      window: Optional[sa.WindowCompare], spot, vi_source: Optional[str],
                      spot_move: Optional[float]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []

    # ① SQ帯の信頼度
    if a.bands and a.bands.divergence is not None:
        div = a.bands.divergence
        if abs(div) > TH_CROSS_CHECK:
            out.append({
                "level": "warn",
                "title": f"手法クロスチェックが目安（±{TH_CROSS_CHECK:,.0f}円）を超過。SQ帯の信頼度は低い",
                "body": (f"プレミアム法 {_fmt(a.bands.median_premium)} vs IV法 "
                         f"{_fmt(a.bands.median_iv)} で乖離 {_fmt(div, plus=True)}円。"
                         "価格差分（−ΔC/ΔK）が不安定なため、implied中央値・50%帯・80%帯は"
                         "いずれも参考程度に留める。"),
            })
        else:
            out.append({
                "level": "ok",
                "title": "手法クロスチェックは目安の範囲内。SQ帯は参考値として使える",
                "body": (f"プレミアム法 {_fmt(a.bands.median_premium)} vs IV法 "
                         f"{_fmt(a.bands.median_iv)}、乖離 {_fmt(div, plus=True)}円。"),
            })

    # ② 捕捉窓のずれ
    if prev_metrics and window:
        p_lo, p_hi = prev_metrics.get("window_lo"), prev_metrics.get("window_hi")
        lo, hi = a.chain.window()
        if p_lo and p_hi and (abs(p_lo - lo) > 1 or abs(p_hi - hi) > 1):
            out.append({
                "level": "warn",
                "title": "捕捉窓が前日からずれた。限月別合計の前日比は同一基準ではない",
                "body": (f"前日 {_fmt(p_lo)}–{_fmt(p_hi)}（{prev_metrics.get('strike_count')}本）、"
                         f"本日 {_fmt(lo)}–{_fmt(hi)}（{len(a.chain.strikes)}本）。"
                         f"前日比の共通窓は {_fmt(window.lo)}–{_fmt(window.hi)}（{window.count}本）。"
                         "窓外に出た行使価格は前日比の集計から除外しているため、"
                         "限月別合計と共通窓の合計は一致しない。これは欠落ではなく窓の違い。"),
            })

    # ③ 名目金額の前日比が使えない条件
    if a.curve:
        nc, np_ = a.curve.no_price_call_oi, a.curve.no_price_put_oi
        parts = [f"値なし建玉は CALL {nc:,}枚／PUT {np_:,}枚"]
        if spot_move is not None and abs(spot_move) >= 300:
            parts.append(
                f"さらに現値が {_fmt(spot_move, plus=True)}円 動いたこと自体で、"
                "すべてのOTMのプレミアムが機械的に動いている")
        out.append({
            "level": "warn",
            "title": "名目金額の前日比は使えない。増減の判断は枚数と実効デルタで行う",
            "body": "。".join(parts) + "。名目金額は水準の絶対値としてのみ使う。",
        })

    # ④ IV列の健全性
    if a.curve and a.curve.evaluated:
        ratio = a.curve.broken / a.curve.evaluated * 100
        level = "warn" if ratio > 25 else "ok"
        out.append({
            "level": level,
            "title": f"IV列の健全性: |ivC−ivP|>3pt が {a.curve.broken}/{a.curve.evaluated}本（{ratio:.0f}%）",
            "body": ("両側でIVが立った行使価格のうち、CALLとPUTのIVが3pt以上ずれている本数。"
                     + ("比率が高く、板の一部が薄い。ATM近辺以外の読みは慎重に。"
                        if level == "warn" else "ATM近辺の板は健全で、フォワード・ATM IV・実効デルタ・GEX は信頼してよい。")),
        })

    # ⑤ 未取得の市場データ
    missing: List[str] = []
    if not (spot and spot.high and spot.low):
        missing.append("日経平均の日中高値・安値・寄り値")
    if vi_source is None:
        missing.append("日経VI（引け直前の水準）")
    missing.append("参加者別の当日手口（売買方向を伴う出来高）")
    missing.append("ナイトセッションの先物終値")
    out.append({
        "level": "warn",
        "title": "未取得の市場データ",
        "body": ("／".join(missing) + "。"
                 "これらは自動取得の対象外か公表待ちのため、本レポートでは判断材料にしていない。"
                 "日経VIは SQ_NIKKEI_VI で手入力できる。"),
    })
    return out


# ---------------------------------------------------------------------------
# 結論の文章
# ---------------------------------------------------------------------------

def build_conclusion(a: MonthAnalysis, far: Optional[MonthAnalysis],
                     m: Dict[str, Any], pm: Optional[Dict[str, Any]],
                     window: Optional[sa.WindowCompare],
                     decomp: Optional[sa.DeltaDecomposition],
                     spot, vi: Optional[float]) -> Dict[str, str]:
    rows: Dict[str, str] = {}

    # 本日の相場
    if spot and spot.close:
        s = f"日経平均 {_fmt(spot.close, 2)}円"
        if spot.change is not None:
            s += f"（{_fmt(spot.change, 2, plus=True)}、{_fmt(spot.change_pct, 2, plus=True)}%）"
        if spot.high and spot.low:
            s += f"。日中は {_fmt(spot.low)}〜{_fmt(spot.high)}"
        s += "。⚠ 下落・上昇の理由は板データからは判定できないため記載しない。"
        rows["market_today"] = s
    else:
        rows["market_today"] = "⚠ 日経平均の終値を取得できなかった。"

    # 方向判定（GEXレジーム）
    if a.gex:
        regime = a.gex.regime
        prev_regime = pm.get("gex_regime") if pm else None
        flip = a.gex.flip
        head = f"レジームは{regime}。"
        if prev_regime and prev_regime != regime:
            head = f"レジームが反転した。{prev_regime} → {regime}。"
        detail = f"Net GEX at ATM {_fmt(a.gex.at_forward, 1, plus=True)}億"
        if pm and pm.get("gex_at_atm") is not None:
            detail = (f"Net GEX at ATM {_fmt(pm['gex_at_atm'], 1, plus=True)}億 → "
                      f"{_fmt(a.gex.at_forward, 1, plus=True)}億")
        if flip:
            gap = a.forward - flip
            side = "上" if gap > 0 else "下"
            detail += (f"、フリップ {_fmt(flip)}。現値 F={_fmt(a.forward)} は"
                       f"フリップの{abs(gap):,.0f}円{side}")
            if regime == "ショートガンマ":
                detail += "＝既に加速域の内側。ディーラーは下落を増幅する側"
            else:
                detail += "＝押し目を吸収する構造の内側"
        rows["direction"] = head + detail
    else:
        rows["direction"] = "⚠ IVが解けずGEXを算出できなかった。"

    # SQ参考帯
    if a.bands:
        b = a.bands
        s = (f"インプライド中心 {_fmt(b.center)}／"
             f"50%帯 {_fmt(b.band50[0])}〜{_fmt(b.band50[1])}／"
             f"80%帯 {_fmt(b.band80[0])}〜{_fmt(b.band80[1])}")
        if b.divergence is not None and abs(b.divergence) > TH_CROSS_CHECK:
            s += f"。⚠ 手法クロスチェック {_fmt(b.divergence, plus=True)}円 で目安超過。帯の信頼度は低い"
        rows["sq_band"] = s

    # 攻防レンジ
    if a.walls:
        up = "／".join(f"{_fmt(w.strike)}（名目{w.nominal_oku:.1f}億・壁スコア{w.score:.1f}）"
                       for w in a.walls["upper"][:2])
        lo = "／".join(f"{_fmt(w.strike)}（{w.nominal_oku:.1f}億・{w.score:.1f}）"
                       for w in a.walls["lower"][:3])
        rows["battle_range"] = f"上 {up} ／ 下 {lo}"

    # 本日の性格（恐怖ゲージ）
    if a.skew and a.skew.rr25 is not None:
        rr = a.skew.rr25
        parts = [f"RR25 {_fmt(rr, 2, plus=True)}"]
        if pm and pm.get("rr25") is not None:
            d_p = (a.skew.put25_iv or 0) - (pm.get("put25_iv") or 0)
            d_c = (a.skew.call25_iv or 0) - (pm.get("call25_iv") or 0)
            parts = [f"RR25 {_fmt(pm['rr25'], 2, plus=True)} → {_fmt(rr, 2, plus=True)}"]
            # 拡大の駆動側を内訳で判定する（元レポートの判定基準）
            if rr > pm["rr25"]:
                if d_p > abs(d_c):
                    parts.append(f"駆動はPUT側（25dPut IV {_fmt(d_p, 2, plus=True)}）＝真の下方警戒の再構築")
                elif d_c < 0 and d_p <= abs(d_c):
                    parts.append(f"駆動はCALL側の低下（25dCall IV {_fmt(d_c, 2, plus=True)}）＝恐怖ではなく上値追いの息切れ")
            else:
                if d_p < 0:
                    parts.append(f"PUT側が売られて縮小（{_fmt(d_p, 2, plus=True)}）＝恐怖の後退")
        if a.skew.bf25 is not None:
            parts.append(f"BF25 {_fmt(a.skew.bf25, 2, plus=True)}")
        if vi is not None:
            parts.append(f"日経VI {_fmt(vi, 2)}")
        rows["character"] = "、".join(parts)

    # 最重要の観察（実効デルタの3分解）
    if decomp and a.book:
        pos, spot_t = decomp.position, decomp.spot_and_time
        verdict = ("「積み直した」のではなく「効き始めた」"
                   if abs(spot_t) > abs(pos) else "実際に保険が積み増された")
        rows["key_observation"] = (
            f"実効デルタは {_fmt((pm or {}).get('put_effective_delta'))} → "
            f"{_fmt(a.book.effective_delta)}（{_fmt(decomp.total_change, plus=True)}）。"
            f"そのうち新規ヘッジ（ポジション要因）は {_fmt(pos, plus=True)}枚相当にすぎず、"
            f"残り {_fmt(spot_t, plus=True)} は現値移動と時間経過で"
            f"もともとあった保険のデルタが立ち上がった分。{verdict}。")
    elif a.book:
        rows["key_observation"] = (
            f"PUTブック実効デルタ {_fmt(a.book.effective_delta)}枚、"
            f"理論プレミアム {_fmt(a.book.premium_oku, 1)}億円、"
            f"ベガ {_fmt(a.book.vega_oku, 2)}億円/1pt。"
            "前日スナップショットが無いため3分解は次回から。")

    # 次の分岐
    if a.gex and a.walls:
        lo_wall = a.walls["lower"][0] if a.walls["lower"] else None
        parts = []
        if lo_wall:
            below = [(lv, g) for lv, g in a.gex.levels if lv < a.forward][-4:]
            prof = "、".join(f"{_fmt(lv)} で {_fmt(g, 1, plus=True)}億" for lv, g in below)
            parts.append(f"下 {_fmt(lo_wall.strike)}（名目{lo_wall.nominal_oku:.1f}億・"
                         f"スコア{lo_wall.score:.1f}）。割れると Net GEX は {prof} と推移する")
        if a.gex.flip:
            parts.append(f"上は {_fmt(a.gex.flip)}（フリップ）"
                         + ("── ここを取り返せばロングガンマに戻る"
                            if a.gex.regime == "ショートガンマ" else "── ここを割ると加速域に入る"))
        rows["next_branch"] = " ／ ".join(parts)
    return rows


# ---------------------------------------------------------------------------
# 表
# ---------------------------------------------------------------------------

def _row(label: str, prev, today, change=None, note: str = "",
         nd: int = 0, plus_change: bool = True) -> Dict[str, Any]:
    if change is None and isinstance(prev, (int, float)) and isinstance(today, (int, float)):
        change = today - prev
    return {
        "label": label,
        "prev": _fmt(prev, nd) if isinstance(prev, (int, float)) else (prev or "—"),
        "today": _fmt(today, nd) if isinstance(today, (int, float)) else (today or "—"),
        "change": _fmt(change, nd, plus=plus_change) if isinstance(change, (int, float)) else (change or "—"),
        "note": note,
    }


def build_summary_table(a: MonthAnalysis, far: Optional[MonthAnalysis],
                        m: Dict[str, Any], pm: Optional[Dict[str, Any]],
                        window: Optional[sa.WindowCompare], spot,
                        vi: Optional[float]) -> List[Dict[str, Any]]:
    p = pm or {}
    rows: List[Dict[str, Any]] = []

    if spot and spot.close:
        rows.append(_row("日経平均終値", p.get("spot_close"), spot.close, nd=2,
                         note="現物終値"))
    rows.append(_row("フォワード F", p.get("forward"), m["forward"],
                     note="プット・コール・パリティ由来"))
    rows.append(_row("ATM strike", p.get("atm_strike"), m["atm_strike"], note="ATM基準"))
    rows.append(_row("ATM IV（カーブ補間）", p.get("atm_iv"), m["atm_iv"], nd=2,
                     note="F でのカーブ補間＝公式値"))
    if far:
        rows.append(_row(f"{far.month} ATM IV", p.get("far_atm_iv"), m["far_atm_iv"], nd=2,
                         note="期先"))
        note = "正=バックワーデーション／負=コンタンゴ"
        if m.get("cal_spread") is not None and m["cal_spread"] < -TH_CAL_SPREAD:
            note += "。⚠ コンタンゴが目安超過＝SQ後リスクの織り込み"
        rows.append(_row(f"{a.month} − {far.month} スプレッド", p.get("cal_spread"),
                         m["cal_spread"], nd=2, note=note))
    if vi is not None:
        rows.append(_row("日経VI", p.get("nikkei_vi"), vi, nd=2,
                         note=f"{TH_VI_PANIC:.0f}超でパニック域／{TH_VI_CALM:.0f}割れで鎮静"))
    if window:
        rows.append(_row(f"CALL OI（共通窓 {_fmt(window.lo)}-{_fmt(window.hi)}）",
                         window.call_prev, window.call_today,
                         note="窓を揃えた前日比"))
        rows.append(_row("PUT OI（共通窓）", window.put_prev, window.put_today,
                         note="窓を揃えた前日比"))
        rows.append(_row("PCR（枚数・共通窓）", window.pcr_prev, window.pcr_today, nd=3))
        rows.append(_row("CALL重心（枚数加重）", window.call_centroid_prev,
                         window.call_centroid_today))
        rows.append(_row("PUT重心（枚数加重）", window.put_centroid_prev,
                         window.put_centroid_today))
    rows.append(_row("max_pain", p.get("max_pain"), m["max_pain"],
                     note=f"現値との差 {_fmt((m['max_pain'] - m['forward']) if m.get('max_pain') else None, plus=True)}円"
                          if m.get("max_pain") else ""))
    slope_note = f"{TH_MAXPAIN_SLOPE:.0f}%割れで引力は無視可"
    rows.append(_row("max_pain 傾き", p.get("max_pain_slope"), m["max_pain_slope"], nd=1,
                     note=slope_note))
    rows.append(_row("Net GEX at ATM（億円）", p.get("gex_at_atm"), m["gex_at_atm"], nd=1,
                     note="符号反転＝レジーム転換"))
    rows.append(_row("GEXフリップ", p.get("gex_flip"), m["gex_flip"],
                     note="現値がこの下ならショートガンマ"))
    rows.append(_row("GEXレジーム", p.get("gex_regime"), m["gex_regime"],
                     change="反転" if (p.get("gex_regime") and p["gex_regime"] != m["gex_regime"]) else "—",
                     note="ショートガンマ＝トレンド加速・オーバーシュート傾向"))
    rows.append(_row("1σ（残期間）", p.get("sigma_abs"), m["sigma_abs"],
                     note=f"残{a.calendar_days}暦日"))
    rr_note = f"+{TH_RR25_PANIC:.1f} 超でパニック域／+{TH_RR25_CALM:.1f} 割れで正常化"
    rows.append(_row("RR25（25dP − 25dC）", p.get("rr25"), m["rr25"], nd=2, note=rr_note))
    rows.append(_row("BF25", p.get("bf25"), m["bf25"], nd=2, note="両テールの持ち上がり"))
    rows.append(_row("PUTブック実効デルタ（枚）", p.get("put_effective_delta"),
                     m["put_effective_delta"],
                     note="判定はポジション要因で行う（§2-4）"))
    rows.append(_row("PUTブック理論プレミアム（億円）", p.get("put_premium_oku"),
                     m["put_premium_oku"], nd=1, note="現値移動の寄与が大きい"))
    rows.append(_row("PUTブック ベガ（億円/1pt）", p.get("put_vega_oku"),
                     m["put_vega_oku"], nd=2))
    rows.append(_row("20円未満PUTの建玉", p.get("put_junk_oi"), m["put_junk_oi"],
                     note=f"当日 {m['put_junk_ratio']:.0f}%＝ゴミ玉比率"
                          if m.get("put_junk_ratio") is not None else ""))
    rows.append(_row("捕捉窓",
                     f"{_fmt(p.get('window_lo'))}–{_fmt(p.get('window_hi'))}" if p.get("window_lo") else "—",
                     f"{_fmt(m['window_lo'])}–{_fmt(m['window_hi'])}",
                     change=f"共通窓 {_fmt(window.lo)}–{_fmt(window.hi)}（{window.count}本）" if window else "—",
                     note="窓が動くと限月別合計の前日比は使えない"))
    rows.append(_row("IV列健全性（|ivC−ivP|>3pt）",
                     f"{p.get('iv_broken')}/{p.get('iv_evaluated')}" if p.get("iv_evaluated") else "—",
                     f"{m['iv_broken']}/{m['iv_evaluated']}" if m.get("iv_evaluated") else "—",
                     change="—", note="板の質"))
    rows.append(_row("手法クロスチェック乖離", p.get("cross_check"), m["cross_check"],
                     note=f"±{TH_CROSS_CHECK:,.0f}円 以内でSQ帯を判断に使える"))
    return rows


def build_skew_section(a: MonthAnalysis, history: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    # 同じ日に2回走らせても行が重複しないよう、日付で後勝ちにする
    by_date: Dict[str, Dict[str, Any]] = {}
    for h in history:
        by_date[h.get("date")] = h
    rr_rows = []
    for h in [by_date[k] for k in sorted(by_date)][-3:]:
        rr_rows.append({
            "date": h.get("date"),
            "rr25": _fmt(h.get("rr25"), 2, plus=True),
            "bf25": _fmt(h.get("bf25"), 2, plus=True),
            "atm_iv": _fmt(h.get("atm_iv"), 2),
            "put25_iv": _fmt(h.get("put25_iv"), 2),
            "call25_iv": _fmt(h.get("call25_iv"), 2),
        })
    money = []
    if a.moneyness:
        for mn, iv in a.moneyness.items():
            money.append({"m": f"{mn:.2f}", "iv": _fmt(iv * 100 if iv else None, 2),
                          "strike": _fmt(a.forward * mn)})
    deltas = []
    if a.delta_smile:
        for label, res in a.delta_smile.items():
            deltas.append({
                "label": label,
                "strike": _fmt(res[0]) if res else "—",
                "iv": _fmt(res[1] * 100, 2) if res else "—",
            })
    return {"rr_history": rr_rows, "moneyness": money, "delta": deltas}


def build_put_book_section(a: MonthAnalysis, decomp: Optional[sa.DeltaDecomposition],
                           window: Optional[sa.WindowCompare]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if a.book:
        out["summary"] = {
            "oi": _fmt(a.book.oi),
            "effective_delta": _fmt(a.book.effective_delta),
            "vega_oku": _fmt(a.book.vega_oku, 2),
            "premium_oku": _fmt(a.book.premium_oku, 1),
            "junk_oi": _fmt(a.book.junk_oi),
            "junk_ratio": f"{a.book.junk_ratio:.0f}%",
        }
    if decomp:
        out["decomposition"] = {
            "total": _fmt(decomp.total_change, plus=True),
            "position": _fmt(decomp.position, plus=True),
            "spot_and_time": _fmt(decomp.spot_and_time, plus=True),
            "verdict": ("新規に建てられた保険が主体＝本物の積み増し"
                        if abs(decomp.position) > abs(decomp.spot_and_time)
                        else "大半は現値移動で既存玉が立ち上がった分＝『効き始めた』"),
            "is_real_build": abs(decomp.position) > TH_POSITION_DELTA and decomp.position > 0,
            "rows": [{
                "strike": _fmt(k), "prev": _fmt(p), "today": _fmt(t),
                "diff": _fmt(d, plus=True), "delta": f"{dl:.3f}",
                "contribution": _fmt(c, plus=True),
            } for k, p, t, d, dl, c in decomp.contributions[:14]],
        }
    if a.buckets:
        out["buckets"] = [{
            "label": label, "oi": _fmt(oi), "effective_delta": _fmt(eff),
        } for label, oi, eff in a.buckets]
        total_eff = sum(e for _, _, e in a.buckets) or 1
        for row, (_, _, eff) in zip(out["buckets"], a.buckets):
            row["share"] = f"{eff / total_eff * 100:.1f}%"
    return out


def build_oi_section(a: MonthAnalysis, prev_chain: Optional[sa.Chain],
                     window: Optional[sa.WindowCompare]) -> Dict[str, Any]:
    if not window:
        return {"available": False}
    out: Dict[str, Any] = {
        "available": True,
        "window": f"{_fmt(window.lo)}–{_fmt(window.hi)}（{window.count}本）",
        "totals": [
            _row("CALL 合計", window.call_prev, window.call_today),
            _row("PUT 合計", window.put_prev, window.put_today),
            _row("PCR（枚数）", window.pcr_prev, window.pcr_today, nd=3),
            _row("CALL 重心（枚数加重）", window.call_centroid_prev, window.call_centroid_today),
            _row("PUT 重心（枚数加重）", window.put_centroid_prev, window.put_centroid_today),
            _row("PUT テール（≤60,000）", *window.put_tail_60k),
            _row("PUT テール（≤53,000）", *window.put_tail_53k),
        ],
        "call_changes": [{"strike": _fmt(k), "prev": _fmt(p), "today": _fmt(t),
                          "diff": _fmt(d, plus=True)}
                         for k, p, t, d in window.call_changes[:12]],
        "put_changes": [{"strike": _fmt(k), "prev": _fmt(p), "today": _fmt(t),
                         "diff": _fmt(d, plus=True)}
                        for k, p, t, d in window.put_changes[:14]],
    }
    if prev_chain:
        for side in ("call", "put"):
            rows = sa.bucket_compare(a.chain, prev_chain, a.forward, side)
            out[f"bucket_{side}"] = [{"center": _fmt(c), "prev": _fmt(p),
                                      "today": _fmt(t), "diff": _fmt(d, plus=True)}
                                     for c, p, t, d in rows]
    return out


def build_walls_section(a: MonthAnalysis) -> Dict[str, Any]:
    def wall_rows(ws: Sequence[sa.Wall]) -> List[Dict[str, Any]]:
        return [{
            "strike": _fmt(w.strike), "oi": _fmt(w.oi),
            "price": _fmt(w.price) if w.price else "—",
            "nominal_oku": f"{w.nominal_oku:.1f}",
            "score": f"{w.score:.1f}",
        } for w in ws]

    out: Dict[str, Any] = {}
    if a.walls:
        out["upper"] = wall_rows(a.walls["upper"])
        out["lower"] = wall_rows(a.walls["lower"])
    if a.condor:
        out["condor"] = {"lower": _fmt(a.condor.lower), "upper": _fmt(a.condor.upper),
                         "width": _fmt(a.condor.width)}
    out["pins"] = [{"strike": _fmt(k), "nominal_oku": f"{v:.1f}", "oi": _fmt(oi)}
                   for k, v, oi in a.pins]
    if a.gex:
        out["gex_profile"] = [{"level": _fmt(lv), "gex": _fmt(g, 1, plus=True),
                               "is_flip": False}
                              for lv, g in a.gex.levels]
        out["gex_flip"] = _fmt(a.gex.flip) if a.gex.flip else "—"
        out["gex_regime"] = a.gex.regime
    if a.max_pain:
        out["max_pain"] = {"strike": _fmt(a.max_pain.strike),
                           "payout_oku": _fmt(a.max_pain.payout_oku, 1),
                           "slope": f"{a.max_pain.slope_pct:.1f}%",
                           "negligible": a.max_pain.slope_pct < TH_MAXPAIN_SLOPE}
    return out


def build_deep_put_table(a: MonthAnalysis, prev_chain: Optional[sa.Chain],
                         span: float = 0.16) -> List[Dict[str, Any]]:
    """現値近傍から深部までのPUT建玉推移。枚数と効き目の乖離を見るための表。"""
    if not a.book:
        return []
    rows: List[Dict[str, Any]] = []
    for k in sorted(a.chain.rows, reverse=True):
        r = a.chain.rows[k]
        if r.put_oi <= 0:
            continue
        if not (a.forward * (1 - span) <= k <= a.forward * 1.03):
            continue
        if k % 500 != 0:
            continue
        d = a.book.per_strike_delta.get(k)
        prev_oi = prev_chain.rows[k].put_oi if (prev_chain and k in prev_chain.rows) else None
        diff = (r.put_oi - prev_oi) if prev_oi is not None else None
        rows.append({
            "strike": _fmt(k),
            "prev": _fmt(prev_oi) if prev_oi is not None else "—",
            "today": _fmt(r.put_oi),
            "diff": _fmt(diff, plus=True) if diff is not None else "—",
            "price": _fmt(r.put_price) if r.put_price else "—",
            "moneyness": f"{k / a.forward:.3f}",
            "delta": f"{d:.3f}" if d is not None else "—",
            "contribution": _fmt(diff * abs(d), plus=True) if (diff is not None and d) else "—",
        })
    return rows


# ---------------------------------------------------------------------------
# 中期レンジ
# ---------------------------------------------------------------------------

def build_midterm_section(analyses: Sequence[MonthAnalysis], forward: float) -> Dict[str, Any]:
    comp = sa.composite_range([a.chain for a in analyses])
    return {
        "months": [a.month for a in analyses],
        "ceiling": {"strike": _fmt(comp.ceiling[0]), "net": _fmt(comp.ceiling[1], plus=True)}
                   if comp.ceiling else None,
        "floor": {"strike": _fmt(comp.floor[0]), "net": _fmt(comp.floor[1], plus=True)}
                 if comp.floor else None,
        "forward": _fmt(forward),
        "upper": [{"strike": _fmt(k), "net": _fmt(v, plus=True)} for k, v in comp.upper],
        "lower": [{"strike": _fmt(k), "net": _fmt(v, plus=True)} for k, v in comp.lower],
        "per_month": [{"month": m, "call": _fmt(c), "put": _fmt(p),
                       "pcr": _fmt(pcr, 3) if pcr else "—"}
                      for m, c, p, pcr in comp.per_month],
    }


# ---------------------------------------------------------------------------
# 監視ポイントと答え合わせ
# ---------------------------------------------------------------------------

def build_watch(a: MonthAnalysis, far: Optional[MonthAnalysis],
                m: Dict[str, Any], decomp: Optional[sa.DeltaDecomposition]) -> List[Dict[str, Any]]:
    """
    明日以降の監視ポイント。閾値は現値移動に汚染されない正規化された量で置く。
    ここに載せた項目が、翌日そのまま「答え合わせ」で自動評価される。
    """
    items: List[Dict[str, Any]] = []

    if m.get("gex_flip") is not None:
        gap = a.forward - m["gex_flip"]
        items.append({
            "id": "gex_flip", "metric": "gex_flip", "value": m["gex_flip"], "star": True,
            "label": f"GEXフリップ {_fmt(m['gex_flip'])} と現値の位置関係（最優先）",
            "rule": (f"現値 F={_fmt(a.forward)} はフリップの {abs(gap):,.0f}円"
                     f"{'上' if gap > 0 else '下'}。回復すればロングガンマに戻り押し目吸収が復活、"
                     "割れば加速する。フリップとの距離が ±300円 以内の間は方向はどちらにも転びうる。"),
        })
    if m.get("gex_at_atm") is not None:
        items.append({
            "id": "gex_at_atm", "metric": "gex_at_atm", "value": m["gex_at_atm"],
            "label": f"Net GEX at ATM {_fmt(m['gex_at_atm'], 1, plus=True)}億円",
            "rule": "符号が反転すればレジーム転換。ゼロ近傍はどちらにも振れやすい。",
        })
    if m.get("put_effective_delta") is not None:
        pos = decomp.position if decomp else None
        items.append({
            "id": "put_effective_delta", "metric": "put_effective_delta",
            "value": m["put_effective_delta"], "star": True,
            "label": f"PUTブック実効デルタ {_fmt(m['put_effective_delta'])}枚",
            "rule": (f"判定はポジション要因で行う。本日は {_fmt(pos, plus=True)}枚相当。"
                     f"翌日も必ず3分解し、ポジション要因が +{TH_POSITION_DELTA:,.0f}枚超なら本物の積み増し、"
                     "マイナスなら『相場が戻って自動的に減っただけ』と区別する。"),
        })
    if m.get("rr25") is not None:
        items.append({
            "id": "rr25", "metric": "rr25", "value": m["rr25"], "star": True,
            "label": (f"RR25 {_fmt(m['rr25'], 2, plus=True)} の駆動側"
                      f"（25dP {_fmt(m.get('put25_iv'), 2)}／25dC {_fmt(m.get('call25_iv'), 2)}）"),
            "rule": (f"PUT側が下がって縮小＝恐怖の後退（本物）／CALL側が上がって縮小＝上値追いの再開。"
                     f"+{TH_RR25_PANIC:.1f} 超に拡大したらパニック域、+{TH_RR25_CALM:.1f} 割れで正常化。"),
            "hi": TH_RR25_PANIC, "hi_means": "パニック域",
            "lo": TH_RR25_CALM, "lo_means": "正常化",
        })
    if a.walls and a.walls["upper"]:
        w = a.walls["upper"][0]
        items.append({
            "id": "upper_wall_oi", "metric": f"wall_oi_call_{int(w.strike)}", "value": w.oi, "star": True,
            "label": f"上値の蓋 {_fmt(w.strike)} 帯の建玉（±375円で {_fmt(w.oi)}枚）",
            "rule": ("枚数で見る。名目金額は現値との距離で機械的に動くため閾値には使わない。"
                     f"{w.oi * 1.15:,.0f}枚超で蓋の強化／{w.oi * 0.85:,.0f}枚割れで蓋が外れた。"),
            "hi": w.oi * 1.15, "hi_means": "蓋の強化",
            "lo": w.oi * 0.85, "lo_means": "蓋が外れた",
        })
    if a.walls and a.walls["lower"]:
        w = a.walls["lower"][0]
        items.append({
            "id": "lower_wall_oi", "metric": f"wall_oi_put_{int(w.strike)}", "value": w.oi,
            "label": (f"下値の主戦場 {_fmt(w.strike)}（建玉 {_fmt(w.oi)}枚・"
                      f"名目{w.nominal_oku:.1f}億・スコア{w.score:.1f}）"),
            "rule": "さらに積み増されれば防衛の意思／減れば防衛の放棄。",
        })
    if m.get("cal_spread") is not None and far:
        items.append({
            "id": "cal_spread", "metric": "cal_spread", "value": m["cal_spread"],
            "label": f"{a.month} と {far.month} の ATM IV スプレッド {_fmt(m['cal_spread'], 2, plus=True)}pt",
            "rule": (f"負＝コンタンゴ（期先が高い）。コンタンゴが {TH_CAL_SPREAD:.1f}pt 超に拡大すれば"
                     "SQ後リスクの織り込みは本物／バックへ戻れば急落局面の一時的な現象。"),
            "lo": -TH_CAL_SPREAD, "lo_means": "SQ後リスクの織り込みが本物",
        })
    if m.get("nikkei_vi") is not None:
        items.append({
            "id": "nikkei_vi", "metric": "nikkei_vi", "value": m["nikkei_vi"],
            "label": f"日経VI {_fmt(m['nikkei_vi'], 2)}",
            "rule": f"{TH_VI_PANIC:.0f}超でパニック域／{TH_VI_CALM:.0f}割れで鎮静。",
            "hi": TH_VI_PANIC, "hi_means": "パニック域",
            "lo": TH_VI_CALM, "lo_means": "鎮静",
        })
    if m.get("cross_check") is not None:
        items.append({
            "id": "cross_check", "metric": "cross_check", "value": m["cross_check"],
            "label": f"手法クロスチェックの乖離 {_fmt(m['cross_check'], plus=True)}円",
            "rule": f"±{TH_CROSS_CHECK:,.0f}円 以内に戻るまで、SQ帯・implied中央値を判断に使わない。",
        })
    if m.get("max_pain_slope") is not None:
        items.append({
            "id": "max_pain_slope", "metric": "max_pain_slope", "value": m["max_pain_slope"],
            "label": f"max_pain {_fmt(m.get('max_pain'))} の傾き {m['max_pain_slope']:.1f}%",
            "rule": f"{TH_MAXPAIN_SLOPE:.0f}%割れなら引力は無視可。上昇すれば引力が復活。",
        })
    return items


def build_answer_check(prev_watch: Sequence[Dict[str, Any]],
                       metrics: Dict[str, Any],
                       extra: Dict[str, Any]) -> List[Dict[str, Any]]:
    """前日の監視ポイントを、本日の値で自動評価する。"""
    out: List[Dict[str, Any]] = []
    lookup = dict(metrics)
    lookup.update(extra)
    for item in prev_watch:
        key = item.get("metric")
        now = lookup.get(key) if key else None
        before = item.get("value")
        diff = None
        if isinstance(now, (int, float)) and isinstance(before, (int, float)):
            diff = now - before
        verdict = "—"
        if now is None:
            verdict = "⚠ 本日は同じ指標を算出できず、答え合わせ不能"
        else:
            hits = []
            if isinstance(now, (int, float)) and isinstance(before, (int, float)):
                # 符号の反転は閾値とは別に効く。GEXなら レジーム転換 そのものを意味する。
                if before * now < 0:
                    hits.append(f"★符号が反転（{'＋→−' if before > 0 else '−→＋'}）")
            if isinstance(now, (int, float)):
                if item.get("hi") is not None and now >= item["hi"]:
                    hits.append(f"閾値 {_fmt(item['hi'], 2)} を超過 → {item.get('hi_means', '')}")
                if item.get("lo") is not None and now <= item["lo"]:
                    hits.append(f"閾値 {_fmt(item['lo'], 2)} を下回り → {item.get('lo_means', '')}")
            verdict = "／".join(hits) if hits else "閾値には未到達"
        out.append({
            "label": item.get("label", item.get("id", "")),
            "before": _fmt(before, 2) if isinstance(before, (int, float)) else (before or "—"),
            "now": _fmt(now, 2) if isinstance(now, (int, float)) else (now or "—"),
            "diff": _fmt(diff, 2, plus=True) if diff is not None else "—",
            "verdict": verdict,
            "rule": item.get("rule", ""),
        })
    return out


# ---------------------------------------------------------------------------
# イベントと限界
# ---------------------------------------------------------------------------

def build_events(base: date, sq: date, extra_events: Sequence[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    SQまでの日程。マクロイベントは自動取得の対象外なので、
    sq_events.json に書いた分だけを載せ、無ければSQ日のみを示す。
    """
    rows: List[Dict[str, str]] = []
    for ev in extra_events:
        d = ev.get("date")
        try:
            dd = datetime.strptime(d, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if dd < base:
            continue
        rows.append({
            "date": d,
            "name": ev.get("name", ""),
            "note": ev.get("note", ""),
            "after_sq": "SQ後" if dd > sq else "",
        })
    rows.append({"date": sq.isoformat(), "name": f"{sq.month}月限SQ算出", 
                 "note": "本レポートの対象", "after_sq": ""})
    rows.sort(key=lambda r: r["date"])
    return rows


def build_limits(a: MonthAnalysis, spot, vi: Optional[float],
                 window: Optional[sa.WindowCompare],
                 decomp: Optional[sa.DeltaDecomposition]) -> List[str]:
    limits = [
        f"残{a.business_days}営業日でのSQ値予測は原理的に不可能。"
        f"中心点 {_fmt(a.forward)} は参考値であって予測ではない。",
        "Net GEX は「ディーラー = +CALL建玉 − PUT建玉」の慣例仮定に依存する。"
        "仮定が崩れる局面では符号が反転しうる。",
        "壁スコアは建玉・名目金額・現値からの近さを合成した独自の相対指標で、"
        "絶対的な意味を持たない。同じ日の中での序列としてのみ使う。",
        "IVは清算値段からの逆算値。板が薄い行使価格では解が不安定になる。",
    ]
    if a.bands and a.bands.divergence is not None and abs(a.bands.divergence) > TH_CROSS_CHECK:
        limits.append(f"手法クロスチェックが目安（±{TH_CROSS_CHECK:,.0f}円）を超えており、SQ帯の信頼度が低い。")
    if not (spot and spot.high and spot.low):
        limits.append("日経平均の日中高値・安値・寄り値が未取得のため、"
                      "値動きの形（寄り天か後場崩れか）を特定できていない。")
    if vi is None:
        limits.append("日経VIが未取得。恐怖ゲージは板由来のRR25・BF25・ATM IVのみで判定している。")
    limits.append("参加者別ネット建玉は週次公表で、証券会社単位・売超買超それぞれ上位15社のみ。"
                  "市場全体の集計ではなく、自己玉と顧客玉も区別できない。")
    if not window:
        limits.append("前日スナップショットが無いため、前日比・実効デルタの3分解・答え合わせは"
                      "次回の更新から有効になる。")
    if decomp:
        limits.append("実効デルタの3分解のうち、現値移動要因と時間経過要因は分離していない（合算）。")
    if a.curve and a.curve.evaluated:
        limits.append(f"IV列の崩壊が {a.curve.broken}/{a.curve.evaluated}本ある。"
                      "深いOTMのIV・デルタはその分不確かである。")
    return limits


# ---------------------------------------------------------------------------
# スナップショットの直列化
# ---------------------------------------------------------------------------

def chain_to_dict(chain: sa.Chain) -> Dict[str, Any]:
    return {
        "contract_month": chain.contract_month,
        "rows": [[r.strike, r.call_price, r.put_price, r.call_oi, r.put_oi,
                  r.call_volume, r.put_volume]
                 for r in (chain.rows[k] for k in chain.strikes)],
    }


def chain_from_dict(d: Dict[str, Any]) -> sa.Chain:
    chain = sa.Chain(contract_month=d["contract_month"])
    for row in d.get("rows", []):
        strike, cp, pp, co, po = row[0], row[1], row[2], row[3], row[4]
        cv = row[5] if len(row) > 5 else 0
        pv = row[6] if len(row) > 6 else 0
        chain.add(sa.StrikeRow(strike=strike, call_price=cp, put_price=pp,
                               call_oi=int(co or 0), put_oi=int(po or 0),
                               call_volume=int(cv or 0), put_volume=int(pv or 0)))
    return chain


def wall_oi_metrics(chain: sa.Chain, forward: float,
                    half_width: float = 375.0, span: float = 0.18) -> Dict[str, int]:
    """
    壁の帯（±375円）の建玉を、答え合わせで参照できるフラットなキーにして返す。

    範囲を広めに取る。前日に監視対象とした壁が現値から離れると、
    翌日に同じキーを算出できず「答え合わせ不能」になってしまうため。
    """
    out: Dict[str, int] = {}
    for center in chain.strikes:
        if center % 500 != 0 or abs(center - forward) > span * forward:
            continue
        c = sum(r.call_oi for k, r in chain.rows.items() if abs(k - center) <= half_width)
        p = sum(r.put_oi for k, r in chain.rows.items() if abs(k - center) <= half_width)
        out[f"wall_oi_call_{int(center)}"] = c
        out[f"wall_oi_put_{int(center)}"] = p
    return out


# ---------------------------------------------------------------------------
# 組み立て
# ---------------------------------------------------------------------------

def build_report(chains: Dict[str, sa.Chain], spot, vi: Optional[float],
                 vi_source: Optional[str], base: date,
                 prev_snapshot: Optional[Dict[str, Any]] = None,
                 history: Optional[Sequence[Dict[str, Any]]] = None,
                 events: Optional[Sequence[Dict[str, str]]] = None,
                 origin: str = "", extra_sources: Optional[Sequence[str]] = None,
                 participants=None
                 ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    レポート用の dict と、翌日のために保存するスナップショットを返す。
    """
    months = front_months(base, 3)
    ordered = [chains[m] for m in months if m in chains]
    if not ordered:
        # 想定した限月が板に無い場合は、板にある限月のうち近いものを使う
        ordered = [chains[m] for m in sorted(chains)]
    if not ordered:
        raise ValueError("オプション板が空です。")

    spot_hint = (spot.close if (spot and spot.close) else
                 (ordered[0].strikes[len(ordered[0].strikes) // 2] if ordered[0].strikes else 0))
    analyses = [MonthAnalysis(ch, spot_hint, base) for ch in ordered]
    a = analyses[0]
    far = analyses[1] if len(analyses) > 1 else None

    metrics = flatten_metrics(a, far, spot, vi)
    prev_metrics = (prev_snapshot or {}).get("metrics")
    prev_watch = (prev_snapshot or {}).get("watch") or []
    prev_chains = (prev_snapshot or {}).get("chains") or {}
    prev_chain = chain_from_dict(prev_chains[a.month]) if a.month in prev_chains else None

    window = sa.compare_window(a.chain, prev_chain) if prev_chain else None
    decomp = None
    if prev_chain and a.book and a.curve and prev_metrics:
        prev_eff = prev_metrics.get("put_effective_delta")
        if prev_eff is not None:
            decomp = sa.decompose_effective_delta(a.chain, prev_chain, a.book, prev_eff)

    spot_move = None
    if prev_metrics and prev_metrics.get("forward") and metrics.get("forward"):
        spot_move = metrics["forward"] - prev_metrics["forward"]

    walls_extra = wall_oi_metrics(a.chain, a.forward)
    hist = list(history or [])

    report: Dict[str, Any] = {
        "meta": {
            "title": ("SQ予測レポート 日経225オプション "
                      f"{sa.parse_contract_month(a.month)[0]}年{sa.parse_contract_month(a.month)[1]}月限"),
            "month": a.month,
            "base_date": base.isoformat(),
            "base_weekday": "月火水木金土日"[base.weekday()],
            "sq_date": a.sq.isoformat(),
            "sq_weekday": "月火水木金土日"[a.sq.weekday()],
            "is_major_sq": a.sq.month in (3, 6, 9, 12),
            "calendar_days": a.calendar_days,
            "business_days": a.business_days,
            "phase": phase_for(a.business_days),
            "generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
            "origin": origin,
            "sources": list(extra_sources or []),
            "has_prev": prev_chain is not None,
        },
        "summary": build_summary(a, metrics, prev_metrics, spot, decomp),
        "conclusion": build_conclusion(a, far, metrics, prev_metrics, window, decomp, spot, vi),
        "disclosures": build_disclosures(a, prev_metrics, window, spot, vi_source, spot_move),
        "sq_band": {
            "center": _fmt(a.bands.center) if a.bands else "—",
            "sigma": _fmt(a.sigma_abs) if a.sigma_abs else "—",
            "band50": [_fmt(a.bands.band50[0]), _fmt(a.bands.band50[1])] if a.bands else ["—", "—"],
            "band80": [_fmt(a.bands.band80[0]), _fmt(a.bands.band80[1])] if a.bands else ["—", "—"],
            "median_premium": _fmt(a.bands.median_premium) if a.bands else "—",
            "median_iv": _fmt(a.bands.median_iv) if a.bands else "—",
            "divergence": _fmt(a.bands.divergence, plus=True) if a.bands else "—",
            "reliable": a.bands.reliable if a.bands else False,
            "prob_in_condor": _fmt(a.bands.prob_in_condor, 1) if a.bands else "—",
            "parity": [{"strike": _fmt(k), "forward": _fmt(v)} for k, v in (a.parity.reference if a.parity else [])],
            "parity_spread": _fmt(a.parity.spread) if a.parity else "—",
        },
        "summary_table": build_summary_table(a, far, metrics, prev_metrics, window, spot, vi),
        "skew": build_skew_section(a, hist + [dict(date=base.isoformat(), **{
            k: metrics.get(k) for k in ("rr25", "bf25", "atm_iv", "put25_iv", "call25_iv")})]),
        "put_book": build_put_book_section(a, decomp, window),
        "oi_compare": build_oi_section(a, prev_chain, window),
        "midterm": build_midterm_section(analyses, a.forward),
        "walls": build_walls_section(a),
        "deep_put": build_deep_put_table(a, prev_chain),
        "events": build_events(base, a.sq, events or []),
        "watch": build_watch(a, far, metrics, decomp),
        "answer_check": build_answer_check(prev_watch, metrics, walls_extra),
        "limits": build_limits(a, spot, vi, window, decomp),
        "participants": build_participant_section(
            participants, (prev_snapshot or {}).get("participants"),
            [an.month for an in analyses], base),
        "metrics": metrics,
    }

    snapshot = {
        "date": base.isoformat(),
        "month": a.month,
        "metrics": metrics,
        "watch": [{k: v for k, v in item.items()
                   if k in ("id", "metric", "value", "label", "rule", "hi", "hi_means", "lo", "lo_means")}
                  for item in report["watch"]],
        "chains": {an.month: chain_to_dict(an.chain) for an in analyses},
        "participants": participant_snapshot(participants, [an.month for an in analyses]),
    }
    return report, snapshot


# ---------------------------------------------------------------------------
# 取引参加者別ネット建玉（週次）
# ---------------------------------------------------------------------------

def build_participant_section(psrc, prev: Optional[Dict[str, Any]],
                              months: Sequence[str], base: date,
                              top: int = 14) -> Dict[str, Any]:
    """
    参加者別のネット建玉と前週比。

    限月では絞らない。先物の限月は3・6・9・12月の四半期サイクルで、
    オプションの当限（例 9・10・11月）で絞ると12月限が丸ごと落ちる。
    ファイルには取引されている限月しか載らないので、全部を合算するのが正しい。

    このデータの性質上、次の3点はレポート側で必ず明示する。
      * 証券会社単位であり、その先の顧客が誰かは分からない（自己玉と顧客玉が混在）
      * 売超・買超それぞれ上位15社のみで、市場全体の集計ではない
      * 週次なので、日次のレポートに対して常に時間差がある
    """
    if psrc is None:
        return {
            "available": False,
            "note": "参加者別建玉残高を取得できなかった。",
        }

    agg = sa_aggregate(psrc.rows, None)
    covered = sorted({r.month for r in psrc.rows})
    prev_net = (prev or {}).get("net") or {}
    prev_as_of = (prev or {}).get("as_of")

    rows = []
    for name, net, breakdown in agg:
        before = prev_net.get(name)
        change = (net - before) if isinstance(before, (int, float)) else None
        rows.append({
            "name": name,
            "net": _fmt(net, 1, plus=True),
            "before": _fmt(before, 1, plus=True) if before is not None else "—",
            "change": _fmt(change, 1, plus=True) if change is not None else "—",
            "side": "ロング" if net > 0 else ("ショート" if net < 0 else "—"),
            "breakdown": "／".join(f"{p} {v:+,.0f}" for p, v in sorted(breakdown.items())),
            "_abs": abs(net),
        })
    rows.sort(key=lambda r: r["_abs"], reverse=True)
    for r in rows:
        r.pop("_abs")

    total = sum(net for _, net, _ in agg)
    lag = (base - psrc.as_of).days

    return {
        "available": True,
        "as_of": psrc.as_of.isoformat(),
        "lag_days": lag,
        "origin": psrc.origin,
        "months": covered,
        "products": sorted({r.product for r in psrc.rows}),
        "total": _fmt(total, 1, plus=True),
        "prev_as_of": prev_as_of or "—",
        "rows": rows[:top],
        "stale": lag >= 3,
    }


def sa_aggregate(rows, months):
    """sq_fetch の集計をレポート層から呼ぶための薄いラッパ。"""
    import sq_fetch
    return sq_fetch.aggregate_participant_net(rows, months)


def participant_snapshot(psrc, months: Sequence[str]) -> Optional[Dict[str, Any]]:
    if psrc is None:
        return None
    return {
        "as_of": psrc.as_of.isoformat(),
        "net": {name: round(net, 1) for name, net, _ in sa_aggregate(psrc.rows, None)},
    }


# ---------------------------------------------------------------------------
# ひとことまとめ（専門用語を使わない要約）
# ---------------------------------------------------------------------------
#
# レポート本文は専門用語で書いてあるので、冒頭に平易な要約を置く。
# 数字はすべて本文と同じ計算結果を使い、言い換えだけを行う。
# 新しい判断はここでは足さない。

def build_summary(a: MonthAnalysis, m: Dict[str, Any], pm: Optional[Dict[str, Any]],
                  spot, decomp: Optional[sa.DeltaDecomposition]) -> Dict[str, Any]:
    p = pm or {}
    points: List[Dict[str, str]] = []

    # 状態の見出し
    short_gamma = m.get("gex_regime") == "ショートガンマ"
    headline = "値動きが増幅されやすい状態" if short_gamma else "値動きが吸収されやすい状態"
    sub = ("オプションの売り手が値動きを打ち消せず、動き出すと勢いがつきやすい"
           if short_gamma else
           "オプションの売り手が値動きを打ち消す側に回り、押し目が吸収されやすい")

    # 相場
    if spot and spot.close:
        s = f"日経平均は {_fmt(spot.close, 0)}円"
        if spot.change is not None:
            direction = "上げ" if spot.change > 0 else ("下げ" if spot.change < 0 else "横ばい")
            s += f"（前日比 {_fmt(spot.change, 0, plus=True)}円・{_fmt(spot.change_pct, 1, plus=True)}%）の{direction}"
        points.append({"label": "相場", "text": s + "。"})

    # 地合い
    flip = m.get("gex_flip")
    if flip:
        gap = a.forward - flip
        where = "下" if gap < 0 else "上"
        points.append({"label": "いまの位置", "text":
            f"切替ライン {_fmt(flip)}円 の {abs(gap):,.0f}円{where}にいる。"
            + ("この下では値動きが増幅されやすい。" if gap < 0 else "この上では値動きが収まりやすい。")})

    # 分かれ目
    lower = a.walls["lower"][0].strike if (a.walls and a.walls["lower"]) else None
    if flip and lower:
        points.append({"label": "分かれ目", "text":
            f"{_fmt(flip)}円 を回復すれば落ち着きやすい。逆に {_fmt(lower)}円 を割ると下げが加速しやすい。"})

    # 警戒度
    if m.get("rr25") is not None and p.get("rr25") is not None:
        d = m["rr25"] - p["rr25"]
        if d > 0.3:
            text = f"下落への警戒が強まった（{p['rr25']:+.2f} → {m['rr25']:+.2f}）。"
        elif d < -0.3:
            text = f"下落への警戒は和らいだ（{p['rr25']:+.2f} → {m['rr25']:+.2f}）。"
        else:
            text = f"下落への警戒はほぼ横ばい（{m['rr25']:+.2f}）。"
        points.append({"label": "警戒度", "text": text})

    # 保険の動き
    if decomp:
        pos, spot_t = decomp.position, decomp.spot_and_time
        if abs(pos) > TH_POSITION_DELTA:
            text = ("新しく下落保険が買われた（本物の積み増し）。"
                    if pos > 0 else "下落保険が外された。")
        elif abs(spot_t) > abs(pos):
            text = ("新しい売買はほとんど無く、相場が動いたことで"
                    + ("もともとの保険が効き始めただけ。" if spot_t > 0 else "保険の効き目が薄れただけ。"))
        else:
            text = "保険の量にめだった動きは無い。"
        points.append({"label": "保険の動き", "text": text})

    # はしご図に渡す水準
    upper = a.walls["upper"][0].strike if (a.walls and a.walls["upper"]) else None
    levels = []
    if upper:
        levels.append({"key": "wall_up", "value": upper,
                       "label": "上の壁", "note": "跳ね返されやすい水準"})
    if flip:
        levels.append({"key": "flip", "value": flip,
                       "label": "切替ライン", "note": "ここより下は増幅、上は収まる"})
    levels.append({"key": "now", "value": a.forward, "label": "いまここ", "note": "現在の水準"})
    if lower:
        levels.append({"key": "wall_down", "value": lower,
                       "label": "下の壁", "note": "支えられやすい水準"})
    levels.sort(key=lambda x: x["value"], reverse=True)
    for lv in levels:
        lv["display"] = _fmt(lv["value"])

    band = a.bands.band50 if a.bands else (None, None)
    return {
        "headline": headline,
        "sub": sub,
        "level": "warn" if short_gamma else "ok",
        "countdown": f"SQまであと {a.business_days}営業日（{a.sq.isoformat()}）",
        "points": points,
        "levels": levels,
        "flip": flip,
        "band50": [_fmt(band[0]), _fmt(band[1])],
        "disclaimer": (
            f"SQ値の予測ではない。9月11日のSQが {_fmt(band[0])}〜{_fmt(band[1])}円 に収まる確率を"
            "市場が50%と値付けしている、という意味。この幅は毎日変わる。"),
    }
