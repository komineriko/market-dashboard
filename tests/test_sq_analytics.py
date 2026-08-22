#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sq_analytics の検証。

既知のスマイルから合成オプション板を組み立て、そこから逆算した指標が
元のパラメータに戻ることを確認する。実データが無くても数学の正しさは検証できる。
"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sq_analytics as sa

F_TRUE = 67260.0
T_TRUE = 24 / 365

# 元レポート 8/18 の固定モネネススマイル（%）
SMILE = {
    1.05: 28.24, 1.02: 28.44, 1.00: 29.13, 0.98: 30.21, 0.95: 32.68,
    0.92: 34.71, 0.90: 36.60, 0.85: 42.00, 0.80: 48.68, 0.75: 55.63,
}


def true_sigma(strike: float) -> float:
    """モネネス補間で任意の行使価格のIVを返す（合成板の生成元）。"""
    m = strike / F_TRUE
    ms = sorted(SMILE)
    if m <= ms[0]:
        return SMILE[ms[0]] / 100
    if m >= ms[-1]:
        return SMILE[ms[-1]] / 100
    for a, b in zip(ms, ms[1:]):
        if a <= m <= b:
            w = (m - a) / (b - a)
            return (SMILE[a] * (1 - w) + SMILE[b] * w) / 100
    raise AssertionError


def build_chain(month="26-09", oi_shift=0):
    """行使価格 39,000〜83,500 の板を合成する。建玉はPUT厚め（実際の日経板の形）。"""
    chain = sa.Chain(contract_month=month)
    k = 39000.0
    while k <= 83500.0:
        sigma = true_sigma(k)
        c = sa.bs_price(F_TRUE, k, T_TRUE, sigma, True)
        p = sa.bs_price(F_TRUE, k, T_TRUE, sigma, False)
        # 建玉: 500円刻みに厚みを置き、PUTは下方に、CALLは上方に積む
        dist = abs(k - F_TRUE) / F_TRUE
        call_oi = int(2000 * math.exp(-((dist - 0.04) ** 2) / 0.002)) if k > F_TRUE else 200
        put_oi = int(3000 * math.exp(-((dist - 0.03) ** 2) / 0.004)) if k < F_TRUE else 150
        if k == 60000.0:
            put_oi += 9000 + oi_shift      # 深部の大きな床
        if k == 67000.0:
            put_oi += 2500 + oi_shift * 2  # 現値直下の主戦場
        if k == 70000.0:
            call_oi += 3500                # 上値の蓋
        chain.add(sa.StrikeRow(strike=k,
                               call_price=round(c, 0) if c >= 1 else None,
                               put_price=round(p, 0) if p >= 1 else None,
                               call_oi=call_oi, put_oi=put_oi))
        k += 250.0
    return chain


class TestBlackScholes(unittest.TestCase):
    def test_put_call_parity(self):
        c = sa.bs_price(F_TRUE, 67000, T_TRUE, 0.29, True)
        p = sa.bs_price(F_TRUE, 67000, T_TRUE, 0.29, False)
        self.assertAlmostEqual(c - p, F_TRUE - 67000, places=6)

    def test_implied_vol_roundtrip(self):
        for k in (55000, 63000, 67250, 70000, 78000):
            for is_call in (True, False):
                sigma = true_sigma(k)
                price = sa.bs_price(F_TRUE, k, T_TRUE, sigma, is_call)
                back = sa.implied_vol(price, F_TRUE, k, T_TRUE, is_call)
                self.assertIsNotNone(back, f"K={k} call={is_call} でIVが解けない")
                self.assertAlmostEqual(back, sigma, places=5)

    def test_implied_vol_rejects_arbitrage(self):
        # 本質価値以下の価格には解が無い
        self.assertIsNone(sa.implied_vol(10.0, F_TRUE, 60000, T_TRUE, True))
        self.assertIsNone(sa.implied_vol(0.0, F_TRUE, 67000, T_TRUE, True))

    def test_delta_gamma_signs(self):
        self.assertGreater(sa.bs_delta(F_TRUE, 67000, T_TRUE, 0.29, True), 0)
        self.assertLess(sa.bs_delta(F_TRUE, 67000, T_TRUE, 0.29, False), 0)
        self.assertGreater(sa.bs_gamma(F_TRUE, 67000, T_TRUE, 0.29), 0)

    def test_gamma_matches_numeric_derivative(self):
        k, sigma, h = 67000, 0.29, 1.0
        d_up = sa.bs_delta(F_TRUE + h, k, T_TRUE, sigma, True)
        d_dn = sa.bs_delta(F_TRUE - h, k, T_TRUE, sigma, True)
        self.assertAlmostEqual((d_up - d_dn) / (2 * h),
                              sa.bs_gamma(F_TRUE, k, T_TRUE, sigma), places=9)


class TestCalendar(unittest.TestCase):
    def test_sq_is_second_friday(self):
        from datetime import date
        self.assertEqual(sa.sq_date_for(2026, 9), date(2026, 9, 11))
        self.assertEqual(sa.sq_date_for(2026, 10), date(2026, 10, 9))

    def test_days_match_report(self):
        from datetime import date
        base, sq = date(2026, 8, 18), date(2026, 9, 11)
        self.assertEqual((sq - base).days, 24)              # 残24暦日
        self.assertEqual(sa.business_days_between(base, sq), 18)  # 残18営業日

    def test_parse_contract_month(self):
        self.assertEqual(sa.parse_contract_month("26-09"), (2026, 9))
        self.assertEqual(sa.parse_contract_month("202610"), (2026, 10))


class TestChainAnalytics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chain = build_chain()
        cls.fwd = sa.implied_forward(cls.chain, 67500)
        cls.curve = sa.build_iv_curve(cls.chain, cls.fwd.forward, T_TRUE)

    def test_forward_recovered_from_parity(self):
        self.assertAlmostEqual(self.fwd.forward, F_TRUE, delta=1.0)
        self.assertLess(self.fwd.spread, 5.0, "パリティのばらつきが大きすぎる")

    def test_iv_curve_recovers_smile(self):
        for m, expected in SMILE.items():
            got = self.curve.at(F_TRUE * m)
            self.assertIsNotNone(got, f"m={m} でIVが取れない")
            # 清算値段を円単位に丸めているぶんの誤差だけ許容する
            self.assertAlmostEqual(got * 100, expected, delta=0.35, msg=f"m={m}")

    def test_atm_iv(self):
        self.assertAlmostEqual(self.curve.atm_iv() * 100, 29.13, delta=0.35)

    def test_iv_columns_are_healthy(self):
        # 合成板は無矛盾なので CALL/PUT のIV乖離はほぼ無いはず
        self.assertLess(self.curve.broken, self.curve.evaluated * 0.05)

    def test_skew_metrics(self):
        sk = sa.skew_metrics(self.curve)
        self.assertIsNotNone(sk.rr25)
        # 下方に厚いスマイルなので 25dPut IV > 25dCall IV
        self.assertGreater(sk.rr25, 0)
        self.assertLess(sk.put25_strike, F_TRUE)
        self.assertGreater(sk.call25_strike, F_TRUE)

    def test_delta_targets_are_hit(self):
        for target, is_call in ((-0.25, False), (-0.10, False), (0.25, True)):
            res = sa.strike_for_delta(self.curve, target, is_call)
            self.assertIsNotNone(res)
            k, sigma = res
            got = sa.bs_delta(self.curve.forward, k, T_TRUE, sigma, is_call)
            self.assertAlmostEqual(got, target, places=3)

    def test_moneyness_smile_is_monotone_downward(self):
        sm = sa.moneyness_smile(self.curve)
        vals = [sm[m] for m in (1.00, 0.95, 0.90, 0.85, 0.80)]
        self.assertTrue(all(a < b for a, b in zip(vals, vals[1:])),
                        "下に行くほどIVが高いスマイルになっていない")

    def test_gex_flip_and_regime(self):
        prof = sa.gex_profile(self.chain, self.curve, self.fwd.forward)
        self.assertIsNotNone(prof.flip, "GEXフリップが見つからない")
        # PUTが厚い板なので現値より上にフリップがあり、下方はショートガンマ
        self.assertLess(sa.net_gex_at(self.chain, self.curve, prof.flip - 2000), 0)
        self.assertGreater(sa.net_gex_at(self.chain, self.curve, prof.flip + 2000), 0)
        self.assertIn(prof.regime, ("ロングガンマ", "ショートガンマ"))

    def test_max_pain(self):
        mp = sa.max_pain(self.chain)
        self.assertIsNotNone(mp)
        self.assertGreater(mp.payout_oku, 0)
        self.assertGreaterEqual(mp.slope_pct, 0)

    def test_walls_and_condor(self):
        walls = sa.wall_map(self.chain, self.curve, self.fwd.forward)
        self.assertTrue(walls["upper"] and walls["lower"])
        cond = sa.iron_condor(walls)
        self.assertGreater(cond.upper, self.fwd.forward)
        self.assertLess(cond.lower, self.fwd.forward)
        self.assertGreater(cond.width, 0)
        # 仕込んだ 70,000 CALL の蓋が最上位に出るはず
        self.assertEqual(walls["upper"][0].strike, 70000.0)
        # 隣接ピークが重複して並んでいないこと
        ups = [w.strike for w in walls["upper"]]
        self.assertTrue(all(abs(a - b) > 375 for a in ups for b in ups if a != b))

    def test_pin_candidates_stay_near_the_money(self):
        pins = sa.pin_candidates(self.chain, self.fwd.forward)
        self.assertTrue(pins)
        for k, _nominal, _oi in pins:
            # 深いITMの本質価値が上位を占領していないこと
            self.assertLess(abs(k - F_TRUE) / F_TRUE, 0.11,
                            f"現値から離れた {k:,.0f} がピン候補に入っている")

    def test_put_book(self):
        book = sa.put_book(self.chain, self.curve, self.fwd.forward)
        self.assertGreater(book.effective_delta, 0)
        self.assertGreater(book.premium_oku, 0)
        self.assertGreater(book.vega_oku, 0)
        # 実効デルタは建玉総数を超えない
        self.assertLess(book.effective_delta, book.oi)

    def test_sq_bands(self):
        walls = sa.wall_map(self.chain, self.curve, self.fwd.forward)
        bands = sa.sq_bands(self.chain, self.curve, self.fwd.forward,
                            sa.iron_condor(walls))
        lo50, hi50 = bands.band50
        lo80, hi80 = bands.band80
        self.assertTrue(lo80 < lo50 < hi50 < hi80, "50%帯が80%帯の内側にない")
        self.assertLess(lo50, self.fwd.forward)
        self.assertGreater(hi50, self.fwd.forward)
        # 1σ は F × IV × √T
        self.assertAlmostEqual(bands.sigma_abs,
                               F_TRUE * self.curve.atm_iv() * math.sqrt(T_TRUE),
                               delta=5.0)
        self.assertIsNotNone(bands.prob_in_condor)
        self.assertGreater(bands.prob_in_condor, 5,
                           "Condor に収まる確率が 0 付近＝上下の取り違えの疑い")
        self.assertLess(bands.prob_in_condor, 100)

    def test_sq_band_cross_check_is_tight_on_clean_book(self):
        walls = sa.wall_map(self.chain, self.curve, self.fwd.forward)
        bands = sa.sq_bands(self.chain, self.curve, self.fwd.forward,
                            sa.iron_condor(walls))
        # 無矛盾な合成板なので2手法の乖離は小さく、信頼できる判定になるはず
        self.assertIsNotNone(bands.divergence)
        self.assertLess(abs(bands.divergence), 500,
                        f"クロスチェック乖離が大きすぎる: {bands.divergence}")
        self.assertTrue(bands.reliable)


class TestDayOverDay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prev = build_chain(oi_shift=0)
        cls.today = build_chain(oi_shift=300)   # 60,000 に +300、67,000 に +600
        cls.fwd = sa.implied_forward(cls.today, 67500).forward
        cls.curve = sa.build_iv_curve(cls.today, cls.fwd, T_TRUE)

    def test_common_window_totals(self):
        cmp = sa.compare_window(self.today, self.prev)
        self.assertIsNotNone(cmp)
        self.assertEqual(cmp.put_today - cmp.put_prev, 900)  # 300 + 600
        self.assertEqual(cmp.call_today - cmp.call_prev, 0)
        self.assertGreater(cmp.pcr_today, cmp.pcr_prev)

    def test_changes_are_listed(self):
        cmp = sa.compare_window(self.today, self.prev)
        changed = {k: d for k, _, _, d in cmp.put_changes}
        self.assertEqual(changed.get(67000.0), 600)
        self.assertEqual(changed.get(60000.0), 300)

    def test_effective_delta_decomposition(self):
        book_t = sa.put_book(self.today, self.curve, self.fwd)
        book_p = sa.put_book(self.prev, self.curve, self.fwd)
        dec = sa.decompose_effective_delta(self.today, self.prev, book_t,
                                           book_p.effective_delta)
        # 現値・IVが同じなら増分はすべてポジション要因で説明できる
        self.assertAlmostEqual(dec.position, dec.total_change, delta=0.5)
        self.assertAlmostEqual(dec.spot_and_time, 0.0, delta=0.5)
        # 手計算との突き合わせ: 67,000 の +600枚 × その行使価格のデルタ
        d67 = abs(book_t.per_strike_delta[67000.0])
        top = {k: c for k, _, _, _, _, c in dec.contributions}
        self.assertAlmostEqual(top[67000.0], 600 * d67, places=6)

    def test_spot_move_shows_up_as_non_position(self):
        """現値が下がると、建玉が同じでも実効デルタは増える（保険が効き始める）。"""
        lower_f = self.fwd - 1730
        curve_lo = sa.build_iv_curve(self.prev, lower_f, T_TRUE)
        book_lo = sa.put_book(self.prev, curve_lo, lower_f)
        book_hi = sa.put_book(self.prev, self.curve, self.fwd)
        self.assertGreater(book_lo.effective_delta, book_hi.effective_delta)
        dec = sa.decompose_effective_delta(self.prev, self.prev, book_lo,
                                           book_hi.effective_delta)
        self.assertAlmostEqual(dec.position, 0.0, places=6)
        self.assertGreater(dec.spot_and_time, 0)

    def test_bucket_compare(self):
        rows = sa.bucket_compare(self.today, self.prev, self.fwd, "put")
        self.assertTrue(rows)
        self.assertTrue(any(d > 0 for _, _, _, d in rows))


class TestComposite(unittest.TestCase):
    def test_composite_range(self):
        chains = [build_chain("26-09"), build_chain("26-10"), build_chain("26-11")]
        comp = sa.composite_range(chains)
        self.assertIsNotNone(comp.ceiling)
        self.assertIsNotNone(comp.floor)
        self.assertGreater(comp.ceiling[1], 0)
        self.assertLess(comp.floor[1], 0)
        self.assertEqual(len(comp.per_month), 3)
        for _, c, p, pcr in comp.per_month:
            self.assertGreater(p, c)   # PUT優勢の板
            self.assertGreater(pcr, 1)


class TestSummary(unittest.TestCase):
    """初心者向けのひとことまとめ。数字は本文と同じで、言い換えだけを行う。"""

    def setUp(self):
        import sq_report as sr
        from datetime import date
        import sq_fetch as sf
        self.sr, self.sf, self.date = sr, sf, date
        self.chain = build_chain()
        self.a = sr.MonthAnalysis(self.chain, 67500, date(2026, 8, 18))
        self.m = sr.flatten_metrics(self.a, None, None, None)

    def _summary(self, pm=None, spot=None, decomp=None):
        return self.sr.build_summary(self.a, self.m, pm, spot, decomp)

    def test_headline_follows_regime(self):
        su = self._summary()
        if self.m["gex_regime"] == "ショートガンマ":
            self.assertIn("増幅", su["headline"])
            self.assertEqual(su["level"], "warn")
        else:
            self.assertIn("吸収", su["headline"])
            self.assertEqual(su["level"], "ok")

    def test_headline_stays_plain_but_sub_names_the_term(self):
        """
        見出しと図は平易な語のまま、説明と箇条書きは本文と同じ用語を使う。
        図で位置関係をつかみ、文章で正確な指標名を追える形にしている。
        """
        su = self._summary()
        for word in ("ガンマ", "GEX", "デルタ", "IV", "スキュー"):
            self.assertNotIn(word, su["headline"])
        self.assertIn("ガンマ", su["sub"])
        # はしご図のラベルは平易なまま
        labels = {l["label"] for l in su["levels"]}
        self.assertIn("切替ライン", labels)
        self.assertIn("いまここ", labels)

    def test_points_use_report_terminology(self):
        su = self._summary(pm={**self.m, "rr25": self.m["rr25"] + 2.0})
        labels = [p["label"] for p in su["points"]]
        self.assertIn("レジーム", labels)
        self.assertIn("RR25", labels)
        regime = next(p for p in su["points"] if p["label"] == "レジーム")["text"]
        self.assertIn("GEXフリップ", regime)
        self.assertIn("Net GEX at ATM", regime)

    def test_levels_are_sorted_and_labelled(self):
        su = self._summary()
        vals = [l["value"] for l in su["levels"]]
        self.assertEqual(vals, sorted(vals, reverse=True))
        keys = {l["key"] for l in su["levels"]}
        self.assertIn("now", keys)
        self.assertIn("flip", keys)
        for l in su["levels"]:
            self.assertTrue(l["label"])
            self.assertTrue(l["display"])

    def test_countdown_and_disclaimer(self):
        su = self._summary()
        self.assertIn("SQまであと", su["countdown"])
        self.assertIn("予測ではない", su["disclaimer"])

    def test_market_line_uses_spot(self):
        spot = self.sf.SpotQuote(close=66216.79, prev_close=65326.42,
                                 change=890.37, change_pct=1.36)
        su = self._summary(spot=spot)
        market = next(p for p in su["points"] if p["label"] == "相場")
        self.assertIn("66,217", market["text"])
        self.assertIn("+890", market["text"])
        self.assertIn("+1.4%", market["text"])

    def test_rr25_direction_and_driver(self):
        eased = self._summary(pm={**self.m, "rr25": self.m["rr25"] + 2.0})
        text = next(p for p in eased["points"] if p["label"] == "RR25")["text"]
        self.assertIn("縮小", text)
        self.assertIn("駆動は", text)

        worse = self._summary(pm={**self.m, "rr25": self.m["rr25"] - 2.0})
        text = next(p for p in worse["points"] if p["label"] == "RR25")["text"]
        self.assertIn("拡大", text)

        flat = self._summary(pm={**self.m, "rr25": self.m["rr25"] + 0.1})
        text = next(p for p in flat["points"] if p["label"] == "RR25")["text"]
        self.assertIn("横ばい", text)

    def test_effective_delta_shows_the_three_way_split(self):
        import sq_analytics as sa
        prev = build_chain()
        book = sa.put_book(self.chain, self.a.curve, self.a.forward)
        # 建玉が同じで現値だけ動いた状況＝ポジション要因ゼロ
        dec = sa.decompose_effective_delta(self.chain, prev, book,
                                           book.effective_delta - 3000)
        su = self._summary(decomp=dec)
        text = next(p for p in su["points"] if p["label"] == "実効デルタ")["text"]
        self.assertIn("ポジション要因", text)
        self.assertIn("現値移動", text)
        self.assertIn("効き始めた", text)

    def test_stays_within_about_twenty_lines(self):
        """ぱっと見で読める分量に収まっていること。"""
        su = self._summary()
        lines = 2 + len(su["levels"]) + len(su["points"]) + 2
        self.assertLessEqual(lines, 20, f"まとめが長すぎる: {lines}行相当")


class TestLadderTemplate(unittest.TestCase):
    """はしご図のテンプレート。線が見えなくなる指定を入れないための歯止め。"""

    def setUp(self):
        import os
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "sq_report.html"), encoding="utf-8") as f:
            self.html = f.read()

    def test_wall_lines_use_a_visible_colour(self):
        """
        壁の線に --card-border を使うと帯の色に沈んで見えなくなる（実際に踏んだ）。
        4本の水準すべてが見える色で描かれること。
        """
        import re
        m = re.search(r"\.ladder \.lv \.bar\{[^}]*\}", self.html)
        self.assertIsNotNone(m, "はしご図の線の指定が見つからない")
        rule = m.group(0)
        self.assertIn("border-top", rule)
        self.assertNotIn("var(--card-border)", rule,
                         "壁の線が背景に沈む色になっている")

    def test_all_four_level_kinds_are_styled(self):
        for cls in (".ladder .lv.flip .bar", ".ladder .lv.now .bar"):
            self.assertIn(cls, self.html, f"{cls} の指定が無い")

    def test_summary_is_its_own_first_page(self):
        """印刷・PDF化したとき、1ページ目がまとめ、2ページ目から詳細になること。"""
        self.assertIn("@media print", self.html)
        self.assertIn(".page1{break-after:page;}", self.html.replace(" ", "").replace("\n", ""))
        # まとめは page1 の中に入れている
        self.assertIn("$('section','page1')", self.html)

    def test_print_uses_a_light_palette(self):
        """画面は暗い配色なので、紙では明るい配色に入れ替わること。"""
        import re
        m = re.search(r"@media print\{(.*?)\n  \}", self.html, re.S)
        self.assertIsNotNone(m, "印刷用の指定が見つからない")
        block = m.group(1)
        self.assertIn("--bg:#fff", block.replace(" ", ""))
        self.assertIn("--text:#111418", block.replace(" ", ""))

    def test_legend_carries_text_not_colour_alone(self):
        """緑と赤は色覚多様性で見分けにくいので、凡例に文字が必ず付くこと。"""
        self.assertIn("切替ラインより上", self.html)
        self.assertIn("切替ラインより下", self.html)


class TestAnswerCheck(unittest.TestCase):
    """監視ポイントの自動評価。"""

    def setUp(self):
        import sq_report as sr
        self.sr = sr

    def test_sign_flip_is_reported(self):
        """GEXの符号反転はレジーム転換そのもの。閾値が無くても明示すること。"""
        watch = [{"id": "gex", "metric": "gex_at_atm", "value": 43.1,
                  "label": "Net GEX at ATM", "rule": ""}]
        out = self.sr.build_answer_check(watch, {"gex_at_atm": -347.4}, {})
        self.assertIn("符号が反転", out[0]["verdict"])
        self.assertIn("＋→−", out[0]["verdict"])

    def test_no_sign_flip_when_same_side(self):
        watch = [{"id": "gex", "metric": "gex_at_atm", "value": -100.0,
                  "label": "Net GEX at ATM", "rule": ""}]
        out = self.sr.build_answer_check(watch, {"gex_at_atm": -347.4}, {})
        self.assertNotIn("符号が反転", out[0]["verdict"])

    def test_threshold_and_sign_flip_combine(self):
        watch = [{"id": "rr", "metric": "rr25", "value": -1.0, "label": "RR25",
                  "rule": "", "hi": 6.0, "hi_means": "パニック域"}]
        out = self.sr.build_answer_check(watch, {"rr25": 6.5}, {})
        self.assertIn("符号が反転", out[0]["verdict"])
        self.assertIn("パニック域", out[0]["verdict"])

    def test_missing_metric_is_flagged(self):
        watch = [{"id": "x", "metric": "not_computed", "value": 1.0, "label": "X", "rule": ""}]
        out = self.sr.build_answer_check(watch, {}, {})
        self.assertIn("算出できず", out[0]["verdict"])

    def test_wall_metrics_cover_strikes_far_from_forward(self):
        """
        現値から離れた壁でも答え合わせできること。
        範囲が狭いと、前日に監視した壁が翌日に評価不能になる（実データで踏んだ）。
        """
        chain = build_chain()
        keys = self.sr.wall_oi_metrics(chain, 65470.0)
        # 65,470 の 8.4%下にある 60,000 が含まれること
        self.assertIn("wall_oi_put_60000", keys)
        self.assertGreater(keys["wall_oi_put_60000"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
