# -*- coding: utf-8 -*-
"""房、车按工资和城市自动安排：全款=自，够钱=贷，不够=租。"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ledger import Ledger, monthly_fixed, monthly_loan, plan_assets  # noqa: E402


class PlanTests(unittest.TestCase):
    def test_high_salary_takes_a_mortgage(self):
        led = Ledger()
        plan = plan_assets(led, "贵阳", 20000)
        self.assertEqual(led.house_kind, "loan")
        self.assertGreater(led.mortgage, 1000)
        self.assertEqual(led.house_rent, 0)
        self.assertTrue(plan["house_desc"].endswith("·贷"))
        self.assertIn("·", plan["house_desc"])
        self.assertEqual(led.car_kind, "loan")
        self.assertGreater(led.car_loan, 0)
        self.assertTrue(plan["car_desc"].endswith("·贷"))

    def test_low_salary_rents(self):
        led = Ledger()
        plan = plan_assets(led, "贵阳", 5000)
        self.assertEqual(led.house_kind, "rent")
        self.assertGreater(led.house_rent, 0)
        self.assertEqual(led.mortgage, 0)
        self.assertTrue(plan["house_desc"].endswith("·租"))
        self.assertEqual(led.car_kind, "rent")
        self.assertTrue(plan["car_desc"].endswith("·租"))

    def test_own_house_is_paid_off(self):
        led = Ledger(own_house=True, own_car=True)
        plan = plan_assets(led, "贵阳", 20000)
        self.assertEqual(led.house_kind, "own")
        self.assertEqual(led.mortgage, 0)
        self.assertEqual(led.house_rent, 0)
        self.assertTrue(plan["house_desc"].endswith("·自"))
        self.assertEqual(led.car_kind, "own")
        self.assertEqual(led.car_loan, 0)
        self.assertTrue(plan["car_desc"].endswith("·自"))

    def test_same_seed_keeps_the_same_house(self):
        first = Ledger()
        plan_assets(first, "贵阳", 20000)
        second = Ledger(asset_seed=first.asset_seed)
        plan_assets(second, "贵阳", 20000)
        self.assertEqual(first.house_desc, second.house_desc)
        self.assertEqual(first.car_desc, second.car_desc)

    def test_monthly_loan_math(self):
        self.assertEqual(monthly_loan(0), 0.0)
        payment = monthly_loan(700000)
        self.assertGreater(payment, 3000)
        self.assertLess(payment, 5000)

    def test_fixed_expenses_use_planned_rent(self):
        led = Ledger()
        plan_assets(led, "贵阳", 5000)
        items = monthly_fixed(led, {}, city="贵阳")
        names = {item["name"]: item["amount"] for item in items}
        self.assertIn("房租", names)
        self.assertAlmostEqual(names["房租"], led.house_rent)
        self.assertIn("租车", names)

    def test_fixed_expenses_use_planned_mortgage(self):
        led = Ledger()
        plan_assets(led, "贵阳", 20000)
        items = monthly_fixed(led, {}, city="贵阳")
        names = {item["name"]: item["amount"] for item in items}
        self.assertIn("房贷", names)
        self.assertNotIn("房租", names)
        self.assertAlmostEqual(names["房贷"], led.mortgage)
        self.assertIn("车贷", names)


if __name__ == "__main__":
    unittest.main()
