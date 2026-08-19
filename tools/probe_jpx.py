#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JPXのページ構成とファイル形式を調べるための一時的な偵察スクリプト。
開発環境からJPXに到達できないため、GitHub Actionsのランナーから実行する。
"""
import re
import sys

import requests

UA = "Mozilla/5.0 (compatible; market-dashboard/1.0; +https://github.com/komineriko/market-dashboard)"
HREF = re.compile(r'href="([^"]+)"', re.IGNORECASE)
FILE_RE = re.compile(r"\.(zip|csv|xls|xlsx|txt)$", re.IGNORECASE)
BASE = "https://www.jpx.co.jp"

PAGES = [
    ("清算値段等", "https://www.jpx.co.jp/markets/derivatives/settlement-price/index.html"),
    ("建玉残高", "https://www.jpx.co.jp/markets/derivatives/open-interest/index.html"),
    ("オプション理論価格等情報", "https://www.jpx.co.jp/markets/derivatives/option-price/index.html"),
    ("参加者別取引状況(日次)", "https://www.jpx.co.jp/markets/derivatives/participant-volume/01.html"),
]


def get(url):
    return requests.get(url, headers={"User-Agent": UA}, timeout=60)


def decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp932", "euc-jp"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp932", errors="replace")


def find_files(html: str):
    out = []
    for h in HREF.findall(html):
        if FILE_RE.search(h.split("?")[0]):
            out.append(h if h.startswith("http") else BASE + (h if h.startswith("/") else "/" + h))
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def dump_csv(url: str, max_lines: int = 18) -> None:
    print(f"\n  --- 中身を確認: {url}")
    try:
        r = get(url)
    except Exception as exc:                      # noqa: BLE001
        print(f"      取得失敗: {exc}")
        return
    print(f"      HTTP {r.status_code} / {len(r.content):,} bytes")
    if r.status_code != 200:
        return
    # どのエンコーディングで素直に読めるか
    for enc in ("utf-8", "cp932"):
        try:
            r.content.decode(enc)
            print(f"      encoding: {enc} で解釈可能")
            break
        except UnicodeDecodeError:
            continue
    text = decode(r.content)
    lines = text.splitlines()
    print(f"      行数 {len(lines)}")
    for line in lines[:max_lines]:
        print(f"      | {line[:300]}")
    # 日経225オプションらしい行を探す
    hits = [l for l in lines if ("NK225E" in l or "日経225オプション" in l)][:6]
    if hits:
        print("      -- 日経225オプションらしい行 --")
        for h in hits:
            print(f"      | {h[:300]}")


def main() -> int:
    for label, url in PAGES:
        print(f"\n{'=' * 78}\n### {label}\n{url}")
        try:
            r = get(url)
        except Exception as exc:                  # noqa: BLE001
            print(f"  取得失敗: {exc}")
            continue
        print(f"  HTTP {r.status_code} / {len(r.content):,} bytes")
        if r.status_code != 200:
            continue
        files = find_files(decode(r.content))
        print(f"  ファイルリンク {len(files)}本")
        for u in files[:12]:
            print(f"    FILE {u}")
        for u in files[:2]:
            dump_csv(u)
    return 0


if __name__ == "__main__":
    sys.exit(main())
