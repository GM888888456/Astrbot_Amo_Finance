# -*- coding: utf-8 -*-
"""模拟炒股：持仓、成本、手续费、买卖规则。

这里只有纯计算和规则，不碰网络、不碰账本。钱怎么进出由 ``main.py`` 调账本完成——
**余额永远等于流水之和**这条不能破。

规则按普通散户来：
- A 股一手 100 股，港股/美股按股；
- 手续费：A 股万三（最低 5 元），卖出加印花税 0.05%；港股 0.1% 佣金＋0.1% 印花税；美股 0；
- 单只上限由插件配置给（默认三成）；「FX 战士」模式下放宽到六成、最多六只，
  她自己决定的，没人替她按按钮。
"""

from __future__ import annotations

import math
from typing import Any

#: 单只持仓占总资产上限（默认值；插件配置可覆盖，激进模式更高）
MAX_WEIGHT = 0.30
#: 默认最多同时持有几只
MAX_POSITIONS = 3
#: A 股一手
LOT_CN = 100

#: 自选股（默认模拟「正常人」会看的几只，可在配置里改）
DEFAULT_WATCHLIST = (
    "600519",   # 贵州茅台
    "300750",   # 宁德时代
    "601318",   # 中国平安
    "000001",   # 平安银行
    "00700",    # 腾讯控股
    "AAPL",     # 苹果
)


def fee_for(market: str, amount: float, side: str) -> float:
    """手续费。买入/卖出分别算，A 股卖出多一道印花税。"""
    amount = max(0.0, float(amount))
    if amount <= 0:
        return 0.0
    if market == "hk":
        rate = 0.001 + (0.001 if side == "sell" else 0.0)
        return round(amount * rate, 2)
    if market == "us":
        return 0.0
    commission = max(5.0, amount * 0.0003)
    stamp = amount * 0.0005 if side == "sell" else 0.0
    transfer = amount * 0.00001
    return round(commission + stamp + transfer, 2)


def lot_size(market: str) -> int:
    return LOT_CN if market == "cn" else 1


def can_buy(qty: int, price: float, cash: float, market: str) -> dict[str, Any]:
    """买不买得起。返回 {ok, cost, reason}。"""
    try:
        qty = int(qty)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "股数不对"}
    if qty <= 0 or qty % lot_size(market) != 0:
        return {"ok": False, "reason": "A 股要按 100 股一手买"}
    if price <= 0:
        return {"ok": False, "reason": "没有行情"}
    amount = price * qty
    cost = amount + fee_for(market, amount, "buy")
    if cost > cash:
        return {"ok": False, "reason": "余额不够", "need": round(cost, 2),
                "short": round(cost - cash, 2)}
    return {"ok": True, "cost": round(cost, 2), "amount": round(amount, 2)}


def can_sell(qty: int, holding: int, price: float, market: str) -> dict[str, Any]:
    try:
        qty = int(qty)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "股数不对"}
    if qty <= 0 or qty % lot_size(market) != 0:
        return {"ok": False, "reason": "股数不对"}
    if qty > holding:
        return {"ok": False, "reason": "没那么多股"}
    if price <= 0:
        return {"ok": False, "reason": "没有行情"}
    amount = price * qty
    proceeds = amount - fee_for(market, amount, "sell")
    return {"ok": True, "proceeds": round(proceeds, 2), "amount": round(amount, 2)}


def position_value(pos: dict[str, Any], fx: dict[str, float]) -> dict[str, Any]:
    """一条持仓折算成人民币：市值、成本、浮盈。"""
    qty = int(pos.get("qty") or 0)
    price = float(pos.get("last") or 0)
    cost = float(pos.get("cost") or 0)
    currency = str(pos.get("currency") or "CNY")
    rate = float(fx.get(currency, 1.0) or 1.0)
    market_value = qty * price * rate
    pnl = market_value - cost
    pnl_pct = (pnl / cost * 100.0) if cost > 0 else 0.0
    return {
        "symbol": pos.get("symbol") or "",
        "name": pos.get("name") or "",
        "qty": qty,
        "last": price,
        "currency": currency,
        "cost": round(cost, 2),
        "market_value": round(market_value, 2),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "weight": 0.0,
    }


def summarize(positions: dict[str, dict[str, Any]], fx: dict[str, float]) -> dict[str, Any]:
    """全部持仓的合计：总市值、总成本、浮盈、今日盈亏。"""
    rows = [position_value(pos, fx) for pos in (positions or {}).values() if int(pos.get("qty") or 0) > 0]
    total_value = sum(row["market_value"] for row in rows)
    total_cost = sum(row["cost"] for row in rows)
    total_pnl = total_value - total_cost
    for row in rows:
        row["weight"] = round(row["market_value"] / total_value, 4) if total_value > 0 else 0.0
    day_pnl = 0.0
    for pos in (positions or {}).values():
        qty = int(pos.get("qty") or 0)
        prev = float(pos.get("prev_close") or 0)
        last = float(pos.get("last") or 0)
        if qty > 0 and prev > 0 and last > 0:
            rate = float(fx.get(str(pos.get("currency") or "CNY"), 1.0) or 1.0)
            day_pnl += (last - prev) * qty * rate
    return {
        "positions": sorted(rows, key=lambda r: -r["market_value"]),
        "count": len(rows),
        "total_value": round(total_value, 2),
        "total_cost": round(total_cost, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / total_cost * 100.0, 2) if total_cost > 0 else 0.0,
        "day_pnl": round(day_pnl, 2),
    }


def suggest_actions(positions: dict[str, dict[str, Any]], cash: float,
                    watchlist: list[str], fx: dict[str, float],
                    rng=None, aggressive: bool = False,
                    max_weight: float = MAX_WEIGHT,
                    max_positions: int = MAX_POSITIONS) -> list[dict[str, Any]]:
    """她的一天会干什么。

    普通模式：赚 8% 落袋、亏 10% 割、偶尔拿闲钱买一点。
    **激进模式（FX 战士）**：赚够 20%~30% 才舍得卖；亏了不割，反而补仓摊平；
    仓位没满就一路买，现金留得很少——她要的是暴富，不是稳健。
    """
    import random
    rng = rng or random.Random()
    actions: list[dict[str, Any]] = []
    held = [code for code, pos in (positions or {}).items()
            if int((pos or {}).get("qty") or 0) > 0]

    if aggressive:
        for code, pos in (positions or {}).items():
            qty = int((pos or {}).get("qty") or 0)
            if qty <= 0:
                continue
            info = position_value(pos, fx)
            if info["pnl_pct"] >= rng.uniform(18.0, 30.0):
                lot = 100 if str(pos.get("currency") or "CNY") == "CNY" else 1
                half = (qty // 2 // lot) * lot
                if half <= 0:
                    half = qty
                actions.append({"action": "sell", "symbol": code, "qty": half,
                                "why": "赚了 %.1f%%，先落一半袋" % info["pnl_pct"]})
            elif info["pnl_pct"] <= -15.0 and cash > 1000:
                budget = min(cash * rng.uniform(0.35, 0.70), cash * 0.7)
                actions.append({"action": "buy", "symbol": code,
                                "budget": round(budget, 2),
                                "why": "亏了 %.1f%%，加仓摊平" % info["pnl_pct"]})
        room = max_positions - len(held)
        picks = [c for c in dict.fromkeys(watchlist) if c not in (positions or {})]
        rng.shuffle(picks)
        for code in picks:
            if room <= 0 or cash <= 800:
                break
            budget = min(cash * rng.uniform(0.30, 0.55), cash * 0.55)
            if budget < 800:
                break
            actions.append({"action": "buy", "symbol": code,
                            "budget": round(budget, 2),
                            "why": "感觉要涨，先上车再说"})
            cash -= budget
            room -= 1
        return actions[:4]

    for code, pos in (positions or {}).items():
        qty = int((pos or {}).get("qty") or 0)
        if qty <= 0:
            continue
        info = position_value(pos, fx)
        if info["pnl_pct"] >= 8.0:
            actions.append({"action": "sell", "symbol": code,
                            "qty": qty, "why": "赚了 %.1f%%，落袋为安" % info["pnl_pct"]})
        elif info["pnl_pct"] <= -10.0:
            actions.append({"action": "sell", "symbol": code,
                            "qty": qty, "why": "亏了 %.1f%%，割了" % info["pnl_pct"]})
    # 闲钱买一点：一天最多一笔，仓位 5%~10%
    if cash > 2000 and rng.random() < 0.25 and len(held) < max_positions:
        code = rng.choice([c for c in watchlist if c not in (positions or {})] or [])
        if code:
            budget = min(cash * rng.uniform(0.05, 0.10), cash * 0.2)
            actions.append({"action": "buy", "symbol": code,
                            "budget": round(budget, 2), "why": "手里有点闲钱，买一点放着"})
    return actions


def qty_from_budget(budget: float, price: float, market: str) -> int:
    """按预算算能买几股，按手取整。"""
    if price <= 0 or budget <= 0:
        return 0
    lot = lot_size(market)
    qty = int(math.floor(budget / (price * lot))) * lot
    return max(0, qty)
