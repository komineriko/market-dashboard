#!/usr/bin/env python3
"""米国株オプション妙味スクリーニングを組み直して us_options.html に流し込む。

  python update_us_options.py              # 取得して更新
  python update_us_options.py --demo       # 合成データ（通信しない）
  python update_us_options.py --dry-run    # 書き込まず結果だけ表示
  python update_us_options.py --snapshot out.json   # 取得した板を保存
  python update_us_options.py --from-snapshot in.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone

import us_fetch as uf
import us_options as uo
import us_report as ur

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(HERE, "us_options.html")
START = "/* ===US_OPTIONS_DATA_START=== */"
END = "/* ===US_OPTIONS_DATA_END=== */"


def _without_timestamp(report):
    copy = json.loads(json.dumps(report))
    copy.get("meta", {}).pop("generated_at", None)
    return copy


def inject_html(report, path: str = HTML_PATH) -> bool:
    """差し込む。生成時刻以外が前回と同じなら書かずに False を返す。"""
    with open(path, encoding="utf-8") as f:
        html = f.read()
    i, j = html.index(START), html.index(END)

    current = html[i + len(START):j]
    try:
        a = current.index("{")
        b = current.rindex("}") + 1
        if _without_timestamp(json.loads(current[a:b])) == _without_timestamp(report):
            return False
    except (ValueError, json.JSONDecodeError):
        pass

    payload = json.dumps(report, ensure_ascii=False, indent=1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html[:i + len(START)] + "\nconst US_DATA = " + payload + ";\n" + html[j:])
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--date", help="基準日 YYYY-MM-DD。既定は板の日付")
    ap.add_argument("--limit", type=int, help="ユニバースの先頭N銘柄だけ")
    ap.add_argument("--snapshot", help="取得した板をこのファイルに保存")
    ap.add_argument("--from-snapshot", help="保存した板から組み直す")
    args = ap.parse_args()

    asof = date.fromisoformat(args.date) if args.date else None
    holdings = uf.load_holdings()
    rep = None

    if args.demo:
        import us_demo
        asof = asof or date(2026, 10, 9)
        unders = us_demo.build(asof)
        rep = uf.FetchReport(asof=asof, ok=[u.symbol for u in unders])
    elif args.from_snapshot:
        with open(args.from_snapshot, encoding="utf-8") as f:
            snap = json.load(f)
        unders = [uf.load_underlying(d) for d in snap["underlyings"]]
        asof = asof or date.fromisoformat(snap["asof"])
        rep = uf.FetchReport(asof=asof, ok=[u.symbol for u in unders])
    else:
        syms = uf.UNIVERSE[:args.limit] if args.limit else uf.UNIVERSE
        unders, rep = uf.load_universe(syms, asof=asof, holdings=holdings)
        asof = asof or rep.asof
        if not unders:
            print("板を1銘柄も取得できませんでした。", file=sys.stderr)
            return 1

    if args.snapshot:
        with open(args.snapshot, "w", encoding="utf-8") as f:
            json.dump({"asof": asof.isoformat(),
                       "underlyings": [uf.dump_underlying(u) for u in unders]},
                      f, ensure_ascii=False)
        print(f"板を {args.snapshot} に保存しました。")

    report = ur.build_report(unders, asof, rep, holdings)
    counts = report["summary"]["counts"]
    print(f"基準日 {asof} / 調査 {len(unders)}銘柄 / "
          f"コール買い {counts['long_call']} ブルプット {counts['bull_put']} "
          f"P売り {counts['csp']} カバコ {counts['covered_call']}")

    if args.dry_run:
        print(json.dumps(report, ensure_ascii=False, indent=1)[:4000])
        return 0

    if inject_html(report):
        print("us_options.html を更新しました。")
    else:
        print("前回と同じ内容のため、書き込みませんでした。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
