# -*- coding: utf-8 -*-
"""她开口借钱：算欠款、不算收入，转账按天封顶、红包按次与累计封顶。"""

from __future__ import annotations

import os
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


def build(balance=100.0, state=None):
    p = object.__new__(M.AmoFinancePlugin)
    p.config = {}
    p._state = dict(state or {})
    p._history = []
    p._ledger = Ledger(balance=balance)
    p._save = lambda: None
    p._log = lambda entry: p._history.append(entry)
    p._city = lambda: "贵阳"
    p._report_to_companion = lambda: None
    return p


class BorrowTests(unittest.TestCase):
    def test_transfer_counts_as_debt_not_income(self):
        p = build(balance=0)
        result = p.borrow(500, "transfer")
        self.assertTrue(result["ok"], result)
        self.assertAlmostEqual(p._ledger.balance, 500.0)
        self.assertAlmostEqual(p._ledger.owed_to_user, 500.0)
        self.assertEqual(p._ledger.month_income, 0.0, "借款不算收入")

    def test_borrow_logs_chinese_reason(self):
        p = build(balance=0)
        p.borrow(500, "transfer")
        self.assertTrue(str(p._history[-1].get("reason") or "").startswith("借款"))

    def test_transfer_in_logs_chinese_reason(self):
        p = build(balance=0)
        p.transfer_in(500, "u")
        self.assertTrue(str(p._history[-1].get("reason") or "").startswith("借款"))

    def test_backfill_history_reasons(self):
        p = build(balance=0)
        p._history = [{"kind": "borrow", "channel": "red", "amount": 100}]
        self.assertTrue(p._backfill_history_reasons())
        self.assertIn("红包", p._history[0]["reason"])

    def test_red_packet_is_gift_and_limit_1000(self):
        p = build(balance=0)
        self.assertFalse(p.borrow(1001, "red")["ok"], "单个红包最多 1000")
        result = p.borrow(1000, "red")
        self.assertTrue(result["ok"])
        self.assertTrue(result.get("gift"))
        self.assertAlmostEqual(p._ledger.owed_to_user, 0.0, msg="红包不用还")
        self.assertAlmostEqual(p._ledger.balance, 1000.0)

    def test_transfer_daily_limit(self):
        p = build(balance=0)
        self.assertTrue(p.borrow(9000, "transfer")["ok"])
        result = p.borrow(2000, "transfer")
        self.assertFalse(result["ok"], "一天最多一万")
        self.assertAlmostEqual(result["left"], 1000.0)

    def test_bad_amount_rejected(self):
        p = build()
        self.assertFalse(p.borrow(0)["ok"])
        self.assertFalse(p.borrow(-5)["ok"])

    def test_borrow_info_shape(self):
        p = build()
        p.borrow(300, "transfer")
        info = p.borrow_info()
        self.assertAlmostEqual(info["transfer_today"], 300.0)
        self.assertAlmostEqual(info["transfer_left"], 9700.0)
        self.assertAlmostEqual(info["owed"], 300.0)

    def test_repay_reduces_debt(self):
        p = build(balance=0)
        p.borrow(500, "transfer")
        p.repay(200)
        self.assertAlmostEqual(p._ledger.owed_to_user, 300.0)


if __name__ == "__main__":
    unittest.main()
