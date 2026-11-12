# -*- coding: utf-8 -*-
"""行情：认代码、解析、缓存；拿不到就返回空，绝不编价格。"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import market  # noqa: E402

SAMPLE = (
    'v_sh600519="1~贵州茅台~600519~1410.27~1402.30~1395.13~32373~15499~16874~1410.27~17~'
    '1410.22~1~1410.21~4~1410.19~4~1410.00~1~1410.43~2~1410.45~1~1410.46~1~1410.50~5~'
    '1410.55~1~~20261112161452~7.97~0.57~1411.00~1388.11~1410.27/32373/4536814518~32373~'
    '453681~0.26~21.46~~1411.00~1388.11~1.63~17629.53~17629.53~6.50~1542.53~1262.07~0.91~17~'
    '1401.42~20.51~21.42~~~-0.03~453681.4518~282.0540~20~   A~GP-A~4.53~-2.87~3.69~30.29~'
    '26.74~1539.98~1151.01~4.16~7.41~5.01~1250081601~1250081601~45.95~-4.50~1250081601~~~'
    '-0.21~0.46~~CNY~0~___D__F__N~1408.17~18~";'
)


class NormalizeTests(unittest.TestCase):
    def test_a_share(self):
        self.assertEqual(market.normalize("600519"), "sh600519")
        self.assertEqual(market.normalize("000001"), "sz000001")
        self.assertEqual(market.normalize("300750"), "sz300750")

    def test_hk_and_us(self):
        self.assertEqual(market.normalize("00700"), "hk00700")
        self.assertEqual(market.normalize("AAPL"), "usAAPL")
        self.assertEqual(market.normalize("us nvda"), "usNVDA")

    def test_prefixed(self):
        self.assertEqual(market.normalize("sz000001"), "sz000001")
        self.assertEqual(market.normalize("hk00700"), "hk00700")

    def test_garbage(self):
        self.assertEqual(market.normalize(""), "")
        self.assertEqual(market.normalize("??"), "")

    def test_market_and_currency(self):
        self.assertEqual(market.market_of("600519"), "cn")
        self.assertEqual(market.market_of("00700"), "hk")
        self.assertEqual(market.market_of("AAPL"), "us")
        self.assertEqual(market.currency_of("00700"), "HKD")


class ParseTests(unittest.TestCase):
    def test_parse_real_line(self):
        parsed = market._parse_line(SAMPLE)
        self.assertIsNotNone(parsed)
        code, data = parsed
        self.assertEqual(code, "sh600519")
        self.assertEqual(data["name"], "贵州茅台")
        self.assertAlmostEqual(data["price"], 1410.27)
        self.assertAlmostEqual(data["prev_close"], 1402.30)
        self.assertGreater(data["pct"], 0)

    def test_bad_lines_ignored(self):
        self.assertIsNone(market._parse_line(""))
        self.assertIsNone(market._parse_line("garbage"))
        self.assertIsNone(market._parse_line('v_sh1="1~x~1~0~0"'), "价格 0 当停牌")


class BoardTests(unittest.TestCase):
    def test_cache_avoids_repeat_calls(self):
        calls = []

        class Resp:
            def __init__(self, text):
                self._text = text

            def read(self):
                return self._text.encode("gbk")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(request, timeout=0):
            calls.append(request.full_url)
            return Resp(SAMPLE)

        board = market.QuoteBoard(ttl=60, opener=opener)
        board.quotes(["600519"])
        board.quotes(["600519"])
        self.assertEqual(len(calls), 1)
        board.quotes(["600519"], force=True)
        self.assertEqual(len(calls), 2)

    def test_network_failure_returns_empty(self):
        def boom(*a, **k):
            raise OSError("no network")

        board = market.QuoteBoard(opener=boom)
        self.assertEqual(board.quotes(["600519"]), {})
        self.assertEqual(board.last_price("600519"), 0.0)


if __name__ == "__main__":
    unittest.main()
