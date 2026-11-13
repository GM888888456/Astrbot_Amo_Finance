# -*- coding: utf-8 -*-
import unittest

import ledger


class MarketPriceTests(unittest.TestCase):
    def tearDown(self):
        ledger.set_market_prices({})

    def test_market_price_overrides(self):
        ledger.set_market_prices({"番茄": 99.0})
        self.assertAlmostEqual(ledger.ingredient_price("番茄"), 99.0)

    def test_unknown_item_falls_back(self):
        ledger.set_market_prices({"番茄": 99.0})
        price = ledger.ingredient_price("排骨")
        self.assertGreater(price, 0)


if __name__ == "__main__":
    unittest.main()
