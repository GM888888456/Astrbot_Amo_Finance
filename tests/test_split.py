# -*- coding: utf-8 -*-
import unittest

import portfolio as pf


class SplitTests(unittest.TestCase):
    def test_10_for_1(self):
        self.assertEqual(pf.split_ratio(1200, 120), 10)

    def test_4_for_1_with_noise(self):
        self.assertEqual(pf.split_ratio(800.0, 200.4), 4)

    def test_normal_drop_is_not_split(self):
        self.assertEqual(pf.split_ratio(100, 90), 0)

    def test_rise_is_not_split(self):
        self.assertEqual(pf.split_ratio(100, 120), 0)

    def test_apply_split_keeps_total_cost(self):
        pos = {"qty": 10, "last": 1000.0, "cost": 10000.0, "prev_close": 1010.0}
        pf.apply_split(pos, 10)
        self.assertEqual(pos["qty"], 100)
        self.assertAlmostEqual(pos["last"], 100.0)
        self.assertAlmostEqual(pos["cost"], 10000.0)


if __name__ == "__main__":
    unittest.main()
