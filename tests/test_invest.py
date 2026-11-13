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
        """普通模式：单只不许超过总资产三成。"""
        p = build()
        p.config = {"invest_aggressive": False}
        self.assertTrue(p.invest_buy("600519", qty=100)["ok"])
        self.assertTrue(p.invest_buy("600519", qty=100)["ok"])
        again = p.invest_buy("600519", qty=200)   # 再买 2 万就到 4 成
        self.assertFalse(again["ok"], again)
        self.assertIn("太多", again["reason"])

    def test_aggressive_weight_cap(self):
        """FX 战士：单只放宽到六成，但六成以上照样拦。"""
        p = build()
        self.assertTrue(p.invest_buy("600519", qty=100)["ok"])
        self.assertTrue(p.invest_buy("600519", qty=100)["ok"])
        self.assertTrue(p.invest_buy("600519", qty=200)["ok"])   # 4 成，允许
        blocked = p.invest_buy("600519", qty=300)                # 7 成，超限
        self.assertFalse(blocked["ok"], blocked)
        self.assertIn("太多", blocked["reason"])

    def test_buy_goes_into_flow_not_month_spent(self):
        """买股是资产转换：进流水，但不进「本月花」。"""
        p = build()
        p.invest_buy("600519", qty=100)
        self.assertEqual(p._ledger.month_spent, 0.0)
        self.assertIn("stock_buy", [h.get("kind") for h in p._history])
        self.assertIn("买入", p._history[-1].get("reason", ""))

    def test_sell_goes_into_flow_not_month_income(self):
        p = build()
        p.invest_buy("600519", qty=100)
        p.invest_sell("600519", all_out=True)
        self.assertEqual(p._ledger.month_income, 0.0)
        self.assertIn("stock_sell", [h.get("kind") for h in p._history])

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

    def test_first_run_buys_100_shares_once(self):
        """第一次装好先买一手：她得先有股票，才谈得上知道股票这回事。"""
        p = build(quotes={
            "sh600519": quote("sh600519", "贵州茅台", 1000.0, 990.0),   # 一手 10 万，太贵
            "sz000001": quote("sz000001", "平安银行", 10.0, 10.0),      # 一手 1000，买得起
        })
        p.config = {"invest_watchlist": "600519,000001"}
        p._seed_invest_once()
        self.assertTrue(p._state.get("invest_seeded"))
        self.assertEqual(p._positions()["sz000001"]["qty"], 100)
        self.assertNotIn("sh600519", p._positions(), "买不起一手就换下一只")

    def test_first_run_does_not_repeat(self):
        p = build(quotes={"sz000001": quote("sz000001", "平安银行", 10.0, 10.0)})
        p.config = {"invest_watchlist": "000001"}
        p._seed_invest_once()
        balance = p._ledger.balance
        p._seed_invest_once()
        self.assertAlmostEqual(p._ledger.balance, balance, msg="只买一次")

    def test_first_run_without_quote_retries_later(self):
        p = build(quotes={})
        p.config = {"invest_watchlist": "000001"}
        p._seed_invest_once()
        self.assertFalse(p._state.get("invest_seeded"), "行情拿不到就下次再试")

    def test_invest_tick_records_day_open(self):
        """开盘记的是持仓市值；他转钱进来不能算成当日盈利。"""
        p = build()
        p.invest_buy("600519", qty=100)
        p._invest_tick()
        day = p._state.get("invest_day") or {}
        self.assertTrue(day.get("date"))
        self.assertIn("open_value", day)
        self.assertGreater(day.get("open_value"), 0)
        before = p.mood_signal()
        p.borrow(5000)
        after = p.mood_signal()
        self.assertAlmostEqual(after["day_pnl_pct"], before["day_pnl_pct"], places=2)


if __name__ == "__main__":
    unittest.main()


class LeekTests(unittest.TestCase):
    """韭菜特质：涨了上头提加仓，亏了死扛。"""

    def _plugin(self, pct):
        p = build(quotes={"sh601318": quote("sh601318", "中国平安", 50.0, 50.0)})
        p.config = {"invest_watchlist": "601318"}
        p.mood_signal = lambda: {"day_pnl_pct": pct, "day_pnl": pct * 10}
        return p

    def test_overconfident_on_big_gain(self):
        p = self._plugin(4.0)
        self.assertEqual(p.market_stance()["stance"], "overconfident")

    def test_hodl_on_big_loss(self):
        p = self._plugin(-4.0)
        self.assertEqual(p.market_stance()["stance"], "hodl")

    def test_normal_on_small_move(self):
        p = self._plugin(0.5)
        self.assertEqual(p.market_stance()["stance"], "")

    def test_stance_sticky_for_the_day(self):
        p = self._plugin(4.0)
        first = p.market_stance()
        p.mood_signal = lambda: {"day_pnl_pct": 0.1, "day_pnl": 0}
        self.assertEqual(p.market_stance()["stance"], first["stance"], "当天触发过就锁住")

    def test_overconfident_buys_without_approval(self):
        """上头时她自己加仓：没有审批、没有按钮。"""
        p = self._plugin(4.0)
        p._leek_add_position()
        record = p._state.get("invest_leek") or {}
        self.assertEqual(record.get("kind"), "bought", record)
        self.assertTrue(p._positions(), "应该真的买了")

    def test_overconfident_runs_once_per_day(self):
        p = self._plugin(4.0)
        p._leek_add_position()
        first = p._state.get("invest_leek")
        balance = p._ledger.balance
        p._leek_add_position()
        self.assertEqual(p._state.get("invest_leek"), first)
        self.assertAlmostEqual(p._ledger.balance, balance, msg="一天只加一次")

    def test_no_money_is_recorded_for_sulking(self):
        """没钱加仓：记下来，让她去撒娇要钱。"""
        p = self._plugin(4.0)
        p._ledger.balance = 100.0
        p._leek_add_position()
        record = p._state.get("invest_leek") or {}
        self.assertEqual(record.get("kind"), "no_money", record)

    def test_position_cap_blocks_and_records(self):
        p = self._plugin(4.0)
        p._ledger.balance = 10 ** 7
        p.invest_buy("sh601318", qty=100)
        # 把权重顶到上限：手动加一笔，让 room 归零
        p._positions()["sh601318"]["qty"] = 10 ** 5
        p._leek_add_position()
        record = p._state.get("invest_leek") or {}
        self.assertIn(record.get("kind"), ("no_room", "failed"), record)


if __name__ == "__main__":
    unittest.main()


class LeekTests(unittest.TestCase):
    """韭菜特质：涨了上头提加仓，亏了死扛。"""

    def _plugin(self, pct):
        p = build(quotes={"sh601318": quote("sh601318", "中国平安", 50.0, 50.0)})
        p.config = {"invest_watchlist": "601318"}
        p.mood_signal = lambda: {"day_pnl_pct": pct, "day_pnl": pct * 10}
        return p

    def test_overconfident_on_big_gain(self):
        p = self._plugin(4.0)
        self.assertEqual(p.market_stance()["stance"], "overconfident")

    def test_hodl_on_big_loss(self):
        p = self._plugin(-4.0)
        self.assertEqual(p.market_stance()["stance"], "hodl")

    def test_normal_on_small_move(self):
        p = self._plugin(0.5)
        self.assertEqual(p.market_stance()["stance"], "")

    def test_stance_sticky_for_the_day(self):
        p = self._plugin(4.0)
        first = p.market_stance()
        p.mood_signal = lambda: {"day_pnl_pct": 0.1, "day_pnl": 0}
        self.assertEqual(p.market_stance()["stance"], first["stance"], "当天触发过就锁住")
