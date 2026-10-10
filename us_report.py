"""米国株オプションのレポート組み立て。

数値はすべて us_options / us_indicators が出したものをそのまま使い、
ここでは並べ替えと表示の整形だけを行う。取得できなかったものは
「—」と出し、埋めない。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

import us_indicators as ui
import us_options as uo

JST = timezone(timedelta(hours=9))

STRATEGY_TITLES = {
    "long_call": "A. コール買い候補",
    "bull_put": "B. ブルプット・スプレッド候補",
    "csp": "C. キャッシュセキュアードP売り候補",
    "covered_call": "D. カバードコール候補",
    "long_put": "E. プット買い候補",
    "bear_call": "F. ベアコール・スプレッド候補",
    "calendar": "G. カレンダースプレッド候補",
}

STRATEGY_LEADS = {
    "long_call": "上に伸びる前提。IVが割安なときだけ買う（IV/MAD ≤ "
                 f"{uo.TH_IV_CHEAP:.2f}）。損失は払ったプレミアムまで。",
    "bull_put": "下がらなければ勝ち。受け取りは小さいが、損失は幅で止まる"
                f"（受取÷最大損失 ≥ {uo.MIN_CREDIT_RATIO:.0%}）。",
    "csp": "下がったら買ってもいい水準でプットを売る。割り当てられたら現物を持つ。",
    "covered_call": "持っている株に上限をつけてプレミアムを取る。"
                    "上に抜けたら株は持っていかれる。",
    "long_put": "下に向かう前提。IVが割安なときだけ買う（IV/MAD ≤ "
                f"{uo.TH_IV_CHEAP:.2f}）。損失は払ったプレミアムまで。",
    "bear_call": "上がらなければ勝ち。コール売りだが、損失は幅で止まる"
                 f"（受取÷最大損失 ≥ {uo.MIN_CREDIT_RATIO:.0%}）。",
    "calendar": "手前を売って後ろを買う。株価がその場に留まるほど得をする。"
                "手前のIVが後ろより高いとき（順ザヤ）がいちばん有利。",
}


def _f(v: Optional[float], digits: int = 2, suffix: str = "") -> str:
    if v is None:
        return "—"
    s = f"{v:,.{digits}f}"
    if s.startswith("-") and float(s.replace(",", "")) == 0:
        s = s[1:]
    return s + suffix


def _pct(v: Optional[float], digits: int = 1) -> str:
    return "—" if v is None else f"{v:+.{digits}f}%"


def _money(v: Optional[float]) -> str:
    return "—" if v is None else f"${v:,.0f}"


def _dte_label(c: uo.Candidate) -> str:
    md = c.expiry[5:].replace("-", "/")
    return f"{md}（{c.dte}日）"


def _quote(leg: uo.Leg) -> str:
    if leg.bid is None or leg.ask is None:
        return "—"
    return f"{leg.bid:,.2f} / {leg.ask:,.2f}"


# ---------------------------------------------------------------------------
# 各セクションの行
# ---------------------------------------------------------------------------

def _row_long_call(c: uo.Candidate) -> Dict[str, Any]:
    leg, m = c.legs[0], c.metrics
    beyond = m.get("beyond_wall")
    target = m.get("target")
    target_txt = "—" if target is None else (
        f"{target:,.2f}" + ("※" if beyond else ""))
    return {
        "symbol": c.symbol,
        "cells": [
            _dte_label(c),
            f"{leg.strike:,.2f} C",
            _quote(leg),
            _f(leg.price),
            _f(leg.delta),
            _f(leg.iv, 1, "%"),
            _f(m.get("iv_ratio")),
            f"{leg.oi:,}",
            _f(leg.spread_pct, 1, "%"),
            f"{_f(m.get('breakeven'))}（{_pct(m.get('breakeven_pct'))}）",
            target_txt,
            _f(m.get("at_call_wall")),
            _pct(m.get("upside_pct"), 0),
        ],
        "accent": m.get("upside_pct"),
    }


def _row_bull_put(c: uo.Candidate) -> Dict[str, Any]:
    short, long_leg = c.legs[0], c.legs[1]
    m = c.metrics
    return {
        "symbol": c.symbol,
        "cells": [
            _dte_label(c),
            f"{short.strike:,.2f} P",
            f"{long_leg.strike:,.2f} P",
            _money(m.get("credit")),
            _money(m.get("max_loss")),
            _f(m.get("credit_ratio")),
            f"{_f(m.get('breakeven'))}（-{abs(m.get('cushion_pct') or 0):.1f}%）",
            _f(m.get("pop"), 0, "%"),
            _f(m.get("iv_ratio")),
        ],
        "accent": m.get("credit_ratio"),
    }


def _row_csp(c: uo.Candidate) -> Dict[str, Any]:
    leg, m = c.legs[0], c.metrics
    return {
        "symbol": c.symbol,
        "cells": [
            _dte_label(c),
            f"{leg.strike:,.2f} P",
            _money(m.get("premium")),
            f"{_f(m.get('effective_cost'))}（-{abs(m.get('cushion_pct') or 0):.1f}%）",
            _f(m.get("annual_pct"), 1, "%"),
            _money(m.get("capital")),
            _f(m.get("pop"), 0, "%"),
            _f(m.get("iv_ratio")),
        ],
        "accent": m.get("annual_pct"),
    }


def _row_covered_call(c: uo.Candidate) -> Dict[str, Any]:
    leg, m = c.legs[0], c.metrics
    above = m.get("above_call_wall")
    wall = m.get("call_wall")
    wall_txt = "—" if wall is None else (
        f"{wall:,.2f}" + ("（上）" if above else "（下）"))
    return {
        "symbol": c.symbol,
        "cells": [
            _dte_label(c),
            f"{leg.strike:,.2f} C",
            _money(m.get("premium")),
            _f(m.get("annual_pct"), 1, "%"),
            _pct(m.get("called_return_pct")),
            _f(m.get("downside_buffer_pct"), 1, "%"),
            wall_txt,
            _f(m.get("iv_ratio")),
        ],
        "accent": m.get("called_return_pct"),
    }


def _row_long_put(c: uo.Candidate) -> Dict[str, Any]:
    leg, m = c.legs[0], c.metrics
    target = m.get("target")
    target_txt = "—" if target is None else (
        f"{target:,.2f}" + ("※" if m.get("beyond_wall") else ""))
    return {
        "symbol": c.symbol,
        "cells": [
            _dte_label(c),
            f"{leg.strike:,.2f} P",
            _quote(leg),
            _f(leg.price),
            _f(leg.delta),
            _f(leg.iv, 1, "%"),
            _f(m.get("iv_ratio")),
            f"{leg.oi:,}",
            _f(leg.spread_pct, 1, "%"),
            f"{_f(m.get('breakeven'))}（{_pct(m.get('breakeven_pct'))}）",
            target_txt,
            _f(m.get("at_call_wall")),
            _pct(m.get("upside_pct"), 0),
        ],
        "accent": m.get("upside_pct"),
    }


def _row_bear_call(c: uo.Candidate) -> Dict[str, Any]:
    short, long_leg = c.legs[0], c.legs[1]
    m = c.metrics
    return {
        "symbol": c.symbol,
        "cells": [
            _dte_label(c),
            f"{short.strike:,.2f} C",
            f"{long_leg.strike:,.2f} C",
            _money(m.get("credit")),
            _money(m.get("max_loss")),
            _f(m.get("credit_ratio")),
            f"{_f(m.get('breakeven'))}（+{abs(m.get('cushion_pct') or 0):.1f}%）",
            _f(m.get("pop"), 0, "%"),
            _f(m.get("iv_ratio")),
        ],
        "accent": m.get("credit_ratio"),
    }


def _row_calendar(c: uo.Candidate) -> Dict[str, Any]:
    sell, buy = c.legs[0], c.legs[1]
    m = c.metrics
    back = m.get("back_expiry") or ""
    back_label = (f"{back[5:].replace('-', '/')}（{m['back_expiry_dte']}日）"
                  if back else f"{m['back_expiry_dte']}日")
    return {
        "symbol": c.symbol,
        "cells": [
            _dte_label(c),
            back_label,
            f"{sell.strike:,.2f} C（{_pct(m.get('distance_pct'))}）",
            _money(sell.price * uo.MULTIPLIER),
            _money(buy.price * uo.MULTIPLIER),
            _money(m.get("debit")),
            _f(m.get("iv_front"), 1, "%"),
            _f(m.get("iv_back"), 1, "%"),
            _f(m.get("term_ratio")),
            "決算またぎ" if m.get("back_crosses_earnings") else "—",
        ],
        "accent": None,
    }


SECTION_SPEC = {
    "long_call": (
        ["満期（残）", "権利行使", "BID / ASK", "MID", "Δ", "IV", "IV/MAD",
         "OI", "スプレッド", "損益分岐（必要上昇）", "目標", "目標時", "伸びしろ"],
        _row_long_call,
    ),
    "bull_put": (
        ["満期（残）", "売る", "買う", "受取", "最大損失", "受取÷損失",
         "損益分岐（余裕）", "勝率目安", "IV/MAD"],
        _row_bull_put,
    ),
    "csp": (
        ["満期（残）", "権利行使", "受取", "実質取得単価（余裕）", "年率",
         "必要資金", "勝率目安", "IV/MAD"],
        _row_csp,
    ),
    "covered_call": (
        ["満期（残）", "権利行使", "受取", "年率", "コール時の総リターン",
         "下落バッファ", "CALL WALL", "IV/MAD"],
        _row_covered_call,
    ),
    "long_put": (
        ["満期（残）", "権利行使", "BID / ASK", "MID", "Δ", "IV", "IV/MAD",
         "OI", "スプレッド", "損益分岐（必要下落）", "目標", "目標時", "伸びしろ"],
        _row_long_put,
    ),
    "bear_call": (
        ["満期（残）", "売る", "買う", "受取", "最大損失", "受取÷損失",
         "損益分岐（余裕）", "勝率目安", "IV/MAD"],
        _row_bear_call,
    ),
    "calendar": (
        ["売る満期（残）", "買う満期（残）", "権利行使（現値差）", "受取", "支払",
         "ネット支払", "手前IV", "後ろIV", "手前÷後ろ", "備考"],
        _row_calendar,
    ),
}

# 候補が出なかったときに、何が足りなかったのかを書く
EMPTY_REASONS = {
    "long_call": f"IVが割安（IV/MAD ≤ {uo.TH_IV_CHEAP:.2f}）で"
                 "MACDが上抜けている銘柄が無かった。IVが高いまま買うと、"
                 "方向が当たってもIVの低下で負ける。",
    "bull_put": f"モメンタム{uo.TH_MOMENTUM_OK}点以上かつIVが割高"
                f"（IV/MAD ≥ {uo.TH_IV_RICH:.2f}）で、受取÷最大損失が"
                f"{uo.MIN_CREDIT_RATIO:.0%}を超える組み合わせが無かった。",
    "csp": f"モメンタム{uo.TH_MOMENTUM_OK}点以上かつIVが割高な銘柄で、"
           "条件に合うデルタ帯の気配が無かった。",
    "covered_call": "伸び切った、または勢いが細り始めた銘柄で、"
                    "IVが割高なものが無かった。",
    "long_put": f"IVが割安（IV/MAD ≤ {uo.TH_IV_CHEAP:.2f}）でMACDが下抜けている"
                "銘柄が無かった。下げ局面はIVが上がりやすく、"
                "割安なまま買える場面はそもそも多くない。",
    "bear_call": f"モメンタム{uo.TH_MOMENTUM_WEAK}以下かつMACDが下抜け、"
                 f"さらにIVが割高（IV/MAD ≥ {uo.TH_IV_RICH:.2f}）で"
                 f"受取÷最大損失が{uo.MIN_CREDIT_RATIO:.0%}を超える組み合わせが無かった。",
    "calendar": f"50日線からの乖離が{uo.CAL_MAX_DEV_ATR:.1f}ATR以内で、"
                f"手前のIVが後ろの{uo.CAL_MIN_TERM_RATIO:.2f}倍以上（順ザヤ）に"
                "なっている銘柄が無かった。逆ザヤのときに組むと、"
                "動かなくても負けやすい。",
}


# ---------------------------------------------------------------------------
# 銘柄メモ
# ---------------------------------------------------------------------------

def _tech_row(t: uo.Technicals, u: uo.Underlying, iv: Optional[float],
              ratio: Optional[float], note: str) -> Dict[str, Any]:
    mom = t.momentum.score if t.momentum else None
    return {
        "symbol": t.symbol,
        "cells": [
            _f(u.spot),
            _f(t.dev_atr, 1, " ATR"),
            _pct(t.dev_pct),
            _f(t.rsi, 1),
            "—" if mom is None else f"{mom} / 5",
            t.macd_state,
            _f(iv, 1, "%"),
            _f(t.hv20ex, 1, "%"),
            _f(t.mad_vol, 1, "%"),
            _f(ratio),
            u.earnings or "—",
            note,
        ],
        "iv_ratio": ratio,
    }


TECH_COLUMNS = ["現値", "乖離", "対50日線", "RSI", "モメンタム", "MACD",
                "ATM IV", "HV20 ギャップ除外", "MADボラ", "IV/MAD", "次回決算", "備考"]
TECH_ROW_CAP = 25


# ---------------------------------------------------------------------------
# まとめ
# ---------------------------------------------------------------------------

TALLY_LABELS = {
    "long_call": "コール買い",
    "bull_put": "ブルプット",
    "csp": "P売り",
    "covered_call": "カバコ",
    "long_put": "プット買い",
    "bear_call": "ベアコール",
    "calendar": "カレンダー",
}

# 方向の内訳。見出しをどちら寄りにするかの判定に使う。
BULLISH = ("long_call", "bull_put", "csp")
BEARISH = ("long_put", "bear_call")
NEUTRAL = ("calendar",)


def _point_for(key: str, c: uo.Candidate) -> Optional[Dict[str, str]]:
    """区分ごとの一番手を1行で書く。"""
    m = c.metrics
    label = TALLY_LABELS[key] + "の一番手"
    if key == "long_call":
        tail = (f"{_f(m['target'])} まで届けば {_pct(m.get('upside_pct'), 0)}。"
                if m.get("target") else "上値の目標は算出できず。")
        return {"label": label, "text":
                f"{c.symbol} {c.expiry[5:]} {c.legs[0].strike:,.0f}C。"
                f"IV/MAD {m['iv_ratio']:.2f} の割安。" + tail}
    if key == "long_put":
        tail = (f"{_f(m['target'])} まで下げれば {_pct(m.get('upside_pct'), 0)}。"
                if m.get("target") else "下値の目標は算出できず。")
        return {"label": label, "text":
                f"{c.symbol} {c.expiry[5:]} {c.legs[0].strike:,.0f}P。"
                f"IV/MAD {m['iv_ratio']:.2f} の割安。" + tail}
    if key in ("bull_put", "bear_call"):
        kind = "P" if key == "bull_put" else "C"
        return {"label": label, "text":
                f"{c.symbol} {c.legs[0].strike:,.0f}/{c.legs[1].strike:,.0f}{kind}。"
                f"受取 {_money(m['credit'])}、最大損失 {_money(m['max_loss'])}、"
                f"勝率目安 {m['pop']:.0f}%。"}
    if key == "csp":
        return {"label": label, "text":
                f"{c.symbol} {c.legs[0].strike:,.0f}P。実質取得単価 "
                f"{_f(m['effective_cost'])}（現値から -{abs(m['cushion_pct']):.1f}%）、"
                f"年率 {m['annual_pct']:.1f}%。"}
    if key == "covered_call":
        return {"label": label, "text":
                f"{c.symbol} {c.legs[0].strike:,.0f}C。受取 {_money(m['premium'])}、"
                f"コールされたときの総リターン {_pct(m['called_return_pct'])}。"}
    if key == "calendar":
        note = "（後ろは決算またぎ）" if m.get("back_crosses_earnings") else ""
        return {"label": label, "text":
                f"{c.symbol} {c.legs[0].strike:,.0f}C を {c.expiry[5:]}（{c.dte}日）で売り、"
                f"{(m.get('back_expiry') or '')[5:]}（{m['back_expiry_dte']}日）を買う。"
                f"ネット支払 {_money(m['debit'])}、"
                f"手前÷後ろ {m['term_ratio']:.2f}。{note}"}
    return None


def build_summary(buckets: Dict[str, List[uo.Candidate]], scanned: int,
                  blocked: int) -> Dict[str, Any]:
    counts = {k: len(v) for k, v in buckets.items()}
    total = sum(counts.values())
    bull = sum(counts.get(k, 0) for k in BULLISH)
    bear = sum(counts.get(k, 0) for k in BEARISH)
    neutral = sum(counts.get(k, 0) for k in NEUTRAL)

    if total == 0:
        headline, level = "今日は出せる候補がありません", "warn"
    elif bull > bear * 2 and bull > neutral:
        headline, level = "買い方向の候補が中心です", "ok"
    elif bear > bull * 2 and bear > neutral:
        headline, level = "売り方向の候補が中心です", "ok"
    elif neutral >= max(bull, bear):
        headline, level = "方向より「動かない」側の候補が中心です", "ok"
    else:
        headline, level = "買いと売りの候補が混在しています", "ok"

    points = []
    for key, _ in uo.SCREENS:
        if buckets.get(key):
            pt = _point_for(key, buckets[key][0])
            if pt:
                points.append(pt)
    if blocked:
        points.append({"label": "決算で除外", "text":
                       f"{blocked}銘柄。満期までに決算をまたぐ限月は、"
                       "方向もIVも読めないので最初から外している。"})

    parts = " / ".join(f"{TALLY_LABELS[k]} {counts.get(k, 0)}"
                       for k, _ in uo.SCREENS)
    return {
        "headline": headline,
        "level": level,
        "sub": f"{scanned}銘柄を調べて、{len(uo.SCREENS)}区分で合計 {total}件。{parts}。",
        "counts": counts,
        "tally": [{"key": k, "label": TALLY_LABELS[k], "n": counts.get(k, 0)}
                  for k, _ in uo.SCREENS],
        "direction": {"bull": bull, "bear": bear, "neutral": neutral},
        "points": points,
        "disclaimer": "テクニカル指標とオプション価格の定量スクリーニングであり、"
                      "投資助言ではありません。売買判断はご自身の責任で。",
    }


# ---------------------------------------------------------------------------
# 次の更新予定
# ---------------------------------------------------------------------------

US_HOLIDAYS = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}


def next_us_business_day(d: date) -> date:
    n = d + timedelta(days=1)
    while n.weekday() >= 5 or n in US_HOLIDAYS:
        n += timedelta(days=1)
    return n


PUBLISH_UTC_HOUR = 9        # ワークフローの cron と合わせること


def freshness(base: date) -> Dict[str, Any]:
    """次の更新予定。

    引けの直後だと日足の配信が間に合わないので、翌日の 09:00 UTC に回している。
    つまり立会日 D の分は D+1 の 09:00 UTC ＝ 日本時間 D+1 の 18:00 に載る。
    """
    nxt = next_us_business_day(base)
    pub = nxt + timedelta(days=1)
    at = datetime(pub.year, pub.month, pub.day, PUBLISH_UTC_HOUR, 0,
                  tzinfo=timezone.utc)
    jst = at.astimezone(JST)
    wd = "月火水木金土日"
    return {
        "next_board_date": nxt.isoformat(),
        "next_board_label": f"{nxt.month}/{nxt.day}",
        "publish_hour_utc": PUBLISH_UTC_HOUR,
        "next_update_at": at.isoformat(),
        "next_update_label": f"{jst.month}/{jst.day}({wd[jst.weekday()]}) "
                             f"{jst.hour:02d}:00 JST 頃",
    }


# ---------------------------------------------------------------------------
# 全体
# ---------------------------------------------------------------------------

FILTERS = [
    f"共通：満期まで {uo.MIN_DTE}〜{uo.MAX_DTE}日／決算をまたぐ限月は除外／"
    f"建玉 {uo.MIN_OI:,}枚以上・スプレッド {uo.MAX_SPREAD_PCT:.0f}%以内",
    f"コール買い：乖離 {uo.TH_OVEREXTENDED_ATR:.0f}ATR未満 ＋ MACD GC"
    f"（{uo.TH_GC_APPROACH_ATR}ATR以内の接近を含む） ＋ IV/MAD ≤ {uo.TH_IV_CHEAP:.2f}"
    f" ＋ Δ {uo.CALL_DELTA[0]:.2f}〜{uo.CALL_DELTA[1]:.2f}",
    f"ブルプット：モメンタム {uo.TH_MOMENTUM_OK}以上 ＋ MACD GC ＋ "
    f"IV/MAD ≥ {uo.TH_IV_RICH:.2f} ＋ 売るΔ {uo.BULLPUT_SHORT_DELTA[0]:.2f}〜"
    f"{uo.BULLPUT_SHORT_DELTA[1]:.2f}",
    f"P売り：モメンタム {uo.TH_MOMENTUM_OK}以上 ＋ IV/MAD ≥ {uo.TH_IV_RICH:.2f}"
    f" ＋ Δ {uo.CSP_DELTA[0]:.2f}〜{uo.CSP_DELTA[1]:.2f}",
    f"カバコ：50日線から {uo.CC_MIN_DEV_ATR:.1f}ATR以上の乖離 または MACDヒストの山越え ＋ "
    f"IV/MAD ≥ {uo.TH_IV_RICH:.2f} ＋ Δ {uo.CC_DELTA[0]:.2f}〜{uo.CC_DELTA[1]:.2f}",
    f"プット買い：乖離 -{uo.TH_OVEREXTENDED_ATR:.0f}ATR超 ＋ MACD DC"
    f"（{uo.TH_GC_APPROACH_ATR}ATR以内の接近を含む） ＋ IV/MAD ≤ {uo.TH_IV_CHEAP:.2f}"
    f" ＋ Δ -{uo.PUT_DELTA[1]:.2f}〜-{uo.PUT_DELTA[0]:.2f}",
    f"ベアコール：モメンタム {uo.TH_MOMENTUM_WEAK}以下 ＋ MACD DC ＋ "
    f"IV/MAD ≥ {uo.TH_IV_RICH:.2f} ＋ 売るΔ {uo.BEARCALL_SHORT_DELTA[0]:.2f}〜"
    f"{uo.BEARCALL_SHORT_DELTA[1]:.2f}",
    f"カレンダー：乖離 {uo.CAL_MAX_DEV_ATR:.1f}ATR以内 ＋ 手前IV÷後ろIV ≥ "
    f"{uo.CAL_MIN_TERM_RATIO:.2f} ＋ 後ろの限月は {uo.CAL_BACK_MIN_DTE}〜"
    f"{uo.CAL_BACK_MAX_DTE}日",
]

DEFINITIONS = [
    ("現値",
     "板のプット・コール・パリティ（F = K + C − P）から逆算した値。"
     "日足の配信は引けから数時間遅れることがあり、そのまま日足の終値を現値に"
     "すると板が織り込んでいる株価とずれる。乖離・RSI・MACDは完成した日足で"
     "計算しているので、日足が遅れている日はその旨をデータの出どころに書く。"),
    ("乖離：ATR換算",
     "単純な乖離率(%)ではボラティリティの高い銘柄が不当に「離れている」と判定される。"
     "乖離 ÷ ATR(14) で、ふだんの1日の値幅の何本分だけ50日線から離れているかを見る。"
     "ATR(14)は真の値幅の14日単純平均。"),
    ("モメンタム改善スコア（5点満点）",
     "①MACDヒストが前日比プラス ②2日連続で上昇 ③RSI(14)が3営業日前より上 "
     "④直近10日の安値を割っていない ⑤終値が直近5日レンジの上半分。"),
    ("MACDゴールデンクロス",
     "DIF＝EMA12−EMA26、DEA＝DIFの9日EMA。直近20本以内にDIFがDEAを下から上抜け、"
     "かつ現在もDIF>DEA。上抜けがゼロラインの下で起きた場合は「ゼロ下」と書く"
     "（ゼロ下は底打ちからの初動、ゼロ上はトレンド中の再加速）。"
     "まだDIFがDEAの下でも、差が0.5ATR以内でヒストが前日より上向きなら「GC接近」。"),
    ("IVが極端な銘柄の除外",
     f"ATM IV が MADボラの {uo.TH_IV_ABSURD:.0f}倍を超える銘柄は、"
     "うまみではなく「取り逃がしているイベント」か気配の異常を疑うべきなので、"
     "4区分すべてから外している。年率利回りで並べている以上、"
     "異常な気配ほど上位に来てしまうため。除外した銘柄はデータの出どころに書く。"),
    ("IV割高・割安（IV/MAD）",
     "ATM IV ÷ MADボラ。MADボラは日次対数リターンの中央絶対偏差を60日で取り、"
     "1.4826倍して年率換算した頑健な実現ボラ推定値。素のHV20は決算ギャップ1本に"
     "支配されるため使わない（参考として、絶対値最大の1本を落としたHV20も併記）。"
     f"{uo.TH_IV_RICH:.2f}以上で「割高＝売り有利」、{uo.TH_IV_CHEAP:.2f}以下で"
     "「割安＝買い有利」。"),
    ("CALL WALL / PUT WALL / ガンマフリップ",
     "行使価格ごとのガンマ×建玉を合算したGEXが最大・最小になる行使価格。"
     "満期までの全満期を合算している。ディーラーが「コール買い持ち・プット売り持ち」"
     "という慣例の仮定に依存しており、実際の建玉の向きは公開されていない。"),
    ("目標・目標時・伸びしろ",
     "目標はCALL WALL。CALL WALLが現値より下にある（すでに抜けている）場合は、"
     "現値より上でGEXが最も厚い行使価格に置き換え、※を付けている。"
     "「目標時」はそこへ届いた場合のプレミアムの想定で、満期時点の損益ではない。"
     "現在のMIDを起点に、株価が動いた分のBlack-Scholes理論値の差を足したもの。"
     "IVと残存日数は現在のままと仮定している（実際は上昇時にIVが下がりやすく、"
     "日数がかかれば時間価値も減るので、上限寄りの目安）。手数料は含まない。"),
    ("勝率目安",
     "1 − |Δ|。満期時点で権利行使されない確率のリスク中立での近似であり、"
     "実際の勝率ではない。"),
]

LIMITS = [
    "銘柄リスト（us_universe.txt）に載っている銘柄のうち、テクニカルの条件を"
    "どれか1つでも満たしたものだけ板を取りに行っている。板と決算は銘柄ごとの"
    "リクエストになるため、全銘柄ぶん取ると時間がかかりすぎるため。"
    "上限を超えた分は売買代金の小さい順に見送っている。",
    "気配はスナップショットで、約定できる保証はない。スプレッドの広い銘柄は"
    "MIDで約定しない。手数料・金利・配当は計算に含めていない。",
    "IVは気配のMIDから自前で逆算している（r=0、フォワードはプット・コール・"
    "パリティ）。データ提供元のIV列は流動性の薄い行で壊れるため使わない。",
    "GEXは建玉×ガンマからの簡易推定で、実際のディーラーのポジションではない。",
    "決算日はデータ提供元の予定で、変更されることがある。決算日が分からなかった"
    "銘柄は、またぐかどうかを判定できないのでスクリーニング自体から外している"
    "（ETFは決算が無いので対象外）。",
]


def _trim(cands: List[uo.Candidate], limit: int) -> List[uo.Candidate]:
    """上位を切り出す。ただし同じ銘柄で埋め尽くさない。

    1銘柄から満期違いで2本拾えるようにしているので、素直に順位で切ると
    「4銘柄が2本ずつ」になって選択肢が狭く見える。まず銘柄ごとの最上位を
    並べ、余った枠を2本目で埋める。
    """
    ordered = sorted(cands, key=lambda c: c.rank, reverse=True)
    best: List[uo.Candidate] = []
    extra: List[uo.Candidate] = []
    seen = set()
    for c in ordered:
        if c.symbol in seen:
            extra.append(c)
        else:
            seen.add(c.symbol)
            best.append(c)
    return (best + extra)[:limit]


def build_report(underlyings: Sequence[uo.Underlying], asof: date,
                 fetch_report: Any = None,
                 holdings: Optional[List[str]] = None,
                 top_per_section: int = 8) -> Dict[str, Any]:
    buckets: Dict[str, List[uo.Candidate]] = {k: [] for k, _ in uo.SCREENS}
    tech_rows: List[Dict[str, Any]] = []
    absurd: List[str] = []
    contracts = 0
    expiry_labels: List[str] = []

    for u in underlyings:
        tech, found = uo.screen_symbol(u, asof)
        for e in u.expiries:
            contracts += sum(1 for r in e.rows for q in (r.call, r.put) if q.oi > 0)
            if e.expiry not in expiry_labels and uo.MIN_DTE <= e.dte(asof) <= uo.MAX_DTE:
                expiry_labels.append(e.expiry)
        for key, cands in found.items():
            buckets[key].extend(cands)

        usable = uo.usable_expiries(u, asof)
        iv = ratio = None
        if usable:
            e0 = usable[0]
            t = e0.t(asof)
            f = uo.implied_forward(e0, u.spot, asof)
            iv = uo.atm_iv(e0, f, t)
            ratio = uo.iv_ratio(iv, tech)
        picked = sum(len(v) for v in found.values())
        if iv is not None and tech.mad_vol is None:
            note = "実現ボラを算出できず除外"
        elif ratio is not None and not uo.iv_is_sane(ratio):
            note = "IVが異常に高く除外"
            # 数字も一緒に出す。しきい値が妥当かを読む側が判断できるように。
            absurd.append(f"{u.symbol}（IV {iv:.0f}% ÷ MAD {tech.mad_vol:.0f}% "
                          f"= {ratio:.1f}）")
        elif picked:
            note = f"候補 {picked}件"
        elif not usable:
            # 条件に合う満期が1本も無い。ほぼ決算またぎ。
            note = "決算またぎで除外" if u.earnings else "対象の満期なし"
        else:
            note = "—"
        tech_rows.append(_tech_row(tech, u, iv, ratio, note))

    for key in buckets:
        buckets[key] = _trim(buckets[key], top_per_section)

    sections = []
    for key, _ in uo.SCREENS:
        cols, builder = SECTION_SPEC[key]
        rows = [builder(c) for c in buckets[key]]
        sections.append({
            "key": key,
            "title": STRATEGY_TITLES[key],
            "lead": STRATEGY_LEADS[key],
            "columns": cols,
            "rows": rows,
            "empty_reason": EMPTY_REASONS[key] if not rows else None,
        })

    tech_rows.sort(key=lambda r: (r["iv_ratio"] is None, r["iv_ratio"] or 0))
    tech_truncated = max(0, len(tech_rows) - TECH_ROW_CAP)
    tech_rows = tech_rows[:TECH_ROW_CAP]

    rep = fetch_report
    scanned = len(underlyings)
    blocked = len(getattr(rep, "earnings_blocked", []) or [])
    sources = []
    if rep is not None:
        src = getattr(rep, "universe_source", "")
        size = getattr(rep, "universe_size", 0)
        short = getattr(rep, "shortlisted", 0)
        capped = getattr(rep, "capped", 0)
        if src:
            line = f"銘柄リスト: {src}"
            if short:
                line += (f" → テクニカルで {short}銘柄に絞って板を取得")
                if capped:
                    line += (f"（条件は通ったが上限を超えた {capped}銘柄は"
                             "売買代金の小さい順に見送り）")
            sources.append(line)
        sources.append(f"日足・オプション板・決算予定: Yahoo Finance"
                       f"（{asof.isoformat()} の引け後のスナップショット）")
        if getattr(rep, "no_chain", None):
            sources.append("⚠ 板を取得できなかった銘柄: "
                           + "、".join(rep.no_chain[:12])
                           + ("…" if len(rep.no_chain) > 12 else ""))
        if getattr(rep, "no_bars", None):
            sources.append("⚠ 日足を取得できなかった銘柄: "
                           + "、".join(rep.no_bars[:12])
                           + ("…" if len(rep.no_bars) > 12 else ""))
        lag = getattr(rep, "bars_lag_days", 0) or 0
        if lag:
            sources.append(
                f"⚠ 日足の配信が板より {lag}日遅れている。現値は板のパリティから"
                "逆算した値を使い、乖離・RSI・MACDは1本前の日足で計算している")
        if absurd:
            sources.append(
                f"⚠ IVが実現ボラの{uo.TH_IV_ABSURD:.0f}倍を超えたため外した銘柄 "
                f"{len(absurd)}件: " + "、".join(absurd[:8])
                + ("…" if len(absurd) > 8 else ""))
        unknown = getattr(rep, "earnings_unknown", None) or []
        if unknown:
            sources.append(
                f"⚠ 決算日が分からず対象から外した銘柄 {len(unknown)}件: "
                + "、".join(unknown[:12]) + ("…" if len(unknown) > 12 else ""))
        if not getattr(rep, "earnings_available", True):
            sources.append("⚠ 決算予定をひとつも取得できなかった")

    holdings_note = (
        "保有銘柄リスト（us_holdings.json）が無いため、カバードコールは"
        "「もしその株を持っていれば」という前提で全銘柄から出している。"
        if holdings is None else
        f"カバードコールは保有銘柄 {len(holdings)} 本に絞っている。"
    )

    return {
        "meta": {
            "title": "米国株オプション 妙味スクリーニング",
            "base_date": asof.isoformat(),
            "base_weekday": "月火水木金土日"[asof.weekday()],
            "generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
            "scanned": scanned,
            "universe_size": getattr(fetch_report, "universe_size", 0) or None,
            "contracts": contracts,
            "expiries": sorted(expiry_labels),
            "horizon": f"満期まで {uo.MIN_DTE}〜{uo.MAX_DTE}日",
            "sources": sources,
            **freshness(asof),
        },
        "summary": build_summary(buckets, scanned, blocked),
        "filters": FILTERS,
        "definitions": [{"term": t, "body": b} for t, b in DEFINITIONS],
        "sections": sections,
        "tech": {"columns": TECH_COLUMNS, "rows": tech_rows,
                 "truncated": tech_truncated},
        "holdings_note": holdings_note,
        "limits": LIMITS,
    }
