# -*- coding: utf-8 -*-
"""账本引擎测试：钱怎么变。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger import (  # noqa: E402
    CITY_LEVEL,
    Ledger,
    anxiety_from,
    city_level,
    city_profile,
    income_tax,
    monthly_fixed,
    payroll,
)


class PayrollTests(unittest.TestCase):
    def test_25k_take_home(self):
        """新一线 25k：五险一金 22.5%，个税按累进，到手约 1.79 万。"""
        pay = payroll(25000)
        self.assertAlmostEqual(pay["social_total"], 5625.0, places=2)
        self.assertAlmostEqual(pay["tax"], 1465.0, places=2)
        self.assertAlmostEqual(pay["net"], 17910.0, places=2)

    def test_zero_salary(self):
        pay = payroll(0)
        self.assertEqual(pay["tax"], 0.0)
        self.assertEqual(pay["net"], 0.0)

    def test_low_salary_below_threshold(self):
        """低于起征点不缴个税。"""
        pay = payroll(4000)
        self.assertEqual(pay["tax"], 0.0)
        self.assertGreater(pay["net"], 0)

    def test_social_breakdown(self):
        pay = payroll(20000)
        self.assertAlmostEqual(pay["social"]["养老"], 1600.0)
        self.assertAlmostEqual(pay["social"]["医疗"], 400.0)
        self.assertAlmostEqual(pay["social"]["公积金"], 2400.0)

    def test_bonus_increases_tax(self):
        base = payroll(25000)
        with_bonus = payroll(25000, bonuses=5000)
        self.assertGreater(with_bonus["tax"], base["tax"])
        self.assertGreater(with_bonus["net"], base["net"])


class TaxTests(unittest.TestCase):
    def test_negative(self):
        self.assertEqual(income_tax(-100), 0.0)

    def test_first_bracket(self):
        self.assertAlmostEqual(income_tax(3000), 90.0)

    def test_second_bracket(self):
        # 3000*3% + 6000*10% = 90 + 600
        self.assertAlmostEqual(income_tax(9000), 690.0)

    def test_monotonic(self):
        values = [income_tax(x) for x in (1000, 5000, 20000, 50000, 100000)]
        self.assertEqual(values, sorted(values))


class CityTests(unittest.TestCase):
    def test_known_cities(self):
        self.assertEqual(city_level("北京"), "一线")
        self.assertEqual(city_level("杭州"), "新一线")

    def test_unknown_defaults_to_second_tier(self):
        self.assertEqual(city_level("某个县城"), "二线")
        self.assertEqual(city_level(""), "二线")

    def test_rent_differs_by_tier(self):
        self.assertGreater(city_profile("北京")["rent"], city_profile("三线")["rent"])


class LedgerTests(unittest.TestCase):
    def test_afford(self):
        led = Ledger(balance=100)
        self.assertTrue(led.can_afford(100))
        self.assertFalse(led.can_afford(100.01))

    def test_expense(self):
        led = Ledger(balance=500)
        result = led.apply(120, "expense", reason="奶茶")
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(led.balance, 380.0)
        self.assertAlmostEqual(led.month_spent, 120.0)

    def test_expense_rejected_when_broke(self):
        led = Ledger(balance=100)
        result = led.apply(300, "expense", reason="手术")
        self.assertFalse(result["ok"])
        self.assertAlmostEqual(result["short"], 200.0)
        self.assertAlmostEqual(led.balance, 100.0, msg="失败不能扣钱")

    def test_savings_fallback(self):
        led = Ledger(balance=100, savings=1000)
        result = led.apply(300, "expense", from_savings=True)
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(led.balance, 0.0)
        self.assertAlmostEqual(led.savings, 800.0)

    def test_savings_not_used_when_disallowed(self):
        led = Ledger(balance=100, savings=1000)
        result = led.apply(300, "expense", from_savings=False)
        self.assertFalse(result["ok"])

    def test_income(self):
        led = Ledger(balance=100)
        led.apply(500, "income", reason="稿费")
        self.assertAlmostEqual(led.balance, 600.0)
        self.assertAlmostEqual(led.month_income, 500.0)

    def test_transfer_in_counts_as_debt(self):
        led = Ledger(balance=0)
        led.apply(1000, "transfer_in")
        self.assertAlmostEqual(led.balance, 1000.0)
        self.assertAlmostEqual(led.owed_to_user, 1000.0)
        self.assertAlmostEqual(led.month_income, 0.0, msg="转账不算收入")

    def test_repay(self):
        led = Ledger(balance=2000)
        led.apply(1000, "transfer_in")   # 余额 3000，欠 1000
        result = led.repay_user(600)
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["paid"], 600.0)
        self.assertAlmostEqual(led.owed_to_user, 400.0)
        self.assertAlmostEqual(led.balance, 2400.0)

    def test_repay_capped_by_balance(self):
        """还想还 500，但只剩 200 现钱，就只能还 200。"""
        led = Ledger(balance=100)
        led.apply(1000, "transfer_in")   # 余额 1100，欠 1000
        led.apply(900, "expense")       # 余额 200
        result = led.repay_user(500)
        self.assertAlmostEqual(result["paid"], 200.0)
        self.assertAlmostEqual(led.owed_to_user, 800.0)

    def test_settle_month_pays_salary(self):
        led = Ledger(balance=0)
        pay = led.settle_month(25000)
        self.assertAlmostEqual(led.balance, pay["net"])
        self.assertAlmostEqual(led.medical_account, pay["social"]["医疗"])

    def test_zero_amount_rejected(self):
        led = Ledger(balance=100)
        self.assertFalse(led.apply(0, "expense")["ok"])

    def test_snapshot_rounds(self):
        led = Ledger(balance=10.004)
        self.assertAlmostEqual(led.snapshot()["balance"], 10.0, places=2)


class FixedExpenseTests(unittest.TestCase):
    def test_renter_pays_rent(self):
        items = monthly_fixed(Ledger(own_house=False), {}, city="杭州")
        names = [i["name"] for i in items]
        self.assertIn("房租", names)

    def test_owner_does_not_pay_rent(self):
        items = monthly_fixed(Ledger(own_house=True), {}, city="杭州")
        names = [i["name"] for i in items]
        self.assertNotIn("房租", names)
        # 但物业水电照扣
        self.assertIn("物业", names)
        self.assertIn("水电", names)

    def test_own_house_with_mortgage_still_pays_mortgage(self):
        """有房但还在还房贷：房贷不能因为「有房」被吞掉。"""
        items = monthly_fixed(Ledger(own_house=True, mortgage=6000), {}, city="杭州")
        names = [i["name"] for i in items]
        self.assertIn("房贷", names)
        self.assertNotIn("房租", names)

    def test_car_owner_pays_running_cost_not_loan(self):
        items = monthly_fixed(Ledger(own_car=True), {}, city="杭州")
        names = [i["name"] for i in items]
        self.assertIn("油费", names)
        self.assertNotIn("车贷", names)

    def test_loan_payments(self):
        items = monthly_fixed(Ledger(mortgage=6000, car_loan=2000), {}, city="杭州")
        names = [i["name"] for i in items]
        self.assertIn("房贷", names)
        self.assertIn("车贷", names)


class AnxietyTests(unittest.TestCase):
    def test_rich_is_calm(self):
        self.assertEqual(anxiety_from(Ledger(balance=100000), 5000), 0.0)

    def test_broke_is_anxious(self):
        self.assertGreater(anxiety_from(Ledger(balance=100), 5000), 0.8)

    def test_no_fixed_no_anxiety(self):
        self.assertEqual(anxiety_from(Ledger(balance=0), 0), 0.0)


if __name__ == "__main__":
    unittest.main()
