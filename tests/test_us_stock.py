# -*- coding: utf-8 -*-
"""美股/港股盈亏：成本与市值必须同一币种（人民币）后再算。"""
import unittest

import portfolio as pf

FX = {"CNY": 1.0, "USD": 7.15, "HKD": 0.92}


class UsStockTests(unittest.TestCase):
    def test_us_position_pnl_uses_cny_cost(self):
        # 10 股 NVDA，现价 100 美元，成本 7150 人民币 → 盈亏应接近 0
        pos = {"symbol": "NVDA", "name": "英伟达", "qty": 10, "last": 100.0,
               "cost": 7150.0, "currency": "USD", "cost_cny": True}
        row = pf.position_value(pos, FX)
        self.assertAlmostEqual(row["market_value"], 7150.0, places=2)
        self.assertAlmostEqual(row["pnl"], 0.0, places=2)
        self.assertAlmostEqual(row["pnl_pct"], 0.0, places=2)

    def test_pnl_is_not_inflated_by_fx(self):
        # 成本若还是美元（旧数据），盈亏会被放大 7 倍——这正是 615% 的来源
        bad = {"symbol": "NVDA", "qty": 10, "last": 100.0,
               "cost": 1000.0, "currency": "USD"}
        row = pf.position_value(bad, FX)
        self.assertGreater(row["pnl_pct"], 500)  # 旧数据的样子

        fixed = dict(bad, cost=1000.0 * FX["USD"], cost_cny=True)
        row = pf.position_value(fixed, FX)
        self.assertAlmostEqual(row["pnl_pct"], 0.0, places=2)

    def test_hkd_uses_hkd_rate(self):
        pos = {"symbol": "0700", "qty": 100, "last": 300.0,
               "cost": 30000.0 * FX["HKD"], "currency": "HKD", "cost_cny": True}
        row = pf.position_value(pos, FX)
        self.assertAlmostEqual(row["pnl_pct"], 0.0, places=2)


if __name__ == "__main__":
    unittest.main()
