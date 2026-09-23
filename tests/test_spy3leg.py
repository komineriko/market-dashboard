#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
spy3leg の検証。

実チェーンがこの環境から取得できないため、既知のスキューから合成したチェーンで
①②③の選定・満期損益・Greeks・再評価の数学的な正しさだけを確かめる。
ここで出る行使価格は合成値であり、戦略判断には使わない。
"""

import math
import os
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import spy3leg as sl

SPOT, R, Q, DTE = 600.0, 0.04, 0.012, 14


def skew_iv(k: float) -> float:
    """下方ほど高いスキュー（合成用）。"""
    return 0.16 + 0.9 * max(0.0, 1 - k / SPOT) - 0.3 * max(0.0, k / SPOT - 1)


def synth_chain(spread: float = 0.04) -> sl.Chain:
    t = DTE / 365.0
    rows = {}
    for k in range(540, 641):
        k = float(k)
        s = skew_iv(k)
        row = sl.Row(k)
        for is_call, qt in ((True, row.call), (False, row.put)):
            px = sl.bs_price(SPOT, k, t, R, Q, s, is_call)
            g = sl.bs_greeks(SPOT, k, t, R, Q, s, is_call)
            qt.bid, qt.ask = max(px - spread / 2, 0.01), px + spread / 2
            qt.delta, qt.gamma, qt.vega, qt.theta, qt.iv = g.delta, g.gamma, g.vega, g.theta, s
        rows[k] = row
    return sl.Chain("2026-10-07", DTE, rows)


class BlackScholes(unittest.TestCase):
    def test_parity(self):
        t = 0.1
        c = sl.bs_price(SPOT, 590, t, R, Q, 0.2, True)
        p = sl.bs_price(SPOT, 590, t, R, Q, 0.2, False)
        self.assertAlmostEqual(c - p, SPOT * math.exp(-Q * t) - 590 * math.exp(-R * t), places=9)

    def test_iv_roundtrip(self):
        px = sl.bs_price(SPOT, 610, 0.05, R, Q, 0.23, False)
        self.assertAlmostEqual(sl.implied_vol(px, SPOT, 610, 0.05, R, Q, False), 0.23, places=5)

    def test_vega_matches_finite_difference(self):
        g = sl.bs_greeks(SPOT, 600, 0.04, R, Q, 0.2, False)
        fd = sl.bs_price(SPOT, 600, 0.04, R, Q, 0.21, False) - sl.bs_price(SPOT, 600, 0.04, R, Q, 0.20, False)
        self.assertAlmostEqual(g.vega, fd, delta=0.01)


class LegSelection(unittest.TestCase):
    def setUp(self):
        self.chain = synth_chain()

    def test_k1_is_otm_ten_delta_put(self):
        k1 = sl.strike_for_call_delta(self.chain, 0.90)
        self.assertLess(k1, SPOT)
        self.assertAlmostEqual(self.chain.rows[k1].put.delta, -0.10, delta=0.03)

    def test_k2_is_itm_eightyfive_delta_put(self):
        k2 = sl.strike_for_call_delta(self.chain, 0.15)
        self.assertGreater(k2, SPOT)
        self.assertAlmostEqual(self.chain.rows[k2].put.delta, -0.85, delta=0.03)

    def test_scan_and_choice(self):
        k1 = sl.strike_for_call_delta(self.chain, 0.90)
        k2 = sl.strike_for_call_delta(self.chain, 0.15)
        cands = sl.scan_third_leg(self.chain, k1, k2, SPOT)
        self.assertEqual([c.k3 for c in cands], [k for k in self.chain.strikes() if k1 < k < k2])
        for c in cands:
            p = {lg.strike: lg.quote.price for lg in c.st.legs}
            # 満期最大損失 = (K2 − K3 + Net Debit) × 100（K1 < K3 < K2 の平坦区間）
            want = (k2 - c.k3 + p[k1] - p[k2] + p[c.k3]) * 100
            self.assertAlmostEqual(c.max_loss, want, places=6)
            self.assertEqual(c.feasible, c.max_loss < 2500)
        # K3 が上がるほど（ブル・プット幅が狭まるほど）最大損失は減る
        mls = [c.max_loss for c in cands]
        self.assertEqual(mls, sorted(mls, reverse=True))
        best = sl.choose_third_leg(cands)
        self.assertIsNotNone(best)
        self.assertTrue(best.feasible and best.below_atm)
        self.assertGreater(best.greeks.vega, 0)   # この合成チェーンでは Net Vega Long

    def test_natural_fill_is_worse(self):
        st = sl.build(self.chain, 580, 610, 598, SPOT, R, Q)
        self.assertGreater(st.debit("natural"), st.debit("mid"))
        self.assertGreater(st.max_loss("natural"), st.max_loss("mid"))


class Structure(unittest.TestCase):
    def setUp(self):
        self.st = sl.build(synth_chain(spread=0.0), 580, 610, 598, SPOT, R, Q)

    def test_entry_value_equals_mid(self):
        self.assertAlmostEqual(self.st.pnl(0, 0, DTE), 0.0, places=4)

    def test_expiry_revaluation_matches_payoff(self):
        for m in sl.SPOT_MOVES:
            s = SPOT * (1 + m)
            self.assertAlmostEqual(self.st.pnl(m, 0, 0), self.st.payoff(s), places=2)

    def test_breakevens_are_zero(self):
        for b in self.st.breakevens():
            self.assertAlmostEqual(self.st.payoff(b), 0.0, places=6)

    def test_attribution_sums(self):
        a = self.st.attribution(-0.05, 10, 7)
        self.assertAlmostEqual(a["theta"] + a["delta_gamma"] + a["vega"], a["total"], places=6)
        self.assertAlmostEqual(a["total"], self.st.pnl(-0.05, 10, 7), places=6)

    def test_chain_greeks_match_model(self):
        cg, mg = self.st.chain_greeks(), self.st.model_greeks()
        self.assertAlmostEqual(cg.vega, mg.vega, delta=0.05)
        self.assertAlmostEqual(cg.delta, mg.delta, delta=0.5)


class CsvLoading(unittest.TestCase):
    def test_wide_format_with_percent_iv(self):
        text = (
            "Expiration,DTE,Strike,Call Bid,Call Ask,Call Delta,Call IV,Put Bid,Put Ask,Put Delta,Put IV,Put Vega\n"
            "2026-10-07,14,600,8.10,8.20,0.52,15.9%,6.90,7.00,-0.48,16.1%,0.45\n"
            "2026-10-09,16,600,8.60,8.70,0.52,16.0,7.40,7.50,-0.48,16.2,0.48\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            f.write(text)
        try:
            chains, _ = sl.load_chains(f.name)
        finally:
            os.unlink(f.name)
        ch = sl.select_expiry(chains, 14)
        self.assertEqual(ch.dte, 14)
        row = ch.rows[600.0]
        self.assertAlmostEqual(row.put.price, 6.95)
        self.assertAlmostEqual(row.put.iv, 0.161)
        self.assertAlmostEqual(row.call.delta, 0.52)
        self.assertAlmostEqual(row.put.vega, 0.45)
        self.assertAlmostEqual(chains[1].rows[600.0].put.iv, 0.162)

    def test_long_format(self):
        text = ("expiration,dte,strike,type,bid,ask,delta\n"
                "2026-10-07,14,600,C,8.1,8.2,0.52\n"
                "2026-10-07,14,600,P,6.9,7.0,-0.48\n")
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            f.write(text)
        try:
            chains, _ = sl.load_chains(f.name)
        finally:
            os.unlink(f.name)
        row = chains[0].rows[600.0]
        self.assertAlmostEqual(row.call.delta, 0.52)
        self.assertAlmostEqual(row.put.delta, -0.48)


class Opex(unittest.TestCase):
    def test_third_friday(self):
        self.assertEqual(sl.third_friday(2026, 9), date(2026, 9, 18))
        self.assertEqual(sl.third_friday(2026, 10), date(2026, 10, 16))

    def test_regimes(self):
        cases = [
            (date(2026, 9, 21), date(2026, 10, 5), "Normal"),
            (date(2026, 10, 5), date(2026, 10, 19), "Monthly OPEX Cross"),
            (date(2026, 10, 16), date(2026, 10, 30), "Monthly OPEX Entry"),
            (date(2026, 9, 7), date(2026, 9, 21), "Quarterly OPEX Cross"),
            (date(2026, 9, 18), date(2026, 10, 2), "Quarterly OPEX Entry"),
            (date(2026, 9, 4), date(2026, 9, 18), "Quarterly OPEX Cross"),  # 満期 = 第3金曜
        ]
        for entry, exit_, want in cases:
            self.assertEqual(sl.opex_regime(sl.opex_flags(entry, exit_)), want, (entry, exit_))

    def test_holiday_shift(self):
        # 第3金曜が祝日なら前日の木曜が OPEX
        f = sl.opex_flags(date(2026, 9, 17), date(2026, 9, 30), holidays=[date(2026, 9, 18)])
        self.assertTrue(f["quarterly_entry"])

    def test_macro(self):
        f = sl.macro_flags(date(2026, 9, 1), date(2026, 9, 15),
                           {"FOMC": [date(2026, 9, 16)], "CPI": [date(2026, 9, 10)]})
        self.assertEqual(f, {"FOMC": False, "CPI": True})


class Metrics(unittest.TestCase):
    def test_basic(self):
        pnls = [100, -50, 200, -50, 0] + [-20] * 14 + [5000]
        m = sl.metrics(pnls)
        self.assertEqual(m["n"], 20)
        self.assertAlmostEqual(m["win_rate"], 3 / 20)
        self.assertAlmostEqual(m["profit_factor"], 5300 / 380)
        # 上位5%（1件 = 5000）を除くと期待値はマイナス
        self.assertEqual(m["trimmed_n"], 1)
        self.assertLess(m["ev_ex_top5"], 0)
        self.assertGreater(m["expected_value"], 0)

    def test_drawdown(self):
        self.assertEqual(sl.metrics([100, -30, -40, 50, -100])["max_drawdown"], 120)


if __name__ == "__main__":
    unittest.main()
