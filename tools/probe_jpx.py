#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""株価指数オプション日報PDF(siop_dyr)の表構造を調べ、行使価格別建玉が取れるか確認する。"""
import io
import json
import sys
import zipfile

import requests

UA = "Mozilla/5.0 (compatible; market-dashboard/1.0; +https://github.com/komineriko/market-dashboard)"
BASE = "https://www.jpx.co.jp"


def get(url):
    return requests.get(url, headers={"User-Agent": UA}, timeout=180)


def check_publication_timing():
    """日報がいつ公開されるかを確認する（当日分が出ているか）。"""
    print(f"\n{'=' * 78}\n### 日報の公開タイミング")
    url = BASE + "/automation/markets/statistics-derivatives/daily/json/daily_report_202608.json"
    r = get(url)
    data = json.loads(r.content.decode("utf-8"))
    print(f"  JSONのUpdateDate: {data.get('UpdateDate')}")
    dates = [d.get("TradeDate") for d in data.get("TableDatas", [])][:4]
    print(f"  掲載されている直近の営業日: {dates}")
    for d in ("20260819", "20260818"):
        u = f"{BASE}/automation/markets/statistics-derivatives/daily/files/{d[:6]}/Daily_Report_OSE_{d}.zip"
        try:
            hr = requests.head(u, headers={"User-Agent": UA}, timeout=60)
            print(f"  {d}: HTTP {hr.status_code}")
        except Exception as exc:                   # noqa: BLE001
            print(f"  {d}: {exc}")


def inspect_siop_pdf(trade_date="20260818"):
    print(f"\n{'=' * 78}\n### 株価指数オプション日報 siop_dyr_{trade_date}.pdf")
    url = (f"{BASE}/automation/markets/statistics-derivatives/daily/files/"
           f"{trade_date[:6]}/Daily_Report_OSE_{trade_date}.zip")
    r = get(url)
    if r.status_code != 200:
        print(f"  ZIP取得失敗 HTTP {r.status_code}")
        return
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        name = next((n for n in zf.namelist() if n.startswith("siop_dyr_") and "flex" not in n), None)
        if not name:
            print(f"  siop_dyr が見つからない: {zf.namelist()}")
            return
        pdf_bytes = zf.read(name)
    print(f"  {name}: {len(pdf_bytes):,} bytes")

    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        print(f"  ページ数: {len(pdf.pages)}")
        # 日経225オプションのページを探す
        target_pages = []
        for i, page in enumerate(pdf.pages[:40]):
            text = page.extract_text() or ""
            if "日経225" in text or "NK225" in text:
                target_pages.append(i)
            if len(target_pages) >= 3:
                break
        print(f"  日経225を含むページ(先頭40ページ中): {target_pages}")

        for i in (target_pages[:2] or [0, 1]):
            page = pdf.pages[i]
            print(f"\n  ===== ページ {i} のテキスト（先頭40行） =====")
            for line in (page.extract_text() or "").splitlines()[:40]:
                print(f"  | {line[:250]}")
            tables = page.extract_tables()
            print(f"  --- 抽出できた表: {len(tables)}個")
            for t in tables[:1]:
                for row in t[:12]:
                    cells = ["" if c is None else str(c).replace("\n", " ") for c in row]
                    print(f"  T| {' | '.join(cells)[:250]}")


def main() -> int:
    check_publication_timing()
    try:
        inspect_siop_pdf()
    except Exception as exc:                       # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"  PDF解析で例外: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
