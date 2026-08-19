#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JPXの公表データの所在を調べる診断スクリプト（通常の更新では使わない）。

JPXはページをJSONインデックス経由で描画しており、ファイルの置き場所が
CMSの都合で変わる。取得が壊れたときに、どこに何があるかを調べ直すために使う。
GitHub Actions のジョブに一時的なステップとして差し込んで実行する想定。

2026-08 時点で分かっていること:
  * 行使価格別の建玉 → 大阪取引所日報 Daily_Report_OSE_YYYYMMDD.zip 内の siop_dyr_*.pdf
  * 清算値段・IV・原資産価格・金利・残日数 → 清算値段ページの rbYYYYMMDD.csv
  * 日次相場情報のページにはデータファイルが置かれていない
"""

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
