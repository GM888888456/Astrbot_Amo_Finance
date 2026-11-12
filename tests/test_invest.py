# -*- coding: utf-8 -*-
"""模拟炒股接进账本：买入扣钱、卖出回款、拿不到行情就不成交。"""

from __future__ import annotations

import os
import random
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import main as M  # noqa: E402
from ledger import Ledger  # noqa: E402


class FakeBoard:
    def __init__(self, quotes):
        self._quotes = {k: dict(v) for k, v in quotes.items()}
        self.calls = 0

    def quotes(self, symbols, force=False):
        self.calls += 1
        return {code: dict(data) for code, data in self._quotes.items()
                if code in list(symbols)}


def quote(code, name, price, prev=None):
    currency = "CNY" if code.startswith(("sh", "sz")) else ("HKD" if code.startswith("hk") else "USD")
    return {"symbol": code, "name": name, "price": price,
            "prev_close": prev if prev is not None else price,
            "change": 0.0, "pct": 0.0, "currency": currency}


def build(balance=100000.0, quotes=None, state=None):
    plugin = object.__new__(M.AmoFinancePlugin)
    plugin.config = {}
    plugin._state = dict(state or {})
    plugin._history = []
    plugin._ledger = Ledger(balance=balance)
    plugin._save = lambda: None
    plugin._log = lambda entry: plugin._history.append(entry)
    plugin._city = lambda: "贵阳"
    plugin._rng = random.Random(1)
    plugin._report_to_companion = lambda: None
    plugin._quote_board = FakeBoard(quotes if quotes is not None else {
        "sh600519": quote("sh600519", "贵州茅台", 100.0, 99.0),
    })
    return plugin


class InvestTests(unittest.TestCase):
    def test_buy_deducts_cash_and_creates_position(self):
        p = build()
        result = p.invest_buy("600519", qty=100)
        self.assertTrue(result["ok"], result)
        self.assertLess(p._ledger.balance, 100000.0)
        self.assertEqual(p._positions()["sh600519"]["qty"], 100)

    def test_buy_without_quote_fails(self):
        p = build()
        result = p.invest_buy("000001", qty=100)
        self.assertFalse(result["ok"])
        self.assertIn("行情", result["reason"])

    def test_buy_more_than_cash_fails(self):
        p = build(balance=1000.0)
        result = p.invest_buy("600519", qty=100)
        self.assertFalse(result["ok"])
        self.assertEqual(p._positions(), {})

    def test_position_size_cap(self):
        """单只不许超过总资产三成。"""
        p = build()
        self.assertTrue(p.invest_buy("600519", qty=100)["ok"])
        self.assertTrue(p.invest_buy("600519", qty=100)["ok"])
        again = p.invest_buy("600519", qty=200)   # 再买 2 万就到 4 成
        self.assertFalse(again["ok"], again)
        self.assertIn("太多", again["reason"])

    def test_sell_returns_cash_and_clears(self):
        p = build()
        p.invest_buy("600519", qty=100)
        before = p._ledger.balance
        result = p.invest_sell("600519", all_out=True)
        self.assertTrue(result["ok"], result)
        self.assertGreater(p._ledger.balance, before)
        self.assertNotIn("sh600519", p._positions())

    def test_sell_without_position_fails(self):
        p = build()
        self.assertFalse(p.invest_sell("600519", qty=100)["ok"])

    def test_summary_equity(self):
        p = build()
        p.invest_buy("600519", qty=100)
        summary = p.invest_summary()
        self.assertEqual(summary["count"], 1)
        self.assertAlmostEqual(summary["equity"], p._ledger.balance + summary["total_value"], places=2)

    def test_mood_signal_uses_day_open(self):
        p = build(balance=100000.0, state={"invest_day": {"date": "2026-11-12", "open": 100000.0}})
        p.invest_buy("600519", qty=100)
        signal = p.mood_signal()
        self.assertIn("day_pnl_pct", signal)
        self.assertLessEqual(abs(signal["day_pnl_pct"]), 100.0)

    def test_invest_tick_records_day_open(self):
        p = build()
        p._invest_tick()
        day = p._state.get("invest_day") or {}
        self.assertTrue(day.get("date"))
        self.assertGreater(day.get("open"), 0)


if __name__ == "__main__":
    unittest.main()
