# -*- coding: utf-8 -*-
"""薪资：人格优先，其次城市平均；发薪日只发一次。

现实里她总得有个收入，不然月底就只剩「余额不足」。人格里没写薪资很常见，
所以这里锁三件事：

1. 人格没写 → 按城市平均（贵阳二线 7800），不凭空编 25000；
2. 人格写了 → 用那个数，但手动配置永远优先，不会被旧人格覆盖；
3. 发薪日（默认 10 号）真的会入账，且一个月只发一次。
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if HERE not in sys.path:
    sys.path.insert(0, HERE)


import main as M  # noqa: E402
from ledger import Ledger, average_salary, payroll  # noqa: E402


def build(config=None, city="贵阳", state=None):
    """只拼薪资相关的属性，避开插件初始化（不落盘）。"""
    plugin = object.__new__(M.AmoFinancePlugin)
    plugin.config = dict(config or {})
    plugin._state = dict(state or {})
    plugin._history = []
    plugin._ledger = Ledger()
    plugin._save = lambda: None
    plugin._log = lambda entry: plugin._history.append(entry)
    plugin._city = lambda: city
    plugin._companion = lambda: None
    return plugin


class SalaryTests(unittest.TestCase):
    def test_average_salary_by_city_tier(self):
        self.assertEqual(average_salary("北京"), 13000.0)
        self.assertEqual(average_salary("杭州"), 10000.0)
        self.assertEqual(average_salary("贵阳"), 7800.0)
        self.assertEqual(average_salary("某某县"), 7800.0, "认不出按二线")

    def test_no_persona_salary_uses_city_average(self):
        plugin = build()
        info = plugin.salary_info()
        self.assertEqual(info["gross"], 7800.0)
        self.assertEqual(info["source"], "城市平均（二线）")
        self.assertAlmostEqual(info["net"], payroll(7800)["net"])

    def test_persona_salary_wins_over_average(self):
        plugin = build(state={"salary_persona": 12000})
        info = plugin.salary_info()
        self.assertEqual(info["gross"], 12000.0)
        self.assertEqual(info["source"], "人格设定")

    def test_manual_salary_wins_over_persona(self):
        plugin = build(config={"salary": 15000}, state={"salary_persona": 12000})
        info = plugin.salary_info()
        self.assertEqual(info["gross"], 15000.0)
        self.assertEqual(info["source"], "手动设置")

    def test_persona_push_does_not_override_manual(self):
        plugin = build(config={"salary": 15000})
        result = plugin.set_salary(30000, source="persona")
        self.assertTrue(result.get("skipped"), result)
        self.assertEqual(plugin.config["salary"], 15000)

    def test_manual_set_writes_config(self):
        plugin = build()
        result = plugin.set_salary(9000, source="manual")
        self.assertTrue(result["ok"])
        self.assertEqual(plugin.config["salary"], 9000)

    def test_persona_push_saves_state(self):
        plugin = build()
        plugin.set_salary(11000, source="persona")
        self.assertEqual(plugin._state["salary_persona"], 11000)
        self.assertEqual(plugin.salary_info()["gross"], 11000.0)

    def test_tick_pays_salary_once_per_month(self):
        plugin = build(config={"bonus_enable": False})
        plugin.tick(now=datetime(2026, 11, 10))
        expected = payroll(7800)["net"]
        self.assertAlmostEqual(plugin._ledger.balance, expected)
        self.assertEqual(plugin._state.get("last_pay"), "2026-11")
        plugin.tick(now=datetime(2026, 11, 12))
        self.assertAlmostEqual(plugin._ledger.balance, expected, msg="同一个月不能发两次")

    def test_first_salary_has_bonus(self):
        """首月给一笔签约/年终的意味，之后按月正常发。"""
        plugin = build()
        plugin.tick(now=datetime(2026, 11, 10))
        with_bonus = payroll(7800, 780)["net"]
        self.assertAlmostEqual(plugin._ledger.balance, with_bonus)

    def test_tick_before_payday_does_not_pay(self):
        plugin = build()
        plugin.tick(now=datetime(2026, 11, 3))
        self.assertAlmostEqual(plugin._ledger.balance, 0.0)


if __name__ == "__main__":
    unittest.main()
