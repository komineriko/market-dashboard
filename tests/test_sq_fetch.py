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
