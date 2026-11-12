# -*- coding: utf-8 -*-
"""行情：只取「现在多少钱」，不做任何预测。

数据源是腾讯的公开行情接口（不需要 key）：A 股、港股、美股都能查。
**拿不到就返回空**，绝不编一个价格——价格是账本的一部分，编出来的盈亏会让她
的情绪和实际资产对不上。
"""

from __future__ import annotations

import time
import urllib.request
from typing import Any

QUOTE_URL = "https://qt.gtimg.cn/q="
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; amo-finance/1.0)",
    "Referer": "https://finance.qq.com/",
}

#: 没配汇率时的粗略折算（只用于把港币/美元资产折成人民币记账）
DEFAULT_FX = {"CNY": 1.0, "HKD": 0.92, "USD": 7.15}


def normalize(symbol: str) -> str:
    """把「600519 / 00700 / AAPL」统一成腾讯的写法：sh600519 / hk00700 / usAAPL。"""
    raw = str(symbol or "").strip().upper().replace(" ", "")
    if not raw:
        return ""
    for prefix in ("SH", "SZ", "HK", "US"):
        if raw.startswith(prefix) and len(raw) > len(prefix):
            rest = raw[len(prefix):]
            if prefix == "US":
                return "us" + rest
            if rest.isdigit():
                return prefix.lower() + rest.zfill(5 if prefix == "HK" else 6)
            return ""
    if raw.isdigit():
        if len(raw) == 5:
            return "hk" + raw
        if len(raw) == 6:
            return ("sh" if raw[0] == "6" else "sz") + raw
        return ""
    if raw.isalpha():
        return "us" + raw
    return ""


def market_of(symbol: str) -> str:
    """cn / hk / us。"""
    code = normalize(symbol)
    if code.startswith("hk"):
        return "hk"
    if code.startswith("us"):
        return "us"
    return "cn"


def currency_of(symbol: str) -> str:
    return {"cn": "CNY", "hk": "HKD", "us": "USD"}[market_of(symbol)]


def _parse_line(line: str) -> tuple[str, dict[str, Any]] | None:
    line = line.strip()
    if not line.startswith("v_") or "=" not in line:
        return None
    key, body = line.split("=", 1)
    fields = body.strip().rstrip(";").strip('"').split("~")
    if len(fields) < 33:
        return None
    code = key[2:]
    try:
        price = float(fields[3] or 0)
        prev = float(fields[4] or 0)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None            # 停牌/无成交：不当成有效价格
    try:
        pct = float(fields[32] or 0)
    except (TypeError, ValueError):
        pct = (price - prev) / prev * 100 if prev else 0.0
    return code, {
        "symbol": code,
        "name": fields[1].strip(),
        "price": round(price, 3),
        "prev_close": round(prev, 3),
        "change": round(price - prev, 3) if prev else 0.0,
        "pct": round(pct, 2),
        "currency": currency_of(code),
    }


class QuoteBoard:
    """行情缓存：同一个 symbol 60 秒内只打一次接口。"""

    def __init__(self, timeout: float = 6.0, ttl: float = 60.0, opener=None):
        self.timeout = timeout
        self.ttl = ttl
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._opener = opener or urllib.request.urlopen

    def _fetch(self, codes: list[str]) -> dict[str, dict[str, Any]]:
        url = QUOTE_URL + ",".join(codes)
        request = urllib.request.Request(url, headers=HEADERS)
        with self._opener(request, timeout=self.timeout) as resp:
            raw = resp.read()
        text = raw.decode("gbk", errors="replace")
        out = {}
        for line in text.splitlines():
            parsed = _parse_line(line)
            if parsed:
                code, data = parsed
                out[code] = data
        return out

    def quotes(self, symbols: list[str], force: bool = False) -> dict[str, dict[str, Any]]:
        now = time.time()
        codes, result = [], {}
        for item in symbols:
            code = normalize(item)
            if not code:
                continue
            hit = self._cache.get(code)
            if hit and not force and now - hit[0] <= self.ttl:
                result[code] = dict(hit[1])
            else:
                codes.append(code)
        if codes:
            try:
                fresh = self._fetch(sorted(set(codes)))
            except Exception:
                fresh = {}
            for code, data in fresh.items():
                self._cache[code] = (now, data)
                result[code] = dict(data)
        return result

    def last_price(self, symbol: str) -> float:
        data = self.quotes([symbol]).get(normalize(symbol))
        try:
            return float(data["price"]) if data else 0.0
        except Exception:
            return 0.0
