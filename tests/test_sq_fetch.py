#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sq_fetch のパーサ検証。

JPXの実ファイルはこの環境から取得できないため、実際に出てきそうな形の
CSV/Excel を組み立てて、列順や形式が変わっても壊れないことを確認する。
"""

import io
import math
import os
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sq_analytics as sa
import sq_fetch as sf


# --- 長形式（JPXの日次相場情報に近い形） -----------------------------------

LONG_HEADER = "銘柄,限月,権利行使価格,プット・コール,始値,高値,安値,終値,清算値段,出来高,建玉"

LONG_ROWS = [
    # 先物行（行使価格を持たないので無視されるべき）
    "日経225先物,2026/09,,,67500,67800,67200,67300,67310,135710,52000",
    # 日経225オプション（採用）
    "日経225オプション,2026/09,67000,プット,1800,1900,1750,1870,1870,410,2816",
    "日経225オプション,2026/09,67000,コール,1200,1260,1150,1180,1180,56,1500",
    "日経225オプション,2026/09,70000,コール,900,950,880,905,905,120,3523",
    "日経225オプション,2026/09,70000,プット,3800,3900,3700,3810,3810,5,300",
    "日経225オプション,2026/09,60000,プット,340,360,330,340,340,80,9531",
    # 翌限月（monthsで絞れることの確認用）
    "日経225オプション,2026/10,67000,プット,2400,2500,2350,2450,2450,30,900",
    # ミニ・ウィークリー・他指数は除外されるべき
    "日経225ミニオプション,2026/09,67000,プット,1800,1900,1750,1870,1870,10,50",
    "TOPIXオプション,2026/09,3000,プット,50,55,48,52,52,20,1000",
]

WIDE_ROWS = [
    "225OP 建玉スナップショット 2026年9月限",
    "取得 2026/08/18 20:49:12",
    "",
    "出来高,建玉,現在値,権利行使価格,現在値,建玉,出来高",
    "56,1500,1180,67000,1870,2816,410",
    "120,3523,905,70000,3810,300,5",
    "3,200,180,74000,7100,60,0",
    "0,80,4200,60000,340,9531,80",
]


def to_cp932(lines):
    return ("\r\n".join(lines) + "\r\n").encode("cp932")


class TestLongFormat(unittest.TestCase):
    def setUp(self):
        self.raw = to_cp932([LONG_HEADER] + LONG_ROWS)

    def test_parses_nikkei_options_only(self):
        chains = sf.parse_option_chains(self.raw, "ose20260818.csv")
        self.assertIn("26-09", chains)
        ch = chains["26-09"]
        # ミニ・TOPIX・先物は入らない
        self.assertEqual(sorted(ch.strikes), [60000.0, 67000.0, 70000.0])

    def test_values_land_on_correct_side(self):
        ch = sf.parse_option_chains(self.raw)["26-09"]
        r = ch.rows[67000.0]
        self.assertEqual(r.put_oi, 2816)
        self.assertEqual(r.call_oi, 1500)
        self.assertEqual(r.put_price, 1870)
        self.assertEqual(r.call_price, 1180)
        self.assertEqual(r.put_volume, 410)
        self.assertEqual(ch.rows[60000.0].put_oi, 9531)

    def test_month_filter(self):
        chains = sf.parse_option_chains(self.raw, months=["26-09"])
        self.assertEqual(list(chains), ["26-09"])
        both = sf.parse_option_chains(self.raw)
        self.assertIn("26-10", both)

    def test_column_order_independence(self):
        """列を入れ替えても同じ結果になること。"""
        header = LONG_HEADER.split(",")
        order = [10, 3, 2, 1, 0, 8, 9, 4, 5, 6, 7]   # 建玉・種類・行使価格を先頭へ
        def reorder(line):
            cells = line.split(",")
            return ",".join(cells[i] for i in order)
        raw = to_cp932([reorder(LONG_HEADER)] + [reorder(r) for r in LONG_ROWS])
        ch = sf.parse_option_chains(raw)["26-09"]
        self.assertEqual(ch.rows[67000.0].put_oi, 2816)
        self.assertEqual(ch.rows[67000.0].call_price, 1180)

    def test_extra_unknown_columns_are_tolerated(self):
        raw = to_cp932([LONG_HEADER + ",ボラティリティ,理論価格,備考"]
                       + [r + ",29.13,1875," for r in LONG_ROWS])
        ch = sf.parse_option_chains(raw)["26-09"]
        self.assertEqual(ch.rows[67000.0].put_oi, 2816)

    def test_settle_falls_back_to_close(self):
        """清算値段が空でも終値で埋まること。"""
        rows = [r.split(",") for r in LONG_ROWS]
        for r in rows:
            if len(r) > 8:
                r[8] = ""      # 清算値段を空に
        raw = to_cp932([LONG_HEADER] + [",".join(r) for r in rows])
        ch = sf.parse_option_chains(raw)["26-09"]
        self.assertEqual(ch.rows[67000.0].put_price, 1870)   # 終値が使われる

    def test_missing_price_is_none_not_zero(self):
        """値なし建玉は None であること（0円と区別する）。"""
        rows = [r.split(",") for r in LONG_ROWS]
        for r in rows:
            if len(r) > 8 and r[2] == "60000":
                r[7] = r[8] = "-"
        raw = to_cp932([LONG_HEADER] + [",".join(r) for r in rows])
        ch = sf.parse_option_chains(raw)["26-09"]
        self.assertIsNone(ch.rows[60000.0].put_price)
        self.assertEqual(ch.rows[60000.0].put_oi, 9531)

    def test_zip_wrapper(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("ose20260818.csv", self.raw)
        ch = sf.parse_option_chains(buf.getvalue(), "ose20260818.zip")["26-09"]
        self.assertEqual(ch.rows[67000.0].put_oi, 2816)

    def test_utf8_input(self):
        raw = ("\n".join([LONG_HEADER] + LONG_ROWS)).encode("utf-8-sig")
        ch = sf.parse_option_chains(raw)["26-09"]
        self.assertEqual(ch.rows[67000.0].put_oi, 2816)

    def test_raises_when_unrecognisable(self):
        with self.assertRaises(sf.FetchError):
            sf.parse_option_chains(b"foo,bar\n1,2\n")


class TestWideFormat(unittest.TestCase):
    def test_wide_board(self):
        raw = to_cp932(WIDE_ROWS)
        chains = sf.parse_chains_any(raw, "225OP_OI_20260818.csv")
        self.assertIn("26-09", chains)
        ch = chains["26-09"]
        r = ch.rows[67000.0]
        self.assertEqual(r.call_oi, 1500)   # 左がCALL
        self.assertEqual(r.put_oi, 2816)    # 右がPUT
        self.assertEqual(r.call_price, 1180)
        self.assertEqual(r.put_price, 1870)
        self.assertEqual(r.call_volume, 56)
        self.assertEqual(r.put_volume, 410)

    def test_month_sniffed_from_title(self):
        self.assertEqual(sf.sniff_month([["225OP 2026年9月限"]]), "26-09")
        self.assertEqual(sf.sniff_month([["snapshot 26-11"]]), "26-11")
        self.assertIsNone(sf.sniff_month([["no month here"]]))

    def test_wide_chain_is_analysable(self):
        """横形式で読んだ板がそのまま分析エンジンに通ること。"""
        ch = sf.parse_chains_any(to_cp932(WIDE_ROWS))["26-09"]
        fwd = sa.implied_forward(ch, 67300)
        self.assertIsNotNone(fwd)
        # F = K + C - P = 67000 + 1180 - 1870 = 66310
        self.assertAlmostEqual(fwd.forward, 66310, delta=1200)


class TestMultiSheetExcel(unittest.TestCase):
    """限月ごとにシートが分かれた板ファイル（ユーザーが持っている形に近い）。"""

    def _workbook(self):
        from openpyxl import Workbook
        wb = Workbook()
        wb.remove(wb.active)
        data = {"26-09": (1500, 2816), "26-10": (900, 1400), "26-11": (400, 700)}
        for month, (call_oi, put_oi) in data.items():
            ws = wb.create_sheet(month)
            ws.append([f"225OP 建玉スナップショット {month}"])
            ws.append([])
            ws.append(["出来高", "建玉", "現在値", "権利行使価格", "現在値", "建玉", "出来高"])
            ws.append([56, call_oi, 1180, 67000, 1870, put_oi, 410])
            ws.append([120, 3523, 905, 70000, 3810, 300, 5])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def test_each_sheet_becomes_a_month(self):
        chains = sf.parse_chains_any(self._workbook(), "225OP_OI_20260818.xlsx")
        self.assertEqual(sorted(chains), ["26-09", "26-10", "26-11"])
        self.assertEqual(chains["26-09"].rows[67000.0].call_oi, 1500)
        self.assertEqual(chains["26-10"].rows[67000.0].call_oi, 900)
        self.assertEqual(chains["26-11"].rows[67000.0].put_oi, 700)

    def test_sides_are_not_swapped_across_sheets(self):
        chains = sf.parse_chains_any(self._workbook(), "board.xlsx")
        for month in chains:
            r = chains[month].rows[70000.0]
            self.assertEqual(r.call_price, 905)
            self.assertEqual(r.put_price, 3810)


SETTLE_HEADER = ("銘柄コード,銘柄名称,PUT/CAL,限月,権利行使価格,清算価格,理論価格,"
                 "原資産価格,ボラティリティ,金利,残日数,原資産名称")

SETTLE_ROWS = [
    "＊ 指数先物、国債先物、指数オプション順で表示しております。,,,,,,,,,,,",
    "＊ 前取引日に取引最終日を迎えた銘柄につきましては掲載されておりません。,,,,,,,,,,,",
    SETTLE_HEADER,
    # 先物（行使価格が無いので無視される）
    "161090018,FUT_225_260910,,202609,,65480,65480,65326.42,,1.1529,23,日経225",
    # 日経225オプション 標準限月（採用）
    "141330018,CAL_225_260910_67000,CAL,202609,67000,1180,1178,65326.42,28.5,1.1529,23,日経225",
    "141330019,PUT_225_260910_67000,PUT,202609,67000,1870,1868,65326.42,29.1,1.1529,23,日経225",
    "141330020,CAL_225_260910_70000,CAL,202609,70000,905,903,65326.42,28.0,1.1529,23,日経225",
    "141330021,PUT_225_260910_60000,PUT,202609,60000,340,339,65326.42,36.6,1.1529,23,日経225",
    # 翌限月
    "141340010,CAL_225_261009_67000,CAL,202610,67000,2100,2098,65326.42,29.5,1.2,52,日経225",
    # ウィークリー（最終売買日が違うので除外されるべき）
    "141350010,CAL_225W_260904_67000,CAL,202609,67000,300,299,65326.42,27.0,1.15,9,日経225",
    # 他の原資産（除外されるべき）
    "145000010,CAL_TPX_260910_3000,CAL,202609,3000,50,49,3020.5,18.0,1.15,23,TOPIX",
]


class TestSettlementCsv(unittest.TestCase):
    """JPX 清算値段CSV（rbYYYYMMDD.csv）の実形式にもとづく検証。"""

    def setUp(self):
        self.raw = to_cp932(SETTLE_ROWS)

    def test_extracts_standard_nikkei_options(self):
        chains, meta = sf.parse_settlement_csv(self.raw)
        self.assertIn("26-09", chains)
        self.assertEqual(sorted(chains["26-09"].strikes), [60000.0, 67000.0, 70000.0])

    def test_prices_land_on_correct_side(self):
        chains, _ = sf.parse_settlement_csv(self.raw)
        r = chains["26-09"].rows[67000.0]
        self.assertEqual(r.call_price, 1180)
        self.assertEqual(r.put_price, 1870)
        self.assertEqual(chains["26-09"].rows[60000.0].put_price, 340)
        self.assertIsNone(chains["26-09"].rows[60000.0].call_price)

    def test_futures_rows_are_ignored(self):
        chains, _ = sf.parse_settlement_csv(self.raw)
        # 先物は行使価格を持たないので板に入らない
        for ch in chains.values():
            self.assertTrue(all(k > 0 for k in ch.strikes))

    def test_weekly_and_other_underlyings_excluded(self):
        chains, _ = sf.parse_settlement_csv(self.raw)
        # ウィークリー(225W)を取り込んでいたら 67,000 CALL が 300 で上書きされる
        self.assertEqual(chains["26-09"].rows[67000.0].call_price, 1180)
        # TOPIX の行使価格 3,000 が混ざっていないこと
        self.assertNotIn(3000.0, chains["26-09"].rows)

    def test_month_filter(self):
        chains, _ = sf.parse_settlement_csv(self.raw, months=["26-09"])
        self.assertEqual(list(chains), ["26-09"])
        both, _ = sf.parse_settlement_csv(self.raw)
        self.assertIn("26-10", both)

    def test_meta_carries_underlying_rate_and_days(self):
        _, meta = sf.parse_settlement_csv(self.raw)
        m = meta["26-09"]
        self.assertAlmostEqual(m.underlying, 65326.42)
        self.assertAlmostEqual(m.rate_pct, 1.1529)
        self.assertEqual(m.days, 23)
        self.assertEqual(m.last_trading_day, "260910")

    def test_raises_when_no_nikkei_options(self):
        raw = to_cp932([SETTLE_HEADER,
                        "145000010,CAL_TPX_260910_3000,CAL,202609,3000,50,49,3020.5,18.0,1.15,23,TOPIX"])
        with self.assertRaises(sf.FetchError):
            sf.parse_settlement_csv(raw)

    def test_raises_when_columns_missing(self):
        raw = to_cp932(["銘柄コード,銘柄名称,限月", "1,CAL_225_260910_67000,202609"])
        with self.assertRaises(sf.FetchError):
            sf.parse_settlement_csv(raw)

    def test_discover_settlement_files(self):
        html = ('<a href="/markets/derivatives/settlement-price/tvdivq00000014l6-att/rb20260818.csv">18日</a>'
                '<a href="/markets/derivatives/settlement-price/tvdivq00000014l6-att/rb20260819.csv">19日</a>')
        files = sf.discover_settlement_files(html)
        self.assertEqual(files[0][0].isoformat(), "2026-08-19")
        self.assertTrue(files[0][1].startswith("https://www.jpx.co.jp/"))
        self.assertEqual(len(files), 2)


# 実際の siop_dyr PDF から抽出されたテキストの形に合わせたページ
SIOP_HEADER = [
    "※日経225オプション、日経225ミニオプションの場合、表示単位「Ｐ」は「円」に置き換え",
    "日経225オプション ※PriceforNikkei225Options,Nikkei225miniOptions:JPY,Others:points",
    "Nikkei225Options",
    "競争売買市場 AuctionMarket 2026年8月18日(火曜日)",
    "取引成立銘柄 TradeExecutedIssues Tuesday,August18,2026",
    "yyyymm mm.dd Ｐ Ｐ Ｐ Ｐ Ｐ Ｐ Ｐ Ｐ Ｐ ＰUnit 単位 ￥ 円 ＰUnit 単位 Unit 単位",
]

SIOP_PUT_PAGE = "\n".join(SIOP_HEADER + [
    "プットオプション PutOptions",
    # 前日比に符号が付き、トークン数が1つ増える行
    "202609 09.10 67,000 131091018 … … … … 1800.0000 1900.0000 1750.0000 1870.0000 + 70.0000 410 76,000 1870.00 … 2,816",
    # 中間がすべて「…」で列数が最小になる行
    "202609 09.10 60,000 181091518 … … … … … … … … … … … 340.00 … 9,531",
    # 建玉が「…」の行（清算値だけある）
    "202609 09.10 39,000 181091918 … … … … … … … … … … … 5.00 … …",
])

SIOP_PUT_PAGE2 = "\n".join(SIOP_HEADER + [
    # 見出しが無い継続ページ。PUTの状態が持ち越されること
    "202609 09.10 66,000 131093018 … … … … 1450.0000 1500.0000 1440.0000 1495.0000 + 45.0000 30 5,000 1495.00 … 2,407",
    "202610 10.09 66,000 131093019 … … … … … … … … … … … 2100.00 … 900",
])

SIOP_CALL_PAGE = "\n".join(SIOP_HEADER + [
    "コールオプション CallOptions",
    "202609 09.10 70,000 131094018 … … … … 900.0000 910.0000 890.0000 905.0000 + 5.0000 120 1,000 905.00 … 3,523",
])

# 日経225ミニオプションのページ（タイトル行が別なので取り込まれてはいけない）
SIOP_MINI_PAGE = "\n".join([
    "※日経225オプション、日経225ミニオプションの場合、表示単位「Ｐ」は「円」に置き換え",
    "日経225ミニオプション ※PriceforNikkei225Options,Nikkei225miniOptions:JPY,Others:points",
    "Nikkei225miniOptions",
    "コールオプション CallOptions",
    "202609 09.10 67,000 999999018 … … … … … … … … … … … 1180.00 … 5,000",
])


# J-NET市場の表。清算価格も建玉も列が無く、末尾は 取引高・取引金額。
SIOP_JNET_PAGE = "\n".join([
    "※日経225オプション、日経225ミニオプションの場合、表示単位「Ｐ」は「円」に置き換え",
    "日経225オプション ※PriceforNikkei225Options,Nikkei225miniOptions:JPY,Others:points",
    "Nikkei225Options",
    "J-NET市場 J-NETMarket 2026年8月18日(火曜日)",
    "プットオプション PutOptions",
    "202609 09.10 48,000 131218018 27.0100 47.3300 27.0100 47.3300 1,203 33,716,010",
    "202609 09.10 56,000 181216018 94.9000 143.9000 94.9000 143.9000 11 1,271,690",
    # 取引金額が小さく、桁の上限では弾けない行
    "202609 09.10 57,000 181217018 110.7200 176.9000 110.7200 176.9000 5 512,340",
])


class TestSiopDailyReport(unittest.TestCase):
    """大阪取引所日報（株価指数オプション）から行使価格別の建玉を取り出す。"""

    def parse(self, pages, months=None):
        return sf.parse_siop_pages(pages, months)

    def test_extracts_open_interest_from_last_column(self):
        chains = self.parse([SIOP_PUT_PAGE])
        r = chains["26-09"].rows[67000.0]
        self.assertEqual(r.put_oi, 2816)
        self.assertEqual(r.put_price, 1870.0)

    def test_handles_variable_column_count(self):
        """前日比の符号でトークン数が変わっても右端から読めていること。"""
        chains = self.parse([SIOP_PUT_PAGE])
        self.assertEqual(chains["26-09"].rows[60000.0].put_oi, 9531)
        self.assertEqual(chains["26-09"].rows[60000.0].put_price, 340.0)

    def test_missing_open_interest_is_zero_but_price_kept(self):
        chains = self.parse([SIOP_PUT_PAGE])
        r = chains["26-09"].rows[39000.0]
        self.assertEqual(r.put_oi, 0)
        self.assertEqual(r.put_price, 5.0)

    def test_put_state_carries_across_pages(self):
        chains = self.parse([SIOP_PUT_PAGE, SIOP_PUT_PAGE2])
        self.assertEqual(chains["26-09"].rows[66000.0].put_oi, 2407)
        self.assertEqual(chains["26-09"].rows[66000.0].call_oi, 0)
        self.assertIn("26-10", chains)
        self.assertEqual(chains["26-10"].rows[66000.0].put_oi, 900)

    def test_call_section_switches_side(self):
        chains = self.parse([SIOP_PUT_PAGE, SIOP_CALL_PAGE])
        r = chains["26-09"].rows[70000.0]
        self.assertEqual(r.call_oi, 3523)
        self.assertEqual(r.call_price, 905.0)
        self.assertEqual(r.put_oi, 0)

    def test_mini_options_are_excluded(self):
        """注記行に「日経225ミニオプション」が出るため、行頭判定が効いていること。"""
        chains = self.parse([SIOP_PUT_PAGE, SIOP_MINI_PAGE])
        # ミニの 67,000 CALL 5,000枚 を取り込んでいないこと
        self.assertEqual(chains["26-09"].rows[67000.0].call_oi, 0)

    def test_nikkei_page_is_not_dropped_by_the_disclaimer(self):
        """注記行の存在だけで本体ページが落ちていないこと（実際に踏んだ不具合）。"""
        chains = self.parse([SIOP_PUT_PAGE])
        self.assertTrue(chains, "日経225オプションのページが除外されてしまっている")

    def test_month_filter(self):
        chains = self.parse([SIOP_PUT_PAGE, SIOP_PUT_PAGE2], months=["26-09"])
        self.assertEqual(list(chains), ["26-09"])

    def test_header_lines_are_not_parsed_as_data(self):
        chains = self.parse([SIOP_PUT_PAGE])
        # 「yyyymm mm.dd …」のヘッダ行が行使価格として入っていないこと
        self.assertEqual(sorted(chains["26-09"].strikes), [39000.0, 60000.0, 67000.0])

    def test_multiple_records_on_one_line(self):
        """1行に複数レコードが並ぶページでも、各レコードを取り出せること。"""
        page = "\n".join(SIOP_HEADER + [
            "プットオプション PutOptions",
            "202609 09.10 67,000 131091018 … … … … … … … … … … … 1870.00 … 2,816 "
            "202609 09.10 66,000 131093018 … … … … … … … … … … … 1495.00 … 2,407 "
            "202609 09.10 65,000 131094018 … … … … … … … … … … … 1145.00 … 3,553",
        ])
        chains = self.parse([page])
        self.assertEqual(chains["26-09"].rows[67000.0].put_oi, 2816)
        self.assertEqual(chains["26-09"].rows[66000.0].put_oi, 2407)
        self.assertEqual(chains["26-09"].rows[65000.0].put_oi, 3553)

    def test_column_shift_to_trading_value_is_not_ingested(self):
        """
        列がずれて末尾が取引金額（円）になった行を取り込まないこと。
        これを許すと建玉に桁違いの値が紛れ込む（実データで踏んだ）。

        なおこの並びは「清算価格・整数・整数」の形自体は満たしてしまうので、
        形の検証だけでは捕まらず、桁の上限で弾かれる。検証が二段構えである理由。
        """
        page = "\n".join(SIOP_HEADER + [
            "プットオプション PutOptions",
            "202609 09.10 64,000 131095018 … … … … 900.0000 910.0000 890.0000 905.0000 + 5.0000 1870.00 201 1,234,567,000",
        ])
        st = sf.SiopStats()
        chains = sf.parse_siop_pages([page], stats=st)
        self.assertEqual(st.accepted, 0, "桁違いの建玉が取り込まれている")
        self.assertEqual(st.rejected_oi, 1)
        self.assertNotIn("26-09", chains)

    def test_shape_violation_is_rejected(self):
        """清算価格の位置に小数点を持たない値が来る並びは、形の検証で弾く。"""
        page = "\n".join(SIOP_HEADER + [
            "プットオプション PutOptions",
            # 末尾が 取引高 → 取引金額 → 建玉 の順にずれ、-3 が整数になっている
            "202609 09.10 62,000 131097018 … … … … … … … … … 201 76,000 555",
        ])
        st = sf.SiopStats()
        chains = sf.parse_siop_pages([page], stats=st)
        self.assertEqual(st.accepted, 0)
        self.assertEqual(st.rejected_shape, 1)
        self.assertNotIn("26-09", chains)

    def test_implausible_open_interest_is_rejected(self):
        page = "\n".join(SIOP_HEADER + [
            "プットオプション PutOptions",
            "202609 09.10 63,000 131096018 … … … … … … … … … … … 755.00 … 987,654,321",
        ])
        st = sf.SiopStats()
        sf.parse_siop_pages([page], stats=st)
        self.assertEqual(st.rejected_oi, 1)
        self.assertEqual(st.accepted, 0)

    def test_stats_count_pages_and_records(self):
        st = sf.SiopStats()
        sf.parse_siop_pages([SIOP_PUT_PAGE, SIOP_MINI_PAGE], stats=st)
        self.assertEqual(st.pages, 2)
        self.assertEqual(st.pages_nikkei, 1)
        self.assertGreater(st.accepted, 0)

    def test_totals_are_plausible(self):
        """建玉の合計が現実的な桁に収まること。"""
        chains = self.parse([SIOP_PUT_PAGE, SIOP_CALL_PAGE])
        total = chains["26-09"].total_oi("put") + chains["26-09"].total_oi("call")
        self.assertLess(total, 1_000_000, "建玉の桁がおかしい（列ずれの疑い）")

    def test_jnet_section_is_excluded(self):
        """
        J-NET市場の表は清算価格も建玉も持たない。取り込むと取引金額（円）を
        建玉として拾う（実データで踏んだ）。桁の上限では小さい金額を弾けないので、
        市場の見出しで切り分ける必要がある。
        """
        st = sf.SiopStats()
        chains = sf.parse_siop_pages([SIOP_JNET_PAGE], stats=st)
        self.assertEqual(st.accepted, 0, "J-NET市場の行を取り込んでいる")
        self.assertEqual(st.pages_jnet, 1)
        self.assertEqual(chains, {})

    def test_auction_and_jnet_mixed(self):
        """競争売買市場のページだけを採り、J-NETのページは飛ばすこと。"""
        st = sf.SiopStats()
        chains = sf.parse_siop_pages([SIOP_PUT_PAGE, SIOP_JNET_PAGE, SIOP_CALL_PAGE],
                                     stats=st)
        self.assertEqual(chains["26-09"].rows[67000.0].put_oi, 2816)
        self.assertEqual(chains["26-09"].rows[70000.0].call_oi, 3523)
        # J-NETの 48,000 / 56,000 / 57,000 が混ざっていないこと
        for k in (48000.0, 56000.0, 57000.0):
            self.assertNotIn(k, chains["26-09"].rows, f"J-NETの {k:,.0f} が混入している")

    def test_result_feeds_the_analytics_engine(self):
        chains = self.parse([SIOP_PUT_PAGE, SIOP_CALL_PAGE])
        ch = chains["26-09"]
        self.assertGreater(ch.total_oi("put"), 0)
        self.assertGreater(ch.total_oi("call"), 0)


# 実際の indexfut_oi_by_tp.xlsx から読み取った並び（2026-08-14 時点）
FUT_PARTICIPANT_ROWS = [
    ["指数先物取引参加者別建玉残高"] + [""] * 20,
    ["（ 2026年08月14日現在 ）"] + [""] * 20,
    ["2026年08月17日"] + [""] * 20,
    ["", "", "", "", "", "", "", "", "", "", "", "", "", "", "株式会社大阪取引所"] + [""] * 6,
    ["＜日経225先物＞"] + [""] * 20,
    ["", "", "（売超参加者）", "", "", "（買超参加者）", "", "", "", "", "",
     "", "（売超参加者）", "", "", "（買超参加者）", "", "", "", "", ""],
    ["1", "2026年09月限月", "12724", "ＨＳＢＣ証券", "33442.0", "12400", "野村証券", "35374.0",
     "", "", "1", "2026年12月限月", "11696", "みずほ証券", "2654.0", "11788", "ソシエテＧ証券",
     "2649.0", "", "", ""],
    ["3", "2026年09月限月", "11560", "ゴールドマン証券", "12863.0", "11714", "ＪＰモルガン証券",
     "6181.0", "", "", "3", "2026年12月限月", "11792", "シティグループ証券", "1568.0",
     "12400", "野村証券", "1748.0", "", "", ""],
    # 片側だけ埋まっている行
    ["13", "2026年09月限月", "11512", "光世証券", "80.0", "12336", "日産証券", "85.0",
     "", "", "13", "2026年12月限月", "", "", "", "12330", "マネックス証券", "9.0", "", "", ""],
    ["", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
    ["＜日経225mini＞"] + [""] * 20,
    ["", "", "（売超参加者）", "", "", "（買超参加者）", "", "", "", "", "",
     "", "（売超参加者）", "", "", "（買超参加者）", "", "", "", "", ""],
    ["1", "2026年09月限月", "12410", "バークレイズ証券", "65824.0", "12479",
     "ＡＢＮクリアリン証券", "34314.0", "", "", "1", "2026年10月限月", "12479",
     "ＡＢＮクリアリン証券", "4047.0", "11560", "ゴールドマン証券", "3000.0", "", "", ""],
]


class TestParticipantOpenInterest(unittest.TestCase):
    """取引参加者別建玉残高（週次）の抽出。"""

    def setUp(self):
        self.rows = sf.parse_participant_rows(FUT_PARTICIPANT_ROWS)
        self.by_key = {(r.product, r.month, r.name): r for r in self.rows}

    def test_product_sections_are_separated(self):
        products = {r.product for r in self.rows}
        self.assertEqual(products, {"日経225先物", "日経225mini"})

    def test_sell_and_buy_sides(self):
        hsbc = self.by_key[("日経225先物", "26-09", "ＨＳＢＣ証券")]
        self.assertEqual(hsbc.sell, 33442.0)
        self.assertEqual(hsbc.buy, 0.0)
        self.assertEqual(hsbc.net, -33442.0)
        nomura = self.by_key[("日経225先物", "26-09", "野村証券")]
        self.assertEqual(nomura.buy, 35374.0)
        self.assertEqual(nomura.net, 35374.0)

    def test_right_hand_block_is_a_different_month(self):
        """1行の右側ブロックは別の限月。取り違えないこと。"""
        mizuho = self.by_key[("日経225先物", "26-12", "みずほ証券")]
        self.assertEqual(mizuho.sell, 2654.0)
        self.assertNotIn(("日経225先物", "26-09", "みずほ証券"), self.by_key)

    def test_half_filled_row(self):
        """売超が空で買超だけある行を落とさないこと。"""
        monex = self.by_key[("日経225先物", "26-12", "マネックス証券")]
        self.assertEqual(monex.buy, 9.0)
        self.assertEqual(monex.sell, 0.0)

    def test_same_participant_appears_in_multiple_products(self):
        gs_fut = self.by_key[("日経225先物", "26-09", "ゴールドマン証券")]
        gs_mini = self.by_key[("日経225mini", "26-10", "ゴールドマン証券")]
        self.assertEqual(gs_fut.net, -12863.0)
        self.assertEqual(gs_mini.net, 3000.0)

    def test_aggregate_converts_mini_to_large(self):
        agg = dict((name, net) for name, net, _ in sf.aggregate_participant_net(self.rows))
        # ゴールドマン: 先物 -12,863 ＋ mini +3,000×0.1 = -12,563
        self.assertAlmostEqual(agg["ゴールドマン証券"], -12563.0, places=3)
        # バークレイズ: mini の売超 65,824×0.1 = -6,582.4
        self.assertAlmostEqual(agg["バークレイズ証券"], -6582.4, places=3)

    def test_aggregate_is_sorted_by_net(self):
        agg = sf.aggregate_participant_net(self.rows)
        nets = [net for _, net, _ in agg]
        self.assertEqual(nets, sorted(nets, reverse=True))
        self.assertGreater(nets[0], 0)
        self.assertLess(nets[-1], 0)

    def test_month_filter(self):
        agg = dict((n, v) for n, v, _ in
                   sf.aggregate_participant_net(self.rows, months=["26-09"]))
        # 26-12 しか出ていない参加者は落ちる
        self.assertNotIn("みずほ証券", agg)
        self.assertIn("ＨＳＢＣ証券", agg)

    def test_breakdown_keeps_product_detail(self):
        agg = {name: bd for name, _net, bd in sf.aggregate_participant_net(self.rows)}
        self.assertAlmostEqual(agg["ゴールドマン証券"]["日経225先物"], -12863.0, places=3)
        self.assertAlmostEqual(agg["ゴールドマン証券"]["日経225mini"], 300.0, places=3)

    def test_unknown_product_is_skipped_not_miscounted(self):
        """係数の分からない商品をラージ扱いして誤集計しないこと。"""
        rows = list(self.rows) + [sf.ParticipantRow(product="謎の先物", month="26-09",
                                                    name="架空証券", code="9999", buy=999.0)]
        agg = dict((n, v) for n, v, _ in sf.aggregate_participant_net(rows))
        self.assertNotIn("架空証券", agg)

    def test_header_rows_are_not_parsed_as_data(self):
        names = {r.name for r in self.rows}
        for junk in ("（売超参加者）", "（買超参加者）", "株式会社大阪取引所"):
            self.assertNotIn(junk, names)


class TestParticipantSection(unittest.TestCase):
    """レポート側の参加者別セクション。前週比と注意書きが揃うこと。"""

    def setUp(self):
        import sq_report as sr
        from datetime import date
        self.sr = sr
        self.date = date
        rows = sf.parse_participant_rows(FUT_PARTICIPANT_ROWS)
        self.src = sf.ParticipantSource(as_of=date(2026, 8, 14), rows=rows,
                                        origin="20260814_indexfut_oi_by_tp.xlsx")

    def test_all_contract_months_are_aggregated(self):
        """
        先物の限月は四半期サイクルなので、オプションの当限で絞ると12月限が落ちる。
        引数の months に関わらず、ファイルにある限月をすべて合算すること。
        """
        sec = self.sr.build_participant_section(
            self.src, None, ["26-09"], self.date(2026, 8, 18))
        by = {r["name"]: r for r in sec["rows"]}
        # 12月限にしか出てこない参加者が残っていること
        self.assertIn("みずほ証券", by)
        self.assertIn("26-12", sec["months"])
        self.assertIn("日経225mini", sec["products"])

    def test_section_reports_lag_from_base_date(self):
        sec = self.sr.build_participant_section(
            self.src, None, ["26-09", "26-10", "26-12"], self.date(2026, 8, 18))
        self.assertTrue(sec["available"])
        self.assertEqual(sec["as_of"], "2026-08-14")
        self.assertEqual(sec["lag_days"], 4)
        self.assertTrue(sec["stale"], "4日前のデータが古いものとして扱われていない")

    def test_rows_are_ordered_by_absolute_size(self):
        sec = self.sr.build_participant_section(
            self.src, None, ["26-09", "26-10", "26-12"], self.date(2026, 8, 18))
        names = [r["name"] for r in sec["rows"]]
        # 野村 +35,374 と ＨＳＢＣ -33,442 が上位に来る
        self.assertIn("野村証券", names[:3])
        self.assertIn("ＨＳＢＣ証券", names[:3])

    def test_side_label(self):
        sec = self.sr.build_participant_section(
            self.src, None, ["26-09"], self.date(2026, 8, 18))
        by = {r["name"]: r for r in sec["rows"]}
        self.assertEqual(by["ＨＳＢＣ証券"]["side"], "ショート")
        self.assertEqual(by["野村証券"]["side"], "ロング")

    def test_week_over_week_change(self):
        prev = {"as_of": "2026-08-07", "net": {"ゴールドマン証券": -20000.0}}
        sec = self.sr.build_participant_section(
            self.src, prev, ["26-09", "26-10", "26-12"], self.date(2026, 8, 18))
        gs = next(r for r in sec["rows"] if r["name"] == "ゴールドマン証券")
        # -12,563 − (-20,000) = +7,437 ＝ ショートを縮小した
        self.assertEqual(gs["before"], "-20,000.0")
        self.assertEqual(gs["change"], "+7,437.0")
        self.assertEqual(sec["prev_as_of"], "2026-08-07")

    def test_missing_source_degrades_gracefully(self):
        sec = self.sr.build_participant_section(
            None, None, ["26-09"], self.date(2026, 8, 18))
        self.assertFalse(sec["available"])
        self.assertIn("note", sec)

    def test_snapshot_roundtrip(self):
        snap = self.sr.participant_snapshot(self.src, ["26-09", "26-10", "26-12"])
        self.assertEqual(snap["as_of"], "2026-08-14")
        self.assertIn("ゴールドマン証券", snap["net"])
        self.assertAlmostEqual(snap["net"]["ゴールドマン証券"], -12563.0, places=1)
        self.assertIsNone(self.sr.participant_snapshot(None, ["26-09"]))


class TestHelpers(unittest.TestCase):
    def test_normalise_month(self):
        for text, want in [("2026/09", "26-09"), ("202609", "26-09"),
                           ("2026年9月限", "26-09"), ("26-11", "26-11")]:
            self.assertEqual(sf._normalise_month(text), want, text)

    def test_is_call(self):
        self.assertTrue(sf._is_call("コール"))
        self.assertTrue(sf._is_call("C"))
        self.assertTrue(sf._is_call("CALL"))
        self.assertFalse(sf._is_call("プット"))
        self.assertFalse(sf._is_call("P"))
        self.assertIsNone(sf._is_call(""))

    def test_num_handles_japanese_blanks(self):
        self.assertEqual(sf._num("1,870"), 1870)
        for blank in ("", "-", "―", "N/A", "*"):
            self.assertIsNone(sf._num(blank), blank)

    def test_discover_daily_files_picks_latest(self):
        html = '''
          <a href="/markets/statistics-derivatives/daily/nlsgeu000006g3vt-att/ose20260817.zip">8/17</a>
          <a href="/markets/statistics-derivatives/daily/nlsgeu000006g3vt-att/ose20260818.zip">8/18</a>
          <a href="https://www.jpx.co.jp/x/ose20260814.csv">8/14</a>
          <a href="/other/unrelated.pdf">pdf</a>
        '''
        files = sf.discover_daily_files(html)
        self.assertEqual(files[0][0].isoformat(), "2026-08-18")
        self.assertTrue(files[0][1].startswith("https://www.jpx.co.jp/"))
        self.assertEqual(len(files), 3)

    def test_discover_returns_empty_without_links(self):
        self.assertEqual(sf.discover_daily_files("<html>nothing</html>"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
