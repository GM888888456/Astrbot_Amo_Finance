# -*- coding: utf-8 -*-
"""模拟交易的规则：手续费、够不够买、能不能卖、合计盈亏。"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import portfolio as pf  # noqa: E402

FX = {"CNY": 1.0, "HKD": 0.92, "USD": 7.15}


class FeeTests(unittest.TestCase):
    def test_a_share_minimum_five(self):
        # 万三是 0.3 元，但最低 5 元；再加过户费 0.01 元
        self.assertAlmostEqual(pf.fee_for("cn", 1000, "buy"), 5.01, places=2)

    def test_a_share_sell_has_stamp(self):
        buy = pf.fee_for("cn", 100000, "buy")
        sell = pf.fee_for("cn", 100000, "sell")
        self.assertGreater(sell, buy)
        self.assertAlmostEqual(sell - buy, 50.0, delta=1.0)

    def test_us_free(self):
        self.assertEqual(pf.fee_for("us", 100000, "buy"), 0.0)


class BuySellTests(unittest.TestCase):
    def test_lot_size(self):
        self.assertFalse(pf.can_buy(50, 10, 100000, "cn")["ok"], "A 股要 100 股一手")
        self.assertTrue(pf.can_buy(100, 10, 100000, "cn")["ok"])

    def test_not_enough_cash(self):
        result = pf.can_buy(100, 100, 5000, "cn")
        self.assertFalse(result["ok"])
        self.assertGreater(result["short"], 0)

    def test_sell_more_than_held(self):
        self.assertFalse(pf.can_sell(200, 100, 10, "cn")["ok"])
        self.assertTrue(pf.can_sell(100, 100, 10, "cn")["ok"])

    def test_sell_proceeds_after_fee(self):
        result = pf.can_sell(100, 100, 10, "cn")
        self.assertAlmostEqual(result["proceeds"], 1000 - pf.fee_for("cn", 1000, "sell"))


class SummarizeTests(unittest.TestCase):
    def test_flat_position(self):
        positions = {"sh600519": {"symbol": "sh600519", "name": "茅台", "qty": 100,
                                  "cost": 100000.0, "last": 1000.0,
                                  "prev_close": 1000.0, "currency": "CNY"}}
        summary = pf.summarize(positions, FX)
        self.assertEqual(summary["total_value"], 100000.0)
        self.assertEqual(summary["total_pnl"], 0.0)
        self.assertEqual(summary["day_pnl"], 0.0)

    def test_profit_and_day_change(self):
        positions = {"sh600519": {"symbol": "sh600519", "name": "茅台", "qty": 100,
                                  "cost": 100000.0, "last": 1100.0,
                                  "prev_close": 1080.0, "currency": "CNY"}}
        summary = pf.summarize(positions, FX)
        self.assertEqual(summary["total_pnl"], 10000.0)
        self.assertEqual(summary["day_pnl"], 2000.0)
        self.assertAlmostEqual(summary["total_pnl_pct"], 10.0)

    def test_hk_converted(self):
        positions = {"hk00700": {"symbol": "hk00700", "name": "腾讯", "qty": 100,
                                 "cost": 30000.0, "last": 400.0,
                                 "prev_close": 400.0, "currency": "HKD"}}
        summary = pf.summarize(positions, FX)
        self.assertAlmostEqual(summary["total_value"], 100 * 400 * 0.92)


class SuggestTests(unittest.TestCase):
    def test_take_profit(self):
        positions = {"sh600519": {"symbol": "sh600519", "name": "茅台", "qty": 100,
                                  "cost": 100000.0, "last": 1100.0,
                                  "prev_close": 1100.0, "currency": "CNY"}}
        import random
        actions = pf.suggest_actions(positions, 0, [], FX, random.Random(1))
        self.assertTrue(any(a["action"] == "sell" for a in actions))

    def test_stop_loss(self):
        positions = {"sh600519": {"symbol": "sh600519", "name": "茅台", "qty": 100,
                                  "cost": 100000.0, "last": 800.0,
                                  "prev_close": 800.0, "currency": "CNY"}}
        import random
        actions = pf.suggest_actions(positions, 0, [], FX, random.Random(1))
        self.assertTrue(any(a["action"] == "sell" for a in actions))

    def test_qty_from_budget_rounds_to_lot(self):
        self.assertEqual(pf.qty_from_budget(10000, 33.3, "cn"), 300)
        self.assertEqual(pf.qty_from_budget(1000, 2000, "cn"), 0)
        self.assertEqual(pf.qty_from_budget(1000, 300, "us"), 3)


if __name__ == "__main__":
    unittest.main()
