#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JPXのページ構成を調べるための一時的な偵察スクリプト。

開発環境からJPXに到達できないため、GitHub Actionsのランナーから実行して
実際のリンク構造をログに出す。取得層の実装が固まったら削除してよい。
"""
import re
import sys

import requests

UA = "Mozilla/5.0 (compatible; market-dashboard/1.0; +https://github.com/komineriko/market-dashboard)"

CANDIDATES = [
    ("日次相場情報", "https://www.jpx.co.jp/markets/statistics-derivatives/daily/index.html"),
    ("清算値段等", "https://www.jpx.co.jp/markets/derivatives/settlement-price/index.html"),
    ("建玉残高", "https://www.jpx.co.jp/markets/statistics-derivatives/open-interest/index.html"),
    ("参加者別取引状況", "https://www.jpx.co.jp/markets/derivatives/participant-volume/index.html"),
    ("先物・オプション相場表", "https://www.jpx.co.jp/markets/statistics-derivatives/index.html"),
]

HREF = re.compile(r'href="([^"]+)"', re.IGNORECASE)
INTERESTING = re.compile(r"\.(zip|csv|xls|xlsx|txt)$", re.IGNORECASE)


def probe(label: str, url: str) -> None:
    print(f"\n{'=' * 78}\n### {label}\n{url}")
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=45)
    except Exception as exc:                      # noqa: BLE001
        print(f"  取得失敗: {exc}")
        return
    print(f"  HTTP {r.status_code} / {len(r.content):,} bytes / {r.encoding}")
    if r.status_code != 200:
        print(f"  本文の先頭: {r.text[:200]!r}")
        return

    title = re.search(r"<title>(.*?)</title>", r.text, re.S | re.I)
    if title:
        print(f"  title: {title.group(1).strip()[:100]}")

    hrefs = HREF.findall(r.text)
    files = [h for h in hrefs if INTERESTING.search(h.split("?")[0])]
    print(f"  リンク総数 {len(hrefs)} / ファイル形式のリンク {len(files)}")
    for h in files[:40]:
        print(f"    FILE {h}")
    if not files:
        # ファイルが直接貼られていない場合、下位ページへの導線を探す
        subs = [h for h in hrefs if "/markets/" in h and h.endswith(".html")]
        seen = []
        for h in subs:
            if h not in seen:
                seen.append(h)
        for h in seen[:30]:
            print(f"    PAGE {h}")


def main() -> int:
    for label, url in CANDIDATES:
        probe(label, url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
