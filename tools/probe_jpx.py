#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JPXの清算値段CSVのオプション行と、建玉残高の公表先を調べる一時的な偵察スクリプト。"""
import csv
import io
import re
import sys
from collections import Counter

import requests

UA = "Mozilla/5.0 (compatible; market-dashboard/1.0; +https://github.com/komineriko/market-dashboard)"
BASE = "https://www.jpx.co.jp"
SETTLE_PAGE = "https://www.jpx.co.jp/markets/derivatives/settlement-price/index.html"
HREF = re.compile(r'href="([^"]+\.csv)"', re.I)
# href に限らず、HTML中のファイルらしき文字列を全部拾う
ANYFILE = re.compile(r'[\w/\-.]+\.(?:csv|zip|xls|xlsx|pdf|json)', re.I)

OI_PAGES = [
    ("日次相場情報", "https://www.jpx.co.jp/markets/statistics-derivatives/daily/index.html"),
    ("建玉残高", "https://www.jpx.co.jp/markets/derivatives/open-interest/index.html"),
    ("取引高", "https://www.jpx.co.jp/markets/statistics-derivatives/trading-volume/index.html"),
    ("参加者別(日次)", "https://www.jpx.co.jp/markets/derivatives/participant-volume/01.html"),
]


def get(url):
    return requests.get(url, headers={"User-Agent": UA}, timeout=90)


def decode(raw: bytes) -> str:
    for enc in ("utf-8", "cp932", "euc-jp"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp932", errors="replace")


def inspect_settlement() -> None:
    print(f"\n{'=' * 78}\n### 清算値段CSV のオプション行")
    r = get(SETTLE_PAGE)
    m = HREF.search(r.text)
    if not m:
        print("  CSVリンクが見つからない")
        return
    url = m.group(1)
    url = url if url.startswith("http") else BASE + url
    print(f"  {url}")
    raw = get(url).content
    rows = list(csv.reader(io.StringIO(decode(raw))))

    hidx = next((i for i, row in enumerate(rows) if row and row[0].strip() == "銘柄コード"), None)
    if hidx is None:
        print("  ヘッダ行が見つからない")
        return
    header = [c.strip() for c in rows[hidx]]
    print(f"  ヘッダ({len(header)}列): {header}")
    col = {name: i for i, name in enumerate(header)}

    def cell(row, name):
        i = col.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    opts, names, months, kinds, underlyings = [], Counter(), Counter(), Counter(), Counter()
    for row in rows[hidx + 1:]:
        if not row or len(row) < len(header) - 2:
            continue
        underlyings[cell(row, "原資産名称")] += 1
        if not cell(row, "権利行使価格"):
            continue
        opts.append(row)
        names[re.sub(r"\d+", "#", cell(row, "銘柄名称"))] += 1
        months[cell(row, "限月")] += 1
        kinds[cell(row, "PUT/CAL")] += 1

    print(f"  行使価格を持つ行: {len(opts):,}")
    print(f"  原資産名称の内訳(上位8): {underlyings.most_common(8)}")
    print(f"  銘柄名称のパターン(上位8): {names.most_common(8)}")
    print(f"  PUT/CAL の値: {kinds.most_common()}")
    print(f"  限月(上位8): {months.most_common(8)}")
    print("  -- 日経225オプションのサンプル --")
    shown = 0
    for row in opts:
        if cell(row, "原資産名称") != "日経225":
            continue
        print(f"      | {','.join(row)[:260]}")
        shown += 1
        if shown >= 10:
            break
    if shown == 0:
        print("      原資産名称が「日経225」の行が無い。サンプルを無条件で表示:")
        for row in opts[:6]:
            print(f"      | {','.join(row)[:260]}")


def hunt_open_interest() -> None:
    print(f"\n{'=' * 78}\n### 建玉残高の公表先を探す")
    for label, url in OI_PAGES:
        print(f"\n  --- {label}: {url}")
        try:
            r = get(url)
        except Exception as exc:                  # noqa: BLE001
            print(f"      取得失敗: {exc}")
            continue
        html = decode(r.content)
        print(f"      HTTP {r.status_code} / {len(r.content):,} bytes")
        hits = sorted({h for h in ANYFILE.findall(html)
                       if not h.lower().endswith((".css", ".js"))})
        data_like = [h for h in hits if re.search(r"\d{6,8}", h) or "-att/" in h]
        print(f"      ファイルらしき文字列 {len(hits)}件 / 日付を含むもの {len(data_like)}件")
        for h in (data_like or hits)[:15]:
            print(f"        {h}")
        # 「建玉」という語の近くにあるリンクを見る
        for m in re.finditer(r"建玉", html):
            seg = html[max(0, m.start() - 260): m.start() + 260]
            links = re.findall(r'href="([^"]+)"', seg)
            if links:
                print(f"        「建玉」近傍のリンク: {links[:4]}")
                break


def main() -> int:
    inspect_settlement()
    hunt_open_interest()
    return 0


if __name__ == "__main__":
    sys.exit(main())
