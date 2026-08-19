#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""取引参加者別 建玉残高xlsxの構造を調べる診断スクリプト。"""
import io
import json
import re
import sys

import requests
from openpyxl import load_workbook

UA = "Mozilla/5.0 (compatible; market-dashboard/1.0; +https://github.com/komineriko/market-dashboard)"
BASE = "https://www.jpx.co.jp"
YEARLIST = "/automation/markets/derivatives/open-interest/json/open_interest_yearlist.json"


def get(url):
    return requests.get(url, headers={"User-Agent": UA}, timeout=120)


def latest_entry():
    y = json.loads(get(BASE + YEARLIST).content.decode("utf-8"))
    path = y["TableDatas"][0]["Jsonfile"]
    data = json.loads(get(BASE + path).content.decode("utf-8"))
    return data["TableDatas"][0]


def dump_sheet(label, url, max_rows=26):
    print(f"\n{'=' * 78}\n### {label}\n{url}")
    r = get(url)
    print(f"  HTTP {r.status_code} / {len(r.content):,} bytes")
    if r.status_code != 200:
        return
    wb = load_workbook(io.BytesIO(r.content), read_only=True, data_only=True)
    print(f"  シート: {wb.sheetnames}")
    for ws in wb.worksheets[:2]:
        print(f"\n  --- シート「{ws.title}」 {ws.max_row}行 x {ws.max_column}列 ---")
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            cells = ["" if c is None else str(c) for c in row]
            if not any(cells):
                continue
            print(f"  {i:>3}| {' | '.join(cells)[:300]}")
            if i >= max_rows:
                print("  ...")
                break


def main() -> int:
    e = latest_entry()
    print(f"最新の公表日: {e.get('TradeDate')}")
    print(f"  収録: {[k for k in e if k != 'TradeDate']}")
    for key, label in (("IndexOptions", "日経225オプション 建玉残高(参加者別)"),
                       ("IndexFutures", "指数先物 建玉残高(参加者別)")):
        path = e.get(key)
        if path:
            dump_sheet(label, BASE + path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
