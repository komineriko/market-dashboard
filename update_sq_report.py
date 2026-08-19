#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日経225オプション SQ予測レポート 自動更新スクリプト

JPXの日次相場情報からオプション板を取り、レポートを組み立てて
sq_report.html 内の
  /* ===AUTO_UPDATE_DATA_START=== */ ... /* ===AUTO_UPDATE_DATA_END=== */
に囲まれた const SQ_DATA = {...}; を書き換える。

前日比・実効デルタの3分解・答え合わせのために、日々のスナップショットを
sq_snapshots/ に残す（直近 KEEP_SNAPSHOTS 営業日ぶんだけ保持）。

使い方:
  python update_sq_report.py                     # JPXから取得して更新
  python update_sq_report.py --demo              # 合成データで動作確認（通信なし）
  python update_sq_report.py --chain-file a.xlsx # 手元の板ファイルを使う
  python update_sq_report.py --date 2026-08-19   # 基準日を指定
  python update_sq_report.py --dry-run           # ファイルを書かずに結果だけ表示

環境変数:
  SQ_CHAIN_FILE    板ファイルのパス（カンマ区切りで複数可）
  SQ_JPX_DAILY_URL JPX日次相場情報ファイルのURLを直接指定
  SQ_NIKKEI_VI     日経VI（自動取得できない場合の手入力）
  SQ_EVENTS_FILE   イベント一覧のJSON（既定: sq_events.json）
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from datetime import date, datetime, timedelta

import sq_analytics as sa
import sq_fetch as sf
import sq_report as sr

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(HERE, "sq_report.html")
SNAP_DIR = os.path.join(HERE, "sq_snapshots")
HISTORY_PATH = os.path.join(HERE, "sq_history.json")
EVENTS_PATH = os.environ.get("SQ_EVENTS_FILE", os.path.join(HERE, "sq_events.json"))

START = "/* ===AUTO_UPDATE_DATA_START=== */"
END = "/* ===AUTO_UPDATE_DATA_END=== */"

KEEP_SNAPSHOTS = 15      # 板の全量を残す日数。前日比に必要なのは1日ぶんだが、欠測に備える
KEEP_HISTORY = 400       # 指標の履歴（軽量）


# ---------------------------------------------------------------------------
# スナップショット
# ---------------------------------------------------------------------------

def snapshot_path(d: date) -> str:
    return os.path.join(SNAP_DIR, f"chain_{d.strftime('%Y%m%d')}.json")


def load_previous_snapshot(base: date) -> dict | None:
    """基準日より前で最も新しいスナップショットを返す。"""
    if not os.path.isdir(SNAP_DIR):
        return None
    best = None
    for path in glob.glob(os.path.join(SNAP_DIR, "chain_*.json")):
        m = re.search(r"chain_(\d{8})\.json$", path)
        if not m:
            continue
        d = datetime.strptime(m.group(1), "%Y%m%d").date()
        if d >= base:
            continue
        if best is None or d > best[0]:
            best = (d, path)
    if not best:
        return None
    with open(best[1], "r", encoding="utf-8") as f:
        return json.load(f)


def save_snapshot(snapshot: dict, base: date) -> None:
    os.makedirs(SNAP_DIR, exist_ok=True)
    with open(snapshot_path(base), "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, separators=(",", ":"))
    # 古いものを間引く
    files = sorted(glob.glob(os.path.join(SNAP_DIR, "chain_*.json")))
    for path in files[:-KEEP_SNAPSHOTS]:
        os.remove(path)


def load_history() -> list:
    if not os.path.exists(HISTORY_PATH):
        return []
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_history(history: list, base: date, metrics: dict) -> list:
    keep = ("forward", "atm_iv", "rr25", "bf25", "put25_iv", "call25_iv",
            "gex_at_atm", "gex_flip", "gex_regime", "put_effective_delta",
            "max_pain", "nikkei_vi", "spot_close", "sigma_abs", "cross_check")
    entry = {"date": base.isoformat()}
    entry.update({k: metrics.get(k) for k in keep})
    history = [h for h in history if h.get("date") != entry["date"]]
    history.append(entry)
    history.sort(key=lambda h: h.get("date") or "")
    history = history[-KEEP_HISTORY:]
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=1)
    return history


def load_events() -> list:
    if not os.path.exists(EVENTS_PATH):
        return []
    try:
        with open(EVENTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else data.get("events", [])
    except (json.JSONDecodeError, OSError):
        return []


# ---------------------------------------------------------------------------
# HTMLへの差し込み
# ---------------------------------------------------------------------------

def inject_html(report: dict, path: str = HTML_PATH) -> None:
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    i, j = html.find(START), html.find(END)
    if i < 0 or j < 0:
        raise SystemExit("ERROR: データ差し込み用の目印コメントが見つかりません。")
    payload = json.dumps(report, ensure_ascii=False, indent=1)
    block = f"{START}\nconst SQ_DATA = {payload};\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(html[:i] + block + html[j:])


# ---------------------------------------------------------------------------
# デモ用の合成板
# ---------------------------------------------------------------------------

def demo_chains(base: date) -> tuple[dict, sf.SpotQuote]:
    """通信せずに動作を確認するための合成データ。実勢とは無関係。"""
    import math
    smile = {1.05: .2824, 1.02: .2844, 1.00: .2913, 0.98: .3021, 0.95: .3268,
             0.92: .3471, 0.90: .3660, 0.85: .4200, 0.80: .4868, 0.75: .5563}
    f0 = 67260.0

    def sigma_of(k):
        m = k / f0
        ms = sorted(smile)
        if m <= ms[0]:
            return smile[ms[0]]
        if m >= ms[-1]:
            return smile[ms[-1]]
        for a, b in zip(ms, ms[1:]):
            if a <= m <= b:
                w = (m - a) / (b - a)
                return smile[a] * (1 - w) + smile[b] * w
        return 0.29

    chains = {}
    for idx, month in enumerate(sr.front_months(base, 3)):
        y, mo = sa.parse_contract_month(month)
        t = max((sa.sq_date_for(y, mo) - base).days, 1) / 365
        ch = sa.Chain(contract_month=month)
        k = 39000.0
        while k <= 83500.0:
            s = sigma_of(k)
            c = sa.bs_price(f0, k, t, s, True)
            p = sa.bs_price(f0, k, t, s, False)
            dist = abs(k - f0) / f0
            scale = 1.0 if idx == 0 else 0.25   # 当限を厚く、期先を薄く
            call_oi = int((2000 * math.exp(-((dist - .04) ** 2) / .002) if k > f0 else 200) * scale)
            put_oi = int((3000 * math.exp(-((dist - .03) ** 2) / .004) if k < f0 else 150) * scale)
            if k == 60000.0:
                put_oi += int(9000 * scale)
            if k == 67000.0:
                put_oi += int(2500 * scale)
            if k == 70000.0:
                call_oi += int(3500 * scale)
            ch.add(sa.StrikeRow(strike=k,
                                call_price=round(c) if c >= 1 else None,
                                put_price=round(p) if p >= 1 else None,
                                call_oi=call_oi, put_oi=put_oi))
            k += 250.0
        chains[month] = ch
    spot = sf.SpotQuote(close=67460.73, prev_close=69220.25, change=-1759.52,
                        change_pct=-2.54, as_of=base.isoformat(), source="デモ（合成データ）")
    return chains, spot


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="日経225オプション SQ予測レポートを更新する")
    ap.add_argument("--date", help="基準日 (YYYY-MM-DD)。既定は板データの日付、無ければ本日")
    ap.add_argument("--demo", action="store_true", help="合成データで動作確認（通信しない）")
    ap.add_argument("--chain-file", help="手元のオプション板ファイル")
    ap.add_argument("--dry-run", action="store_true", help="ファイルを書かない")
    args = ap.parse_args(argv)

    if args.chain_file:
        os.environ["SQ_CHAIN_FILE"] = args.chain_file

    base = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else None
    sources: list[str] = []

    if args.demo:
        base = base or date.today()
        chains, spot = demo_chains(base)
        origin = "デモ（合成データ・実勢とは無関係）"
        vi, vi_source = None, None
    else:
        months = sr.front_months(base or date.today(), 3)
        try:
            src = sf.load_chains(months=months, target=base)
        except sf.FetchError as exc:
            print(f"ERROR: オプション板の取得に失敗しました: {exc}", file=sys.stderr)
            return 1
        chains = src.chains
        origin = src.origin
        sources.append(origin)
        if base is None:
            base = src.as_of or date.today()

        spot = sf.fetch_nikkei_spot()
        if spot.ok:
            sources.append(f"日経平均: {spot.source}")
        else:
            print("WARN: 日経平均の終値を取得できませんでした。", file=sys.stderr)
        vi, vi_source = sf.fetch_nikkei_vi()
        if vi_source:
            sources.append(f"日経VI: {vi_source}")

    if not chains:
        print("ERROR: オプション板が空です。", file=sys.stderr)
        return 1

    prev = load_previous_snapshot(base)
    history = load_history()

    try:
        report, snapshot = sr.build_report(
            chains=chains, spot=spot, vi=vi, vi_source=vi_source, base=base,
            prev_snapshot=prev, history=history, events=load_events(),
            origin=origin, extra_sources=sources)
    except (ValueError, KeyError) as exc:
        print(f"ERROR: レポートの組み立てに失敗しました: {exc}", file=sys.stderr)
        return 1

    m = report["meta"]
    print(f"基準日 {m['base_date']} / SQ {m['sq_date']} / 残 {m['calendar_days']}暦日"
          f"・{m['business_days']}営業日 / {m['phase']}")
    print(f"限月 {m['month']} / 行使価格 {report['metrics']['strike_count']}本 / "
          f"F={report['metrics']['forward']} / ATM IV={report['metrics']['atm_iv']}")
    print(f"Net GEX at ATM={report['metrics']['gex_at_atm']}億 / "
          f"フリップ={report['metrics']['gex_flip']} / {report['metrics']['gex_regime']}")
    print(f"前日比: {'有効' if m['has_prev'] else '無し（次回から）'} / "
          f"開示 {len(report['disclosures'])}件 / 監視 {len(report['watch'])}件 / "
          f"答え合わせ {len(report['answer_check'])}件")

    if args.dry_run:
        print("--dry-run のためファイルは書き込みませんでした。")
        return 0

    inject_html(report)
    save_snapshot(snapshot, base)
    save_history(history, base, report["metrics"])
    print(f"更新しました: {os.path.relpath(HTML_PATH, HERE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
