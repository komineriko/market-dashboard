"""テクニカル指標。

中心になるのは TestAgainstReferenceReport。参考にした既存レポートが
NVDA 2026-10-08 で出している値を、同じ日の実データで再現できることを固定する。
指標の定義（ATRを単純平均にする、MADボラは60日、HV20はギャップ1本を落とす）は
どれも他の選び方がありえるので、ここが崩れたら定義を変えてしまったということ。
"""

import csv
import os
import unittest

import us_indicators as ui

HERE = os.path.dirname(os.path.abspath(__file__))


def load_nvda():
    path = os.path.join(HERE, "fixtures", "nvda_eod_20261009.csv")
    with open(path, encoding="utf-8") as f:
        return [ui.Bar(r["date"], float(r["high"]), float(r["low"]), float(r["close"]))
                for r in csv.DictReader(f)]


class TestAgainstReferenceReport(unittest.TestCase):
    """NVDA 2026-10-08。期待値は参考レポートの誌面から取っている。"""

    @classmethod
    def setUpClass(cls):
        cls.bars = [b for b in load_nvda() if b.date <= "2026-10-08"]
        cls.closes = [b.close for b in cls.bars]
        cls.atr = ui.atr(cls.bars)
        cls.macd = ui.macd(cls.closes, cls.atr)

    def test_close(self):
        self.assertAlmostEqual(self.closes[-1], 230.48, places=2)

    def test_deviation_in_atr(self):
        dev = (self.closes[-1] - ui.sma(self.closes, 50)) / self.atr
        self.assertAlmostEqual(dev, 1.7, places=1)

    def test_rsi(self):
        self.assertAlmostEqual(ui.rsi(self.closes), 54.5, places=1)

    def test_macd_values(self):
        # 誌面は 4.49 / 3.84。配当調整の有無で 0.04 ほどずれるので小数1桁で見る。
        self.assertAlmostEqual(self.macd.dif, 4.49, delta=0.06)
        self.assertAlmostEqual(self.macd.dea, 3.84, delta=0.06)

    def test_macd_histogram_is_shrinking(self):
        h = [self.macd.hist_prev2, self.macd.hist_prev, self.macd.hist]
        self.assertAlmostEqual(h[0], 1.37, delta=0.05)
        self.assertAlmostEqual(h[2], 0.65, delta=0.05)
        self.assertTrue(h[0] > h[1] > h[2], "ヒストは縮んでいるはず")

    def test_golden_cross_12_bars_ago_above_zero(self):
        self.assertEqual(self.macd.gc_bars_ago, 12)
        self.assertTrue(self.macd.gc_above_zero)

    def test_mad_vol(self):
        self.assertAlmostEqual(ui.mad_vol(self.closes), 39.6, places=1)

    def test_hv20_excluding_gap(self):
        self.assertAlmostEqual(ui.hv_excl_gap(self.closes), 21.6, places=1)

    def test_momentum_score(self):
        self.assertEqual(ui.momentum_score(self.bars, self.macd).score, 1)


class TestDefinitions(unittest.TestCase):

    def test_atr_default_is_simple_mean_not_wilder(self):
        """既定をWilderに変えると乖離の分母が変わり、全銘柄の順位が動く。"""
        bars = [b for b in load_nvda() if b.date <= "2026-10-08"]
        self.assertAlmostEqual(ui.atr(bars), 5.351, places=2)
        self.assertNotAlmostEqual(ui.atr(bars), ui.atr(bars, wilder=True), places=2)

    def test_ema_seeds_with_sma(self):
        """EMAの初期値を values[0] にすると序盤が歪み、MACDに何十本も残る。"""
        v = [10.0] * 5 + [20.0] * 5
        s = ui.ema_series(v, 5)
        self.assertIsNone(s[3])
        self.assertAlmostEqual(s[4], 10.0)     # 最初の5本の単純平均

    def test_hv_excl_gap_drops_the_outlier(self):
        """決算ギャップ1本で20日ボラが膨らまないこと。"""
        calm = [100.0]
        for _ in range(25):
            calm.append(calm[-1] * 1.002)
        with_gap = list(calm)
        with_gap[-1] = with_gap[-2] * 1.25     # 窓開け1本
        self.assertAlmostEqual(ui.hv_excl_gap(calm), ui.hv_excl_gap(with_gap), delta=0.5)

    def test_mad_vol_is_robust_to_one_spike(self):
        import random
        random.seed(7)
        closes = [100.0]
        for _ in range(80):
            closes.append(closes[-1] * (1 + random.gauss(0, 0.01)))
        base = ui.mad_vol(closes)
        spiked = list(closes)
        spiked[-1] = spiked[-2] * 1.3
        self.assertAlmostEqual(base, ui.mad_vol(spiked), delta=1.0)

    def test_golden_cross_is_dropped_once_price_crosses_back_down(self):
        """上抜けたあとに下抜けたら、その上抜けはもう効いていない。

        定義は「直近20本以内に上抜け、かつ現在も DIF > DEA」。後半を
        見落とすと、すでに崩れた銘柄がコール買いとブルプットに出てしまう。
        実データ（NVDA 2026-10-08 は GC 12本前）の後ろに急落を継ぎ足して確かめる。
        """
        closes = [b.close for b in load_nvda() if b.date <= "2026-10-08"]
        self.assertEqual(ui.macd(closes).gc_bars_ago, 12)

        broken = list(closes)
        for _ in range(10):
            broken.append(broken[-1] * 0.97)
        m = ui.macd(broken)
        self.assertLess(m.hist, 0, "急落後は DIF が DEA の下にいるはず")
        self.assertIsNone(m.gc_bars_ago, "下抜けた後にGC扱いしてはいけない")
        self.assertTrue(m.dead_cross_recent)

    def test_rsi_all_up_is_100(self):
        self.assertEqual(ui.rsi([100 + i for i in range(30)]), 100.0)

    def test_short_history_returns_none(self):
        bars = load_nvda()[:5]
        self.assertIsNone(ui.atr(bars))
        self.assertIsNone(ui.sma([b.close for b in bars], 50))
        self.assertIsNone(ui.macd([b.close for b in bars]))


if __name__ == "__main__":
    unittest.main()
