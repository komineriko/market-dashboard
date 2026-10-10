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
}

STRATEGY_LEADS = {
    "long_call": "上に伸びる前提。IVが割安なときだけ買う（IV/MAD ≤ "
                 f"{uo.TH_IV_CHEAP:.2f}）。損失は払ったプレミアムまで。",
    "bull_put": "下がらなければ勝ち。受け取りは小さいが、損失は幅で止まる"
                f"（受取÷最大損失 ≥ {uo.MIN_CREDIT_RATIO:.0%}）。",
    "csp": "下がったら買ってもいい水準でプットを売る。割り当てられたら現物を持つ。",
    "covered_call": "持っている株に上限をつけてプレミアムを取る。"
                    "上に抜けたら株は持っていかれる。",
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
            _f(t.spot),
            _f(t.dev_atr, 1, " ATR"),
            _pct(t.dev_pct),
            _f(t.rsi, 1),
            "—" if mom is None else f"{mom} / 5",
            t.gc_state,
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

def build_summary(buckets: Dict[str, List[uo.Candidate]], scanned: int,
                  blocked: int) -> Dict[str, Any]:
    counts = {k: len(v) for k, v in buckets.items()}
    total = sum(counts.values())

    bullish = counts["long_call"] + counts["bull_put"]
    premium = counts["csp"] + counts["covered_call"]
    if total == 0:
        headline = "今日は出せる候補がありません"
        level = "warn"
    elif bullish and bullish >= premium:
        headline = "買い方向の候補が中心です"
        level = "ok"
    elif premium:
        headline = "プレミアムを受け取る側の候補が中心です"
        level = "ok"
    else:
        headline = "候補は限定的です"
        level = "warn"

    def top(key: str) -> Optional[uo.Candidate]:
        return buckets[key][0] if buckets[key] else None

    points = []
    lc, bp, cs, cc = top("long_call"), top("bull_put"), top("csp"), top("covered_call")
    if lc:
        points.append({"label": "コール買いの一番手", "text":
                       f"{lc.symbol} {lc.expiry[5:]} {lc.legs[0].strike:,.0f}C。"
                       f"IV/MAD {lc.metrics['iv_ratio']:.2f} の割安。"
                       + (f"{_f(lc.metrics['target'])} まで届けば "
                          f"{_pct(lc.metrics.get('upside_pct'), 0)}。"
                          if lc.metrics.get("target") else "上値の目標は算出できず。")})
    if bp:
        points.append({"label": "ブルプットの一番手", "text":
                       f"{bp.symbol} {bp.legs[0].strike:,.0f}/{bp.legs[1].strike:,.0f}P。"
                       f"受取 {_money(bp.metrics['credit'])}、最大損失 "
                       f"{_money(bp.metrics['max_loss'])}、勝率目安 "
                       f"{bp.metrics['pop']:.0f}%。"})
    if cs:
        points.append({"label": "P売りの一番手", "text":
                       f"{cs.symbol} {cs.legs[0].strike:,.0f}P。実質取得単価 "
                       f"{_f(cs.metrics['effective_cost'])}（現値から "
                       f"-{abs(cs.metrics['cushion_pct']):.1f}%）、年率 "
                       f"{cs.metrics['annual_pct']:.1f}%。"})
    if cc:
        points.append({"label": "カバコの一番手", "text":
                       f"{cc.symbol} {cc.legs[0].strike:,.0f}C。受取 "
                       f"{_money(cc.metrics['premium'])}、コールされたときの総リターン "
                       f"{_pct(cc.metrics['called_return_pct'])}。"})
    if blocked:
        points.append({"label": "決算で除外", "text":
                       f"{blocked}銘柄。満期までに決算をまたぐ限月は、"
                       "方向もIVも読めないので最初から外している。"})

    return {
        "headline": headline,
        "level": level,
        "sub": f"{scanned}銘柄を調べて、4区分で合計 {total}件。"
               f"コール買い {counts['long_call']} / ブルプット {counts['bull_put']} / "
               f"P売り {counts['csp']} / カバコ {counts['covered_call']}。",
        "counts": counts,
        "points": points[:5],
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


def freshness(base: date) -> Dict[str, Any]:
    """次の更新予定。

    米国の引け（16:00 ET）後に回すので、基準日の次の営業日の板は
    その日の夜＝日本時間の翌朝に載る。
    """
    nxt = next_us_business_day(base)
    at = datetime(nxt.year, nxt.month, nxt.day, 22, 0, tzinfo=timezone.utc)
    jst = at.astimezone(JST)
    wd = "月火水木金土日"
    return {
        "next_board_date": nxt.isoformat(),
        "next_board_label": f"{nxt.month}/{nxt.day}",
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
    f"カバコ：50日線から {1.5:.1f}ATR以上の乖離 または MACDヒストの山越え ＋ "
    f"IV/MAD ≥ {uo.TH_IV_RICH:.2f} ＋ Δ {uo.CC_DELTA[0]:.2f}〜{uo.CC_DELTA[1]:.2f}",
]

DEFINITIONS = [
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
    "オプション出来高の日次ランキングは無料では取れないため、ユニバースは"
    "流動性のある銘柄を固定で持っている。日々の出来高の入れ替わりは追えていない。",
    "気配はスナップショットで、約定できる保証はない。スプレッドの広い銘柄は"
    "MIDで約定しない。手数料・金利・配当は計算に含めていない。",
    "IVは気配のMIDから自前で逆算している（r=0、フォワードはプット・コール・"
    "パリティ）。データ提供元のIV列は流動性の薄い行で壊れるため使わない。",
    "GEXは建玉×ガンマからの簡易推定で、実際のディーラーのポジションではない。",
    "決算日はデータ提供元の予定で、変更されることがある。決算日が分からなかった"
    "銘柄は、またぐかどうかを判定できないのでスクリーニング自体から外している"
    "（ETFは決算が無いので対象外）。",
]


def build_report(underlyings: Sequence[uo.Underlying], asof: date,
                 fetch_report: Any = None,
                 holdings: Optional[List[str]] = None,
                 top_per_section: int = 8) -> Dict[str, Any]:
    buckets: Dict[str, List[uo.Candidate]] = {k: [] for k, _ in uo.SCREENS}
    tech_rows: List[Dict[str, Any]] = []
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
        if picked:
            note = f"候補 {picked}件"
        elif not usable:
            # 条件に合う満期が1本も無い。ほぼ決算またぎ。
            note = "決算またぎで除外" if u.earnings else "対象の満期なし"
        else:
            note = "—"
        tech_rows.append(_tech_row(tech, u, iv, ratio, note))

    for key in buckets:
        buckets[key].sort(key=lambda c: c.rank, reverse=True)
        buckets[key] = buckets[key][:top_per_section]

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
