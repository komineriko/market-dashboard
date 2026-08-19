#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""参加者別・投資部門別のデータが日次で取れるかを調べる診断スクリプト。"""
import json
import re
import sys

import requests

UA = "Mozilla/5.0 (compatible; market-dashboard/1.0; +https://github.com/komineriko/market-dashboard)"
BASE = "https://www.jpx.co.jp"


def get(url):
    return requests.get(url, headers={"User-Agent": UA}, timeout=90)


def decode(raw):
    for enc in ("utf-8", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp932", errors="replace")


def show_json(label, path, head=1400):
    url = BASE + path if path.startswith("/") else path
    print(f"\n{'=' * 78}\n### {label}\n{url}")
    try:
        r = get(url)
    except Exception as exc:                       # noqa: BLE001
        print(f"  取得失敗: {exc}")
        return None
    print(f"  HTTP {r.status_code} / {len(r.content):,} bytes")
    if r.status_code != 200:
        return None
    text = decode(r.content)
    print(f"  先頭:\n{text[:head]}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def scrape_js_paths(label, url):
    """ページのJSが組み立てているJSONパスを見る。"""
    print(f"\n{'=' * 78}\n### JS内のパス: {label}\n{url}")
    try:
        html = decode(get(url).content)
    except Exception as exc:                       # noqa: BLE001
        print(f"  取得失敗: {exc}")
        return
    seen = set()
    for m in re.finditer(r'["\'`]([^"\'`]*(?:automation|\.json|_json)[^"\'`]*)["\'`]', html):
        s = m.group(1).strip()
        if len(s) > 6 and s not in seen and not s.endswith((".css", ".js")):
            seen.add(s)
            print(f"    {s}")
    for m in re.finditer(r"`\$\{[^}]+\}([^`]+)`", html):
        print(f"    TEMPLATE ...{m.group(1)}")


def main() -> int:
    # 1) 参加者別: 日次のJSONがあるか
    scrape_js_paths("参加者別取引状況(日次)",
                    BASE + "/markets/derivatives/participant-volume/01.html")
    for name in ("participant_volume_daily", "participant_volume_day",
                 "participant_volume_dailylist", "participant_volume_yearlist"):
        show_json(f"参加者別 {name}",
                  f"/automation/markets/derivatives/participant-volume/json/{name}.json",
                  head=700)

    # 2) 建玉残高: 2026年のファイル一覧（頻度と命名を見る）
    data = show_json("建玉残高 2026年一覧",
                     "/automation/markets/derivatives/open-interest/json/open_interest_2026.json",
                     head=900)
    if data:
        blob = json.dumps(data, ensure_ascii=False)
        files = sorted({m for m in re.findall(r'[\w/\-.]+\.xlsx', blob)})
        nk = [f for f in files if "nk225op" in f]
        print(f"  ファイル総数 {len(files)} / 日経225オプション分 {len(nk)}")
        print("  日経225オプションの直近5件:")
        for f in nk[-5:]:
            print(f"    {f}")
        dates = sorted({re.search(r'(\d{8})', f).group(1) for f in nk if re.search(r'(\d{8})', f)})
        print(f"  日付の範囲: {dates[:3]} … {dates[-3:]}（{len(dates)}件）")

    # 3) 投資部門別
    scrape_js_paths("投資部門別取引状況",
                    BASE + "/markets/statistics-derivatives/sector/01.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
