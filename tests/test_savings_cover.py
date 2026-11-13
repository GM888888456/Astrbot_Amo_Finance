# -*- coding: utf-8 -*-
"""2.4.1：活期不够时自动从储蓄扣。"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (ROOT, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

import main as M  # noqa: E402
from ledger import Ledger  # noqa: E402


def build(balance=0.0, savings=20000.0, auto_cover=True):
    p = object.__new__(M.AmoFinancePlugin)
    p.config = {"savings_rate": 1.5, "auto_repay": True,
                "savings_auto_cover": auto_cover}
    p._state = {}
    p._history = []
    p._ledger = Ledger(balance=balance, savings=savings)
    p._save = lambda: None
    p._log = lambda entry: p._history.append(entry)
    p._city = lambda: "贵阳"
    p._report_to_companion = lambda: None
    return p


class SavingsCoverTests(unittest.TestCase):
    def test_expense_beyond_cash_uses_savings(self):
        p = build(balance=10.0, savings=20000.0)
        result = p.charge(35.0, reason="外卖", payee="楼下小馆")
        self.assertTrue(result.get("ok"))
        self.assertEqual(p._ledger.balance, 0.0)
        self.assertAlmostEqual(p._ledger.savings, 19975.0, places=2)
        self.assertAlmostEqual(p._ledger.balance + p._ledger.savings, 19975.0, places=2)
        self.assertTrue(p._history and p._history[-1].get("from_savings"))

    def test_can_be_disabled(self):
        p = build(balance=10.0, savings=20000.0, auto_cover=False)
        result = p.charge(35.0, reason="外卖", payee="楼下小馆")
        self.assertFalse(result.get("ok"))
        self.assertAlmostEqual(p._ledger.savings, 20000.0, places=2)

    def test_cash_enough_never_touches_savings(self):
        p = build(balance=100.0, savings=20000.0)
        p.charge(35.0, reason="外卖", payee="楼下小馆")
        self.assertAlmostEqual(p._ledger.balance, 65.0, places=2)
        self.assertAlmostEqual(p._ledger.savings, 20000.0, places=2)

    def test_not_enough_even_with_savings(self):
        p = build(balance=0.0, savings=10.0)
        result = p.charge(35.0, reason="外卖", payee="楼下小馆")
        self.assertFalse(result.get("ok"))
        self.assertAlmostEqual(p._ledger.savings, 10.0, places=2)


if __name__ == "__main__":
    unittest.main()
