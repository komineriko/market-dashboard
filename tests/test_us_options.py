"""オプションのスクリーニング。

戦略ごとに「出るべき条件で出る」「出てはいけない条件で出ない」の両方を押さえる。
片側だけだと、条件を1つ外しても気づけない。
"""

import json
import os
import tempfile
import unittest
from datetime import date, timedelta

import sq_analytics as sa
import us_demo
import us_fetch as uf
import us_indicators as ui
import us_options as uo
import us_report as ur

ASOF = date(2026, 10, 9)


def q(bid, ask, oi=3000, last=None):
    return uo.Quote(bid=bid, ask=ask, last=last if last is not None else (bid + ask) / 2,
                    volume=100, oi=oi)


def flat_chain(spot, dte, iv, step=5.0, width=8, oi=3000, asof=ASOF):
    """Black-76 で値段をつけた板。スマイル無しの素直な形。"""
    t = max(dte, 0.5) / 365.0
    base = round(spot / step) * step
    rows = []
    for i in range(-width, width + 1):
        k = base + i * step
        if k <= 0:
            continue
        row = uo.StrikeQuote(strike=k)
        for is_call in (True, False):
            p = sa.bs_price(spot, k, t, iv, is_call)
            if p < 0.05:
                continue
            half = max(p * 0.01, 0.01)
            qq = uo.Quote(bid=round(p - half, 2), ask=round(p + half, 2),
                          last=round(p, 2), volume=50, oi=oi)
            if is_call:
                row.call = qq
            else:
                row.put = qq
        rows.append(row)
    return uo.Expiry(expiry=(asof + timedelta(days=dte)).isoformat(), rows=rows)


class TestQuote(unittest.TestCase):

    def test_mid_prefers_the_two_sided_quote(self):
        self.assertAlmostEqual(q(1.00, 1.10).mid, 1.05)

    def test_last_alone_is_not_a_price(self):
        """建玉ゼロの行使価格に残る古い約定値を現在値として読まないこと。

        実データで V の 385P が bid/ask 無しの last=12.7（実勢7程度）、
        T・VZ・TMUS では ATM IV が 170〜230% と出た。
        """
        self.assertIsNone(uo.Quote(bid=None, ask=None, last=2.0).mid)
        self.assertIsNone(uo.Quote(bid=None, ask=None, last=2.0, oi=5000).mid)
        self.assertFalse(uo.Quote(bid=None, ask=None, last=2.0, oi=5000).tradable(True))

    def test_no_price_at_all_is_none(self):
        self.assertIsNone(uo.Quote().mid)

    def test_spread_pct(self):
        self.assertAlmostEqual(q(1.00, 1.10).spread_pct, 9.52, places=1)

    def test_thin_open_interest_is_not_tradable(self):
        self.assertFalse(q(1.00, 1.10, oi=uo.MIN_OI - 1).tradable(True))
        self.assertTrue(q(1.00, 1.10, oi=uo.MIN_OI).tradable(True))

    def test_wide_spread_is_not_tradable(self):
        self.assertFalse(q(1.00, 2.00).tradable(True))

    def test_penny_option_is_not_tradable(self):
        """数セントの玉はMIDで約定しないし、往復の手数料で消える。"""
        self.assertFalse(q(0.01, 0.05).tradable(True))

    def test_selling_needs_a_bid(self):
        self.assertFalse(uo.Quote(bid=0.0, ask=1.0, oi=3000).tradable(False))


class TestForwardAndIv(unittest.TestCase):

    def test_parity_forward_recovers_the_spot(self):
        exp = flat_chain(100.0, 14, 0.30)
        self.assertAlmostEqual(uo.implied_forward(exp, 100.0, ASOF), 100.0, delta=0.5)

    def test_broken_parity_falls_back_to_spot(self):
        """気配が崩れてパリティが現値から5%以上ずれたら採らない。"""
        exp = flat_chain(100.0, 14, 0.30)
        for r in exp.rows:
            if r.put.bid:
                r.put.bid, r.put.ask = 0.01, 0.02      # プットだけ潰す
        self.assertAlmostEqual(uo.implied_forward(exp, 100.0, ASOF), 100.0, places=6)

    def test_atm_iv_recovers_the_input(self):
        exp = flat_chain(100.0, 14, 0.42)
        f = uo.implied_forward(exp, 100.0, ASOF)
        self.assertAlmostEqual(uo.atm_iv(exp, f, exp.t(ASOF)), 42.0, delta=1.0)

    def test_iv_is_computed_not_taken_from_the_feed(self):
        """提供元のIV列は持っていない。気配から逆算している。"""
        self.assertNotIn("iv", uo.Quote().__dict__)


class TestGex(unittest.TestCase):

    def setUp(self):
        self.spot = 100.0
        self.exp = flat_chain(self.spot, 14, 0.30, oi=100)
        # コールは110に、プットは90に厚みを作る
        for r in self.exp.rows:
            if r.strike == 110.0:
                r.call.oi = 50000
            if r.strike == 90.0:
                r.put.oi = 50000

    def test_walls_land_on_the_heavy_strikes(self):
        g = uo.build_gex([self.exp], self.spot, ASOF)
        self.assertEqual(g.call_wall, 110.0)
        self.assertEqual(g.put_wall, 90.0)

    def test_call_wall_up_is_above_spot(self):
        g = uo.build_gex([self.exp], self.spot, ASOF)
        self.assertIsNotNone(g.call_wall_up)
        self.assertGreater(g.call_wall_up, self.spot)

    def test_flip_is_found_below_spot_when_puts_dominate_down_there(self):
        g = uo.build_gex([self.exp], self.spot, ASOF)
        self.assertGreater(g.at_spot, 0)
        self.assertIsNotNone(g.flip)
        self.assertLess(g.flip, self.spot)

    def test_gex_scales_with_open_interest(self):
        """建玉を倍にすればGEXも倍。

        建玉が流動性の足切りを下回っていると、パリティのフォワードが現値に
        落ちてIVの逆算結果まで変わるので、足切りの上で比べる。
        """
        exp = flat_chain(self.spot, 14, 0.30, oi=uo.MIN_OI * 5)
        a = uo.build_gex([exp], self.spot, ASOF).at_spot
        for r in exp.rows:
            r.call.oi *= 2
            r.put.oi *= 2
        b = uo.build_gex([exp], self.spot, ASOF).at_spot
        self.assertAlmostEqual(b, a * 2, delta=abs(a) * 1e-6)


class TestExpirySelection(unittest.TestCase):

    def _u(self, dtes, earnings=None):
        bars = [ui.Bar(f"2026-0{1 + i // 28}-{1 + i % 28:02d}", 101, 99, 100)
                for i in range(80)]
        return uo.Underlying(symbol="X", bars=bars, earnings=earnings,
                             expiries=[flat_chain(100.0, d, 0.3) for d in dtes])

    def test_only_the_short_term_window(self):
        """短期の窓だけを採ること。境界は定数から引いて、値を変えても腐らせない。"""
        dtes = [uo.MIN_DTE - 1, uo.MIN_DTE, uo.MAX_DTE, uo.MAX_DTE + 1,
                uo.MAX_DTE + 20]
        u = self._u(dtes)
        got = [e.dte(ASOF) for e in uo.usable_expiries(u, ASOF)]
        self.assertEqual(got, [uo.MIN_DTE, uo.MAX_DTE])

    def test_expiry_crossing_earnings_is_dropped(self):
        u = self._u([7, 14], earnings=(ASOF + timedelta(days=10)).isoformat())
        got = [e.dte(ASOF) for e in uo.usable_expiries(u, ASOF)]
        self.assertEqual(got, [7], "決算をまたぐ14日限月は残ってはいけない")

    def test_horizon_matches_the_stated_couple_of_weeks(self):
        """「数日〜2週間」と書いている以上、窓もそこに収まっていること。"""
        self.assertGreaterEqual(uo.MIN_DTE, 3)
        self.assertLessEqual(uo.MAX_DTE, 18)

    def test_earnings_after_every_expiry_blocks_nothing(self):
        u = self._u([7, 14], earnings=(ASOF + timedelta(days=60)).isoformat())
        self.assertEqual(len(uo.usable_expiries(u, ASOF)), 2)

    def test_past_earnings_blocks_nothing(self):
        u = self._u([7, 14], earnings=(ASOF - timedelta(days=3)).isoformat())
        self.assertEqual(len(uo.usable_expiries(u, ASOF)), 2)


class TestScreens(unittest.TestCase):
    """合成データの各銘柄が、狙った区分だけに出ること。"""

    @classmethod
    def setUpClass(cls):
        cls.found = {}
        cls.tech = {}
        for u in us_demo.build(ASOF):
            t, f = uo.screen_symbol(u, ASOF)
            cls.found[u.symbol] = f
            cls.tech[u.symbol] = t

    def test_cheap_iv_with_fresh_cross_gives_a_long_call(self):
        self.assertTrue(self.found["ALFA"]["long_call"])

    def test_rich_iv_never_gives_a_long_call(self):
        for sym in ("BETA", "GAMM"):
            self.assertFalse(self.found[sym]["long_call"],
                             f"{sym} はIVが割高なのでコール買いに出てはいけない")

    def test_cheap_iv_does_not_give_premium_selling(self):
        for key in ("bull_put", "csp", "covered_call"):
            self.assertFalse(self.found["ALFA"][key])

    def test_extended_and_rich_gives_a_covered_call(self):
        self.assertTrue(self.found["GAMM"]["covered_call"])

    def test_earnings_blocks_every_strategy(self):
        self.assertEqual(sum(len(v) for v in self.found["EPSI"].values()), 0)

    def test_wide_spreads_block_every_strategy(self):
        self.assertEqual(sum(len(v) for v in self.found["ZETA"].values()), 0)

    def test_thin_open_interest_blocks_every_strategy(self):
        self.assertEqual(sum(len(v) for v in self.found["OMEG"].values()), 0)

    def test_long_call_delta_is_in_band(self):
        for c in self.found["ALFA"]["long_call"]:
            self.assertGreaterEqual(c.legs[0].delta, uo.CALL_DELTA[0])
            self.assertLessEqual(c.legs[0].delta, uo.CALL_DELTA[1])


class TestBullPutArithmetic(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.c = None
        for u in us_demo.build(ASOF):
            _, f = uo.screen_symbol(u, ASOF)
            if f["bull_put"]:
                cls.c = f["bull_put"][0]
                break

    def test_a_candidate_exists(self):
        self.assertIsNotNone(self.c)

    def test_sells_the_higher_strike(self):
        short, long_leg = self.c.legs
        self.assertEqual(short.action, "売")
        self.assertEqual(long_leg.action, "買")
        self.assertGreater(short.strike, long_leg.strike)

    def test_credit_plus_max_loss_equals_the_width(self):
        m = self.c.metrics
        self.assertAlmostEqual(m["credit"] + m["max_loss"],
                               m["width"] * uo.MULTIPLIER, places=4)

    def test_breakeven_sits_below_the_short_strike(self):
        self.assertLess(self.c.metrics["breakeven"], self.c.legs[0].strike)

    def test_breakeven_is_below_spot(self):
        """ブルプットは「下がらなければ勝ち」。損益分岐が現値より上では成立しない。"""
        self.assertLess(self.c.metrics["breakeven"], self.c.spot)
        self.assertGreater(self.c.metrics["cushion_pct"], 0)


class TestPremiumSellingArithmetic(unittest.TestCase):

    def _first(self, key):
        for u in us_demo.build(ASOF):
            _, f = uo.screen_symbol(u, ASOF)
            if f[key]:
                return f[key][0]
        return None

    def test_csp_effective_cost_is_strike_minus_premium(self):
        c = self._first("csp")
        self.assertIsNotNone(c)
        prem = c.metrics["premium"] / uo.MULTIPLIER
        self.assertAlmostEqual(c.metrics["effective_cost"],
                               c.legs[0].strike - prem, places=6)

    def test_csp_effective_cost_is_below_spot(self):
        c = self._first("csp")
        self.assertLess(c.metrics["effective_cost"], c.spot)

    def test_csp_capital_is_the_full_strike(self):
        """キャッシュ確保の売りなので、必要資金は行使価格×100。"""
        c = self._first("csp")
        self.assertAlmostEqual(c.metrics["capital"],
                               c.legs[0].strike * uo.MULTIPLIER, places=6)

    def test_covered_call_strike_is_above_spot(self):
        c = self._first("covered_call")
        self.assertIsNotNone(c)
        self.assertGreater(c.legs[0].strike, c.spot)

    def test_covered_call_called_return_is_positive(self):
        """現値より上で売るので、持っていかれても利益になる。"""
        c = self._first("covered_call")
        self.assertGreater(c.metrics["called_return_pct"], 0)

    def test_covered_call_needs_the_shares(self):
        u = us_demo.build(ASOF)[2]
        u.held = False
        _, f = uo.screen_symbol(u, ASOF)
        self.assertEqual(f["covered_call"], [])


class TestFetchHelpers(unittest.TestCase):

    class Frame:
        def __init__(self, recs):
            self.recs = recs

        def to_dict(self, _how):
            return self.recs

    def test_calls_and_puts_merge_on_the_strike(self):
        calls = self.Frame([{"strike": 100.0, "bid": 1.0, "ask": 1.1,
                             "lastPrice": 1.05, "volume": 5, "openInterest": 7}])
        puts = self.Frame([{"strike": 100.0, "bid": 2.0, "ask": 2.1,
                            "lastPrice": 2.05, "volume": 6, "openInterest": 8}])
        rows = uf.chain_from_frames(calls, puts)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].call.oi, 7)
        self.assertEqual(rows[0].put.oi, 8)

    def test_a_strike_present_on_one_side_only_still_appears(self):
        calls = self.Frame([{"strike": 105.0, "bid": 1.0, "ask": 1.1,
                             "lastPrice": 1.05, "volume": 1, "openInterest": 2}])
        rows = uf.chain_from_frames(calls, self.Frame([]))
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].put.bid)

    def test_nan_becomes_zero_or_none(self):
        nan = float("nan")
        calls = self.Frame([{"strike": 100.0, "bid": nan, "ask": nan,
                             "lastPrice": nan, "volume": nan, "openInterest": nan}])
        rows = uf.chain_from_frames(calls, self.Frame([]))
        self.assertIsNone(rows[0].call.bid)
        self.assertEqual(rows[0].call.oi, 0)

    def test_rows_come_back_sorted_by_strike(self):
        calls = self.Frame([{"strike": s, "bid": 1.0, "ask": 1.1, "lastPrice": 1.0,
                             "volume": 1, "openInterest": 1} for s in (110, 90, 100)])
        rows = uf.chain_from_frames(calls, self.Frame([]))
        self.assertEqual([r.strike for r in rows], [90.0, 100.0, 110.0])

    def test_snapshot_roundtrip(self):
        u = us_demo.build(ASOF)[0]
        back = uf.load_underlying(json.loads(json.dumps(uf.dump_underlying(u))))
        self.assertEqual(back.symbol, u.symbol)
        self.assertEqual(len(back.bars), len(u.bars))
        self.assertEqual([e.expiry for e in back.expiries],
                         [e.expiry for e in u.expiries])
        self.assertAlmostEqual(back.spot, u.spot)

    def test_snapshot_keeps_the_screening_result_identical(self):
        u = us_demo.build(ASOF)[0]
        back = uf.load_underlying(json.loads(json.dumps(uf.dump_underlying(u))))
        a = uo.screen_symbol(u, ASOF)[1]
        b = uo.screen_symbol(back, ASOF)[1]
        self.assertEqual({k: len(v) for k, v in a.items()},
                         {k: len(v) for k, v in b.items()})

    def test_holdings_file_absent_means_unknown(self):
        saved = uf.HOLDINGS_PATH
        try:
            uf.HOLDINGS_PATH = os.path.join(tempfile.gettempdir(), "no_such_holdings.json")
            self.assertIsNone(uf.load_holdings())
        finally:
            uf.HOLDINGS_PATH = saved

    def test_holdings_file_is_read(self):
        saved = uf.HOLDINGS_PATH
        fd = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8")
        json.dump({"symbols": ["aapl", "MSFT"]}, fd)
        fd.close()
        try:
            uf.HOLDINGS_PATH = fd.name
            self.assertEqual(uf.load_holdings(), ["AAPL", "MSFT"])
        finally:
            uf.HOLDINGS_PATH = saved
            os.unlink(fd.name)


class TestReport(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.rep = ur.build_report(us_demo.build(ASOF), ASOF, None, None)

    def test_every_row_matches_its_column_count(self):
        for s in self.rep["sections"]:
            for r in s["rows"]:
                self.assertEqual(len(r["cells"]), len(s["columns"]),
                                 f"{s['key']} の列数と値の数が合っていない")

    def test_tech_rows_match_their_columns(self):
        cols = self.rep["tech"]["columns"]
        for r in self.rep["tech"]["rows"]:
            self.assertEqual(len(r["cells"]), len(cols))

    def test_all_four_sections_are_present_in_order(self):
        self.assertEqual([s["key"] for s in self.rep["sections"]],
                         ["long_call", "bull_put", "csp", "covered_call"])

    def test_empty_section_explains_itself(self):
        rep = ur.build_report([], ASOF, None, None)
        for s in rep["sections"]:
            self.assertEqual(s["rows"], [])
            self.assertTrue(s["empty_reason"], "該当なしの理由が空になっている")

    def test_earnings_blocked_symbol_still_shows_in_the_memo(self):
        syms = [r["symbol"] for r in self.rep["tech"]["rows"]]
        self.assertIn("EPSI", syms)
        row = next(r for r in self.rep["tech"]["rows"] if r["symbol"] == "EPSI")
        self.assertIn("決算", row["cells"][-1])

    def test_memo_is_sorted_by_iv_ratio(self):
        vals = [r["iv_ratio"] for r in self.rep["tech"]["rows"] if r["iv_ratio"]]
        self.assertEqual(vals, sorted(vals))

    def test_holdings_note_changes_with_the_file(self):
        a = ur.build_report([], ASOF, None, None)["holdings_note"]
        b = ur.build_report([], ASOF, None, ["AAPL"])["holdings_note"]
        self.assertNotEqual(a, b)
        self.assertIn("us_holdings.json", a)

    def test_no_nan_or_inf_reaches_the_page(self):
        blob = json.dumps(self.rep, ensure_ascii=False)
        for bad in ("NaN", "Infinity"):
            self.assertNotIn(bad, blob, f"{bad} がページに出ている")

    def test_report_is_json_serialisable(self):
        json.loads(json.dumps(self.rep, ensure_ascii=False))


class TestFreshness(unittest.TestCase):

    def test_friday_points_at_the_next_session_published_the_day_after(self):
        f = ur.freshness(date(2026, 10, 9))          # 金曜
        self.assertEqual(f["next_board_date"], "2026-10-12")   # 次の立会日は月曜
        self.assertIn("10/13", f["next_update_label"])         # 載るのは火曜

    def test_publish_hour_matches_the_workflow_cron(self):
        """ページに出す予定時刻と実際の実行時刻がずれると意味が無い。"""
        import os
        import re
        path = os.path.join(os.path.dirname(HERE_DIR),
                            ".github", "workflows", "update-us-options.yml")
        with open(path, encoding="utf-8") as fh:
            yml = fh.read()
        m = re.search(r'cron:\s*"(\d+)\s+(\d+)\s', yml)
        self.assertIsNotNone(m, "cron が読めない")
        self.assertEqual(int(m.group(2)), ur.PUBLISH_UTC_HOUR)

    def test_us_holiday_is_skipped(self):
        # 2026-11-26 は感謝祭
        self.assertEqual(ur.next_us_business_day(date(2026, 11, 25)),
                         date(2026, 11, 27))

    def test_same_base_date_gives_the_same_value(self):
        """基準日が変わらなければ差分が出ないこと。"""
        self.assertEqual(ur.freshness(date(2026, 10, 9)),
                         ur.freshness(date(2026, 10, 9)))


class TestInjectHtml(unittest.TestCase):

    def setUp(self):
        src = os.path.join(os.path.dirname(HERE_DIR), "us_options.html")
        with open(src, encoding="utf-8") as f:
            self.html = f.read()
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False,
                                               encoding="utf-8")
        self.tmp.write(self.html)
        self.tmp.close()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_writes_then_skips_identical_content(self):
        import update_us_options as up
        rep = ur.build_report(us_demo.build(ASOF), ASOF, None, None)
        self.assertTrue(up.inject_html(rep, self.tmp.name))
        self.assertFalse(up.inject_html(rep, self.tmp.name),
                         "同じ内容で2回目も書くとコミットが毎回増える")

    def test_only_the_timestamp_changing_does_not_rewrite(self):
        import update_us_options as up
        rep = ur.build_report(us_demo.build(ASOF), ASOF, None, None)
        up.inject_html(rep, self.tmp.name)
        rep["meta"]["generated_at"] = "2099-01-01 00:00 JST"
        self.assertFalse(up.inject_html(rep, self.tmp.name))

    def test_real_change_is_written(self):
        import update_us_options as up
        rep = ur.build_report(us_demo.build(ASOF), ASOF, None, None)
        up.inject_html(rep, self.tmp.name)
        rep["meta"]["base_date"] = "2026-10-12"
        self.assertTrue(up.inject_html(rep, self.tmp.name))

    def test_written_payload_parses_back(self):
        import update_us_options as up
        rep = ur.build_report(us_demo.build(ASOF), ASOF, None, None)
        up.inject_html(rep, self.tmp.name)
        with open(self.tmp.name, encoding="utf-8") as f:
            out = f.read()
        seg = out[out.index(up.START) + len(up.START):out.index(up.END)]
        back = json.loads(seg[seg.index("{"):seg.rindex("}") + 1])
        self.assertEqual(back["meta"]["base_date"], rep["meta"]["base_date"])


HERE_DIR = os.path.dirname(os.path.abspath(__file__))

if __name__ == "__main__":
    unittest.main()


class TestBoardDate(unittest.TestCase):
    """基準日は時計から決める。

    日足の最終日をそのまま使うと、配信が遅れた日に残存日数が1日ずれる。
    """

    def test_lands_on_a_weekday(self):
        d = uf.board_date_from_clock()
        self.assertLess(d.weekday(), 5)

    def test_is_not_in_the_future(self):
        from datetime import datetime as dt
        self.assertLessEqual(uf.board_date_from_clock(), dt.utcnow().date())

    def test_reference_price_prefers_the_chain(self):
        """日足が1営業日古いとき、現値は板から取り直すこと。"""
        bars = [ui.Bar(f"2026-08-{(i % 28) + 1:02d}", 101, 99, 100.0)
                for i in range(80)]
        # 板は 95 を織り込んでいる（日足の 100 より新しい）
        u = uo.Underlying(symbol="X", bars=bars,
                          expiries=[flat_chain(95.0, 10, 0.30, oi=3000)])
        self.assertAlmostEqual(u.spot, 100.0)              # 設定前は日足の終値
        uo.screen_symbol(u, ASOF)
        self.assertAlmostEqual(u.spot, 95.0, delta=0.5)    # 設定後は板の値
        self.assertAlmostEqual(u.last_bar_close, 100.0)

    def test_reference_price_falls_back_to_the_last_close(self):
        bars = [ui.Bar(f"2026-08-{(i % 28) + 1:02d}", 101, 99, 100.0)
                for i in range(80)]
        u = uo.Underlying(symbol="X", bars=bars, expiries=[])
        uo.screen_symbol(u, ASOF)
        self.assertAlmostEqual(u.spot, 100.0)


class TestDateCoercion(unittest.TestCase):
    """決算日の型をそろえる。

    pandas の Timestamp は datetime の、datetime は date のサブクラス。
    isinstance(x, date) だけで通すと Timestamp がそのまま残り、
    date と比較した時点で pandas が例外を投げる（実データで踏んだ）。
    """

    def test_plain_types(self):
        from datetime import datetime as dt
        self.assertEqual(uf._as_date(date(2026, 11, 18)), date(2026, 11, 18))
        self.assertEqual(uf._as_date(dt(2026, 11, 18, 21, 0)), date(2026, 11, 18))
        self.assertEqual(uf._as_date("2026-11-18"), date(2026, 11, 18))
        self.assertEqual(uf._as_date("2026-11-18 21:00:00"), date(2026, 11, 18))
        self.assertIsNone(uf._as_date(None))
        self.assertIsNone(uf._as_date("まだ未定"))

    def test_result_can_be_compared_with_a_date(self):
        for raw in (date(2026, 11, 18), "2026-11-18"):
            self.assertGreater(uf._as_date(raw), date(2026, 1, 1))

    def test_pandas_timestamp(self):
        try:
            import pandas as pd
        except ImportError:
            self.skipTest("pandas が無い")
        ts = pd.Timestamp("2026-11-18 21:00:00")
        got = uf._as_date(ts)
        self.assertEqual(got, date(2026, 11, 18))
        self.assertNotIsInstance(got, pd.Timestamp)
        self.assertGreater(got, date(2026, 1, 1))     # ここで例外が出ないこと
        self.assertIsNone(uf._as_date(pd.NaT))


class TestAbsurdIvGuard(unittest.TestCase):
    """実現ボラに対してIVが極端な銘柄を候補から外す。

    年率利回りで並べている以上、気配が異常な銘柄ほど上位に押し上げられる。
    実データで、実現ボラの8.5倍のIVを持つ銘柄がP売りの1位に出た。
    """

    def _underlying(self, iv: float) -> uo.Underlying:
        import random
        rng = random.Random(3)
        closes = [50.0]
        for _ in range(140):
            closes.append(closes[-1] * (1 + rng.gauss(0.0015, 0.012)))
        bars = [ui.Bar(f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", c * 1.01,
                       c * 0.99, c) for i, c in enumerate(closes)]
        spot = bars[-1].close
        return uo.Underlying(symbol="X", bars=bars,
                             expiries=[flat_chain(spot, 7, iv, step=2.5, oi=5000),
                                       flat_chain(spot, 14, iv, step=2.5, oi=5000)])

    def test_sane_threshold(self):
        self.assertTrue(uo.iv_is_sane(1.0))
        self.assertTrue(uo.iv_is_sane(uo.TH_IV_ABSURD))
        self.assertFalse(uo.iv_is_sane(uo.TH_IV_ABSURD + 0.01))
        self.assertFalse(uo.iv_is_sane(None))

    def test_moderately_rich_iv_still_produces_candidates(self):
        u = self._underlying(0.42)
        tech = uo.technicals(u)
        ratio = uo.iv_ratio(42.0, tech)
        self.assertTrue(uo.iv_is_sane(ratio), f"IV/MAD {ratio} は正常な範囲のはず")
        _, found = uo.screen_symbol(u, ASOF)
        self.assertTrue(sum(len(v) for v in found.values()) > 0)

    def test_absurd_iv_produces_nothing(self):
        u = self._underlying(2.5)          # 実現ボラの10倍以上
        tech = uo.technicals(u)
        ratio = uo.iv_ratio(250.0, tech)
        self.assertFalse(uo.iv_is_sane(ratio))
        _, found = uo.screen_symbol(u, ASOF)
        self.assertEqual(sum(len(v) for v in found.values()), 0,
                         "IVが異常な銘柄が候補に残っている")

    def test_excluded_symbol_is_disclosed(self):
        u = self._underlying(2.5)
        u.symbol = "WEIRD"
        rep = ur.build_report([u], ASOF, uf.FetchReport(asof=ASOF), None)
        blob = " ".join(rep["meta"]["sources"])
        self.assertIn("WEIRD", blob, "外した銘柄が開示されていない")
        self.assertIn("IV", blob)
        self.assertIn("MAD", blob)
        row = next(r for r in rep["tech"]["rows"] if r["symbol"] == "WEIRD")
        self.assertIn("IV", row["cells"][-1])


class TestSectionTrim(unittest.TestCase):
    """1区分が同じ銘柄だけで埋まらないこと。"""

    def _c(self, sym, rank):
        return uo.Candidate(strategy="long_call", symbol=sym, expiry="2026-10-16",
                            dte=7, spot=100.0, legs=[], metrics={}, rank=rank)

    def test_best_of_each_symbol_comes_first(self):
        cands = [self._c("AAA", 9), self._c("AAA", 8), self._c("BBB", 7),
                 self._c("BBB", 6), self._c("CCC", 5)]
        got = [c.symbol for c in ur._trim(cands, 3)]
        self.assertEqual(got, ["AAA", "BBB", "CCC"])

    def test_spare_slots_are_filled_with_seconds(self):
        cands = [self._c("AAA", 9), self._c("AAA", 8), self._c("BBB", 7)]
        got = [c.symbol for c in ur._trim(cands, 3)]
        self.assertEqual(got, ["AAA", "BBB", "AAA"])

    def test_still_sorted_by_rank_within_the_first_pass(self):
        cands = [self._c("AAA", 1), self._c("BBB", 9)]
        self.assertEqual([c.symbol for c in ur._trim(cands, 2)], ["BBB", "AAA"])

    def test_limit_is_respected(self):
        cands = [self._c(f"S{i}", i) for i in range(20)]
        self.assertEqual(len(ur._trim(cands, 8)), 8)


class TestUniverseParsing(unittest.TestCase):
    """銘柄リストの読み込み。

    手元のリストは TradingView の書き出し形式（カンマ区切り＋###見出し）
    だったり1行1銘柄だったりするので、両方を受ける。
    """

    def test_tradingview_export_with_section_headers(self):
        raw = (r"\#\#\#マグニフィセント7,NVDA,AAPL,GOOGL,"
               r"\#\#\#40. 金融,BRK.B,JPM,"
               r"\#\#\#45. 情報技術,AVGO,MU")
        self.assertEqual(uf.parse_universe_text(raw),
                         ["NVDA", "AAPL", "GOOGL", "BRK-B", "JPM", "AVGO", "MU"])

    def test_class_shares_are_normalised_for_the_data_source(self):
        self.assertEqual(uf.normalize_ticker("BRK.B"), "BRK-B")
        self.assertEqual(uf.normalize_ticker(" brk.b "), "BRK-B")

    def test_exchange_prefixes_are_stripped_and_non_us_dropped(self):
        raw = "NASDAQ:AAPL,NYSE:BRK.B,TSE:1678,IDX:COMPOSITE,AMEX:ARKK,MIL:MONC"
        self.assertEqual(uf.parse_universe_text(raw), ["AAPL", "BRK-B", "ARKK"])

    def test_one_per_line_with_comments(self):
        self.assertEqual(uf.parse_universe_text("AAPL\nMSFT\n\n# メモ\nNVDA\n"),
                         ["AAPL", "MSFT", "NVDA"])

    def test_duplicates_keep_the_first(self):
        self.assertEqual(uf.parse_universe_text("AAPL,MSFT,AAPL"),
                         ["AAPL", "MSFT"])

    def test_junk_is_ignored(self):
        self.assertEqual(uf.parse_universe_text("AAPL,,  ,日本語,123456789012,MSFT"),
                         ["AAPL", "MSFT"])

    def test_file_is_used_when_present(self):
        fd = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                         encoding="utf-8")
        fd.write("AAPL,MSFT,NVDA")
        fd.close()
        try:
            syms, src = uf.load_universe_symbols(fd.name)
            self.assertEqual(syms, ["AAPL", "MSFT", "NVDA"])
            self.assertIn("3銘柄", src)
        finally:
            os.unlink(fd.name)

    def test_falls_back_to_the_builtin_list(self):
        missing = os.path.join(tempfile.gettempdir(), "no_such_universe.txt")
        syms, src = uf.load_universe_symbols(missing)
        self.assertEqual(syms, list(uf.UNIVERSE))
        self.assertIn("組み込み", src)

    def test_empty_file_falls_back(self):
        fd = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                         encoding="utf-8")
        fd.write("\n# 見出しだけ\n")
        fd.close()
        try:
            syms, src = uf.load_universe_symbols(fd.name)
            self.assertEqual(syms, list(uf.UNIVERSE))
            self.assertIn("組み込み", src)
        finally:
            os.unlink(fd.name)


class TestShortlist(unittest.TestCase):
    """板を取りに行く前の絞り込み。

    リストが数百本になると、板と決算の取得が全体の時間をほとんど占める。
    テクニカルで落とせる銘柄は先に落とす。
    """

    def _bars(self, seed, n=160, vol_mult=1.0, trend=0.0015):
        import random
        rng = random.Random(seed)
        closes = [100.0]
        for _ in range(n - 1):
            closes.append(closes[-1] * (1 + rng.gauss(trend, 0.013)))
        return [ui.Bar(f"d{i}", c * 1.012, c * 0.988, c, 1_000_000 * vol_mult)
                for i, c in enumerate(closes)]

    def test_gate_rejection_means_no_candidates(self):
        """ゲートで落とした銘柄が、実は候補になりえた…が起きないこと。"""
        checked = 0
        for seed in range(40):
            bars = self._bars(seed, trend=0.0)
            u = uo.Underlying(symbol=f"S{seed}", bars=bars,
                              expiries=[flat_chain(bars[-1].close, 10, 0.5,
                                                   step=2.5, oi=5000)])
            if uo.technical_gate(uo.technicals(u)):
                continue
            checked += 1
            _, found = uo.screen_symbol(u, ASOF)
            self.assertEqual(sum(len(v) for v in found.values()), 0,
                             f"S{seed} はゲートで落としたのに候補が出た")
        self.assertGreater(checked, 0, "ゲートで落ちる銘柄が1つも無く、検証できていない")

    def test_short_history_is_dropped(self):
        bars_map = {"AAA": self._bars(1)[:30]}
        kept, _ = uf.shortlist(bars_map, ["AAA"])
        self.assertEqual(kept, [])

    def test_penny_stock_is_dropped(self):
        bars = [ui.Bar(f"d{i}", 2.0, 1.8, 1.9, 1e6) for i in range(160)]
        kept, _ = uf.shortlist({"AAA": bars}, ["AAA"])
        self.assertEqual(kept, [])

    def test_cap_keeps_the_most_traded(self):
        bars_map = {
            "BIG": self._bars(2, vol_mult=100.0),
            "MID": self._bars(2, vol_mult=10.0),
            "SMALL": self._bars(2, vol_mult=1.0),
        }
        syms = ["SMALL", "MID", "BIG"]
        # まず3本とも条件を通ることを確かめる（通らないとこのテストが無意味）
        all_kept, _ = uf.shortlist(bars_map, syms, cap=10)
        self.assertEqual(sorted(all_kept), ["BIG", "MID", "SMALL"])
        kept, capped = uf.shortlist(bars_map, syms, cap=2)
        self.assertEqual(kept, ["BIG", "MID"])
        self.assertEqual(capped, 1)

    def test_dollar_volume_uses_the_median(self):
        bars = [ui.Bar(f"d{i}", 10, 10, 10.0, 100.0) for i in range(20)]
        bars[-1] = ui.Bar("spike", 10, 10, 10.0, 10_000_000.0)
        self.assertAlmostEqual(uf.dollar_volume(bars), 1000.0)


class TestStaleLastPrices(unittest.TestCase):
    """建玉ゼロの行使価格に残る古い約定値でIVが壊れないこと。

    数字は 2026-10-09 の実データから取っている。V（現値385前後）の
    権利行使385のプットは bid/ask が無く last=12.70 だけが残っていた。
    実勢は7程度なので、これを現在値として読むとIVが倍近くに出る。
    """

    def _chain_with_one_stale_strike(self):
        exp = flat_chain(385.0, 14, 0.21, step=2.5, oi=3000)
        row = exp.row(385.0)
        self.assertIsNotNone(row)
        row.put = uo.Quote(bid=None, ask=None, last=12.70, volume=0, oi=0)
        return exp

    def test_atm_iv_ignores_the_stale_strike(self):
        clean = flat_chain(385.0, 14, 0.21, step=2.5, oi=3000)
        stale = self._chain_with_one_stale_strike()
        f = uo.implied_forward(clean, 385.0, ASOF)
        a = uo.atm_iv(clean, f, clean.t(ASOF))
        b = uo.atm_iv(stale, uo.implied_forward(stale, 385.0, ASOF),
                      stale.t(ASOF))
        self.assertAlmostEqual(a, 21.0, delta=1.5)
        self.assertAlmostEqual(b, a, delta=1.0,
                               msg="古い約定値がATM IVを動かしている")

    def test_stale_strike_does_not_reach_the_forward(self):
        stale = self._chain_with_one_stale_strike()
        self.assertAlmostEqual(uo.implied_forward(stale, 385.0, ASOF), 385.0,
                               delta=1.0)

    def test_stale_strike_contributes_no_gamma(self):
        """GEXの材料にも入らないこと。古い値段からのガンマは実勢ではない。"""
        stale = self._chain_with_one_stale_strike()
        material = uo._strike_ivs([stale], 385.0, ASOF)
        self.assertFalse([m for m in material
                          if abs(m[0] - 385.0) < 1e-6 and m[3] is False],
                         "気配の無い行使価格がGEXに入っている")

    def test_very_wide_quotes_are_left_out_of_the_iv_sample(self):
        """仲値が当てにならないほど広い気配は基準値に使わない。"""
        exp = flat_chain(100.0, 10, 0.30, step=2.5, oi=3000)
        row = exp.row(100.0)
        row.call = uo.Quote(bid=0.10, ask=9.00, last=1.0, volume=1, oi=3000)
        f = uo.implied_forward(exp, 100.0, ASOF)
        iv = uo.atm_iv(exp, f, exp.t(ASOF))
        self.assertAlmostEqual(iv, 30.0, delta=2.0)


class TestUniverseProvenance(unittest.TestCase):
    """どの銘柄リストを使ったかがページに出ること。"""

    def test_source_label_is_passed_through(self):
        rep = uf.FetchReport()
        rep.universe_source = "us_universe.txt（430銘柄）"
        rep.universe_size = 430
        rep.shortlisted = 395
        out = ur.build_report([], ASOF, rep, None)
        blob = " ".join(out["meta"]["sources"])
        self.assertIn("us_universe.txt", blob)
        self.assertIn("395", blob)


class TestPageTemplate(unittest.TestCase):
    """ページの指定。見た目が壊れる書き方を入れないための歯止め。"""

    def setUp(self):
        with open(os.path.join(os.path.dirname(HERE_DIR), "us_options.html"),
                  encoding="utf-8") as f:
            self.html = f.read()

    def test_tally_styles_only_its_direct_children(self):
        """`.tally div` だと中の数字と見出しにも枠がつき、二重枠になる。"""
        self.assertIn(".tally > div{", self.html)
        self.assertNotIn(".tally div{", self.html)

    def test_freshness_banner_is_rendered(self):
        self.assertIn("function freshness(", self.html)
        self.assertIn("freshness(m)", self.html)
        self.assertIn("next_update_at", self.html)

    def test_definitions_survive_printing(self):
        """印刷時に details を display:none にすると定義がPDFから丸ごと落ちる。"""
        self.assertIn("beforeprint", self.html)
        self.assertNotIn("details{display:none;}", self.html.replace(" ", ""))
