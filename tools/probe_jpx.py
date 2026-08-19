#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JPXがページ描画に使っているJSONインデックスを辿り、建玉残高の実ファイルを探す。"""
import json
import re
import sys

import requests

UA = "Mozilla/5.0 (compatible; market-dashboard/1.0; +https://github.com/komineriko/market-dashboard)"
BASE = "https://www.jpx.co.jp"

JSONS = [
    ("建玉残高", "/automation/markets/derivatives/open-interest/json/open_interest_yearlist.json"),
    ("参加者別手口", "/automation/markets/derivatives/participant-volume/json/participant_volume_monthly.json"),
    ("日報(月次一覧)", "/automation/markets/statistics-derivatives/daily/json/daily_report_monthlylist.json"),
]
PAGES_FOR_JS = [
    ("日次相場情報", "https://www.jpx.co.jp/markets/statistics-derivatives/daily/index.html"),
    ("建玉残高", "https://www.jpx.co.jp/markets/derivatives/open-interest/index.html"),
]


def get(url):
    return requests.get(url, headers={"User-Agent": UA}, timeout=90)


def decode(raw):
    for enc in ("utf-8", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp932", errors="replace")


def show_json(label, path):
    url = BASE + path
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
    print(f"  先頭: {text[:600]}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"  JSONとして読めない: {exc}")
        return None


def main() -> int:
    # 1) ページ内のJSでどのパスを組み立てているかを見る
    for label, url in PAGES_FOR_JS:
        print(f"\n{'=' * 78}\n### JS内のパス: {label}")
        try:
            html = decode(get(url).content)
        except Exception as exc:                   # noqa: BLE001
            print(f"  取得失敗: {exc}")
            continue
        for m in re.finditer(r'["\']([^"\']*(?:automation|json|att)[^"\']*)["\']', html):
            s = m.group(1)
            if len(s) > 8 and not s.endswith((".css", ".js", ".ico", ".png")):
                print(f"    {s}")
        for m in re.finditer(r"<script[^>]*src=\"([^\"]+)\"", html):
            if "automation" in m.group(1) or "derivativ" in m.group(1):
                print(f"    SCRIPT {m.group(1)}")

    # 2) JSONインデックスを辿る
    for label, path in JSONS:
        data = show_json(label, path)
        if data is None:
            continue
        # 中に現れるファイルパスらしき文字列を集める
        blob = json.dumps(data, ensure_ascii=False)
        files = sorted({m for m in re.findall(r'[\w/\-.]+\.(?:csv|zip|xls|xlsx|pdf)', blob)})
        print(f"  内包するファイル {len(files)}件")
        for f in files[:10]:
            print(f"    {f}")
        # 最新の1件を実際に取ってみる
        if files:
            target = files[-1]
            url = target if target.startswith("http") else BASE + ("" if target.startswith("/") else "/") + target
            print(f"  --- 実ファイルを確認: {url}")
            try:
                rr = get(url)
                print(f"      HTTP {rr.status_code} / {len(rr.content):,} bytes")
                if rr.status_code == 200 and not target.lower().endswith((".zip", ".pdf")):
                    for line in decode(rr.content).splitlines()[:12]:
                        print(f"      | {line[:250]}")
            except Exception as exc:               # noqa: BLE001
                print(f"      取得失敗: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
