#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""建玉残高・日報のJSONを辿り、行使価格別の建玉が取れるファイルを特定する。"""
import io
import json
import re
import sys
import zipfile

import requests

UA = "Mozilla/5.0 (compatible; market-dashboard/1.0; +https://github.com/komineriko/market-dashboard)"
BASE = "https://www.jpx.co.jp"

TARGETS = [
    ("建玉残高 2026", "/automation/markets/derivatives/open-interest/json/open_interest_2026.json"),
    ("日報 202608", "/automation/markets/statistics-derivatives/daily/json/daily_report_202608.json"),
]
FILE_RE = re.compile(r'[\w/\-.]+\.(?:csv|zip|xlsx|xls|pdf)', re.I)


def get(url):
    return requests.get(url, headers={"User-Agent": UA}, timeout=120)


def decode(raw):
    for enc in ("utf-8", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp932", errors="replace")


def dump_data_file(url: str) -> None:
    print(f"\n  --- 実ファイル: {url}")
    try:
        r = get(url)
    except Exception as exc:                       # noqa: BLE001
        print(f"      取得失敗: {exc}")
        return
    print(f"      HTTP {r.status_code} / {len(r.content):,} bytes")
    if r.status_code != 200:
        return
    raw = r.content
    if raw[:2] == b"PK" and url.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            print(f"      ZIP内: {zf.namelist()[:10]}")
            inner = sorted(zf.namelist(), key=lambda n: zf.getinfo(n).file_size, reverse=True)
            if inner:
                raw = zf.read(inner[0])
                print(f"      展開: {inner[0]} ({len(raw):,} bytes)")
    if url.lower().endswith((".xlsx", ".xls")) or raw[:2] == b"PK":
        print("      Excel形式のため中身のダンプは省略")
        return
    lines = decode(raw).splitlines()
    print(f"      行数 {len(lines)}")
    for line in lines[:14]:
        print(f"      | {line[:260]}")
    hits = [l for l in lines if "225" in l and re.search(r"\d{4,6}", l)][:5]
    if hits:
        print("      -- 日経225らしき行 --")
        for h in hits:
            print(f"      | {h[:260]}")


def main() -> int:
    for label, path in TARGETS:
        url = BASE + path
        print(f"\n{'=' * 78}\n### {label}\n{url}")
        try:
            r = get(url)
        except Exception as exc:                   # noqa: BLE001
            print(f"  取得失敗: {exc}")
            continue
        print(f"  HTTP {r.status_code} / {len(r.content):,} bytes")
        if r.status_code != 200:
            continue
        text = decode(r.content)
        print(f"  先頭 1200文字:\n{text[:1200]}")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        blob = json.dumps(data, ensure_ascii=False)
        files = []
        for m in FILE_RE.findall(blob):
            if m not in files:
                files.append(m)
        print(f"\n  内包するファイル {len(files)}件（末尾5件）")
        for f in files[-5:]:
            print(f"    {f}")
        for f in files[-2:]:
            dump_data_file(f if f.startswith("http") else BASE + ("" if f.startswith("/") else "/") + f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
