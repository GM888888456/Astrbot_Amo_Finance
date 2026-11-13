# -*- coding: utf-8 -*-
"""银行语义：储蓄计息、自动还款。"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import main as M  # noqa: E402
from ledger import Ledger  # noqa: E402


def build(balance=10000.0, savings=30000.0, owed=0.0, state=None):
    p = object.__new__(M.AmoFinancePlugin)
    p.config = {"savings_rate": 1.5, "auto_repay": True}
    p._state = dict(state or {})
    p._history = []
    p._ledger = Ledger(balance=balance, savings=savings, owed_to_user=owed)
    p._save = lambda: None
    p._log = lambda entry: p._history.append(entry)
    p._city = lambda: "贵阳"
    p._report_to_companion = lambda: None
    return p


class FakeBoard:
    def __init__(self, quotes):
        self._quotes = quotes

    def quotes(self, symbols, force=False):
        return {k: dict(v) for k, v in self._quotes.items() if k in list(symbols)}


class MoodTests(unittest.TestCase):
    def _with_position(self):
        p = build(balance=10000.0)
        p._state["invest"] = {"positions": {
            "sh600519": {"symbol": "sh600519", "name": "贵州茅台", "qty": 100,
                         "cost": 10000.0, "last": 110.0, "prev_close": 100.0,
                         "currency": "CNY"}}}
        p._state["invest_day"] = {"date": date.today().isoformat(),
                                  "open": 10000.0, "open_value": 10000.0}
        p._quote_board = FakeBoard({"sh600519": {
            "symbol": "sh600519", "name": "贵州茅台", "price": 110.0,
            "prev_close": 100.0, "change": 10.0, "pct": 10.0, "currency": "CNY"}})
        return p

    def test_day_pnl_is_market_only(self):
        p = self._with_position()
        sig = p.mood_signal()
        self.assertAlmostEqual(sig["day_pnl"], 1000.0, places=2)
        self.assertAlmostEqual(sig["day_pnl_pct"], 10.0, places=2)

    def test_deposit_does_not_change_day_pnl(self):
        """他转钱进来不能被算成股市盈利（曾把 7150 算成 +24.54%）。"""
        p = self._with_position()
        before = p.mood_signal()
        p.borrow(5000)
        after = p.mood_signal()
        self.assertAlmostEqual(after["day_pnl"], before["day_pnl"], places=2)
        self.assertAlmostEqual(after["day_pnl_pct"], before["day_pnl_pct"], places=2)


class FakeBoard:
    def __init__(self, quotes):
        self._quotes = quotes

    def quotes(self, symbols, force=False):
        return {k: dict(v) for k, v in self._quotes.items() if k in list(symbols)}


class BankTests(unittest.TestCase):
    def test_savings_interest_once_a_day(self):
        p = build()
        p._state["last_interest_date"] = (date.today() - timedelta(days=1)).isoformat()
        got = p._credit_savings_interest()
        self.assertGreater(got, 1.0)
        self.assertGreater(p._ledger.savings, 30000.0)
        self.assertEqual(p._history[-1]["kind"], "interest")
        self.assertEqual(p._credit_savings_interest(), 0.0)

    def test_auto_repay_uses_spare_cash(self):
        p = build(balance=12000.0, owed=3000.0)
        paid = p._auto_repay()
        self.assertGreater(paid, 0)
        self.assertLess(p._ledger.owed_to_user, 3000.0)
        self.assertEqual(p._history[-1]["kind"], "repay")
        self.assertEqual(p._auto_repay(), 0.0)   # 一天只还一次

    def test_deposit_does_not_count_as_market_gain(self):
        """用户转钱不是炒股盈利：当日盈亏只看现价 vs 昨收。"""
        p = build(balance=10000.0)
        today = date.today().isoformat()
        p._state["invest"] = {"positions": {
            "sh600519": {"symbol": "sh600519", "name": "贵州茅台", "qty": 100,
                         "cost": 10000.0, "last": 110.0, "prev_close": 100.0,
                         "currency": "CNY"}}}
        p._state["invest_day"] = {"date": today, "open": 10000.0,
                                  "open_value": 10000.0}
        p._state["invest_stance"] = {"date": today, "stance": "", "pct": 0}
        p._quote_board = FakeBoard({"sh600519": {
            "symbol": "sh600519", "name": "贵州茅台", "price": 110.0,
            "prev_close": 100.0, "currency": "CNY"}})
        before = p.mood_signal()
        self.assertAlmostEqual(before["day_pnl"], 1000.0, places=2)
        self.assertAlmostEqual(before["day_pnl_pct"], 10.0, places=2)
        p.borrow(5000)
        after = p.mood_signal()
        self.assertAlmostEqual(after["day_pnl"], before["day_pnl"], places=2)
        self.assertAlmostEqual(after["day_pnl_pct"], before["day_pnl_pct"], places=2)

    def test_fx_rate(self):
        p = build()
        p.config = {"fx_usd": 7.2, "fx_hkd": 0.9}
        self.assertAlmostEqual(p.fx_rate("USD"), 7.2)
        self.assertAlmostEqual(p.fx_rate("HKD"), 0.9)
        self.assertAlmostEqual(p.fx_rate("CNY"), 1.0)

    def test_auto_repay_skips_when_no_owed(self):
        p = build(balance=12000.0, owed=0.0)
        self.assertEqual(p._auto_repay(), 0.0)
