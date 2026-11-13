# -*- coding: utf-8 -*-
"""扣款幂等：同一个号只扣一次，重启也认；失败不锁死。

账本才是唯一真相，所以幂等号必须落在金融这边（book.json），不能只存在终端的
内存里——否则终端一重启，医疗重试同一笔就会再扣一次。
"""

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


def build(balance=1000.0, state=None):
    plugin = object.__new__(M.AmoFinancePlugin)
    plugin.config = {}
    plugin._state = dict(state or {})
    plugin._history = []
    plugin._ledger = Ledger(balance=balance)
    plugin._save = lambda: None
    plugin._log = lambda entry: plugin._history.append(entry)
    plugin._city = lambda: "贵阳"
    return plugin


class ChargeIdempotencyTests(unittest.TestCase):
    def test_same_request_id_charges_once(self):
        p = build()
        first = p.charge(200, "看病", "医院", request_id="med-1")
        second = p.charge(200, "看病", "医院", request_id="med-1")
        self.assertTrue(first["ok"])
        self.assertTrue(second.get("repeated"))
        self.assertAlmostEqual(p._ledger.balance, 800.0)
        self.assertEqual(len(p._history), 1, "重复的号不该再记一条流水")

    def test_same_id_different_amount_is_a_conflict(self):
        """同一个幂等号换了金额＝调用方拿错号，绝不能当同一笔账静默复用。"""
        p = build()
        p.charge(200, "看病", "医院", request_id="med-x")
        conflict = p.charge(999, "看病", "医院", request_id="med-x")
        self.assertFalse(conflict["ok"])
        self.assertEqual(conflict["code"], "idempotency_conflict")
        self.assertAlmostEqual(p._ledger.balance, 800.0, msg="冲突时不许动钱")
        self.assertEqual(len(p._history), 1, "冲突不该多记一条流水")

    def test_same_id_different_reason_is_a_conflict(self):
        p = build()
        p.charge(200, "看病", "医院", request_id="med-y")
        conflict = p.charge(200, "旅游", "医院", request_id="med-y")
        self.assertEqual(conflict["code"], "idempotency_conflict")
        self.assertAlmostEqual(p._ledger.balance, 800.0)

    def test_same_id_different_payee_is_a_conflict(self):
        p = build()
        p.charge(200, "看病", "医院", request_id="med-z")
        conflict = p.charge(200, "看病", "药店", request_id="med-z")
        self.assertEqual(conflict["code"], "idempotency_conflict")

    def test_legacy_record_without_amount_still_replays(self):
        """老账本只有 ts+result：没有金额可比，照旧按重复返回，不误报冲突。"""
        p = build(state={"charge_ids": {"old-1": {
            "ts": 1.0, "result": {"ok": True, "balance": 900.0}}}})
        replay = p.charge(200, "看病", "医院", request_id="old-1")
        self.assertTrue(replay.get("repeated"))
        self.assertAlmostEqual(p._ledger.balance, 1000.0)

    def test_different_request_id_charges_twice(self):
        p = build()
        p.charge(200, "看病", "医院", request_id="med-1")
        p.charge(200, "买药", "药房", request_id="med-2")
        self.assertAlmostEqual(p._ledger.balance, 600.0)

    def test_replay_survives_restart(self):
        """新实例拿着同一份 state（book.json）= 重启，仍认这个号。"""
        p = build()
        p.charge(200, "看病", "医院", request_id="med-restart")
        state = p._state
        p2 = build(state=state)   # 重新加载同一个 book
        result = p2.charge(200, "看病", "医院", request_id="med-restart")
        self.assertTrue(result.get("repeated"))
        self.assertAlmostEqual(p2._ledger.balance, 1000.0, msg="重启后不能再扣一次")

    def test_failed_charge_not_recorded(self):
        p = build(balance=100)
        result = p.charge(500, "看病", "医院", request_id="med-poor")
        self.assertFalse(result["ok"])
        self.assertNotIn("med-poor", p._state.get("charge_ids", {}))
        # 补钱后同一个号可以重试
        p._ledger.balance = 1000
        retry = p.charge(500, "看病", "医院", request_id="med-poor")
        self.assertTrue(retry["ok"])
        self.assertAlmostEqual(p._ledger.balance, 500.0)

    def test_records_are_capped(self):
        p = build(balance=10 ** 9)
        p.CHARGE_ID_KEEP = 5
        for i in range(12):
            p.charge(1, "小额", "测试", request_id="rid-%d" % i)
        self.assertLessEqual(len(p._state["charge_ids"]), 5)


if __name__ == "__main__":
    unittest.main()
