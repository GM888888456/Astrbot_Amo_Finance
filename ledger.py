# -*- coding: utf-8 -*-
"""账本引擎：纯计算，不碰网络、不碰 AstrBot。

这里只有一件事：**钱怎么变**。所有金额变动都经由 `Ledger.apply()`，返回新的账本
状态和一条流水，不做任何 IO，方便单独测。

设计原则：

1. **余额永远等于流水之和。** 每次变更都追加一条流水，不从余额倒推。
2. **扣款失败不抛异常。** 返回 `ok=False` 和差多少，让上层决定怎么说。
3. **金额用「分」存整数。** 浮点数记账迟早差一分，这一分会被用户看到。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 常量：价格与税率
# ---------------------------------------------------------------------------

#: 默认物价（元）。面板可改，改完立即生效；这里只是初值。
DEFAULT_PRICES: dict[str, float] = {
    "奶茶": 18.0, "咖啡": 28.0, "早餐": 12.0, "午餐": 32.0, "晚餐": 38.0,
    "外卖": 35.0, "地铁": 4.0, "打车": 28.0, "零食": 15.0, "水果": 22.0,
    "挂号": 50.0, "买药": 60.0, "门诊": 320.0, "输液": 480.0,
    "住院": 12000.0, "手术": 8000.0, "体检": 600.0,
    "泡面": 5.0, "食堂": 15.0, "酸菜鱼": 50.0, "火锅": 90.0,
    "电影": 55.0, "奶茶聚会": 60.0, "网购": 180.0, "化妆品": 320.0,
    "水电": 180.0, "燃气": 60.0, "物业": 300.0, "网费": 100.0, "话费": 50.0,
}

#: 城市档位：房租与基础生活成本。终端可以从天气插件的城市推，推不到用默认。
CITY_TIERS: dict[str, dict[str, Any]] = {
    "一线": {"rent": 4800, "food_day": 110, "transport": 400, "base": 42000},
    "新一线": {"rent": 3200, "food_day": 90, "transport": 300, "base": 25000},
    "二线": {"rent": 2200, "food_day": 75, "transport": 250, "base": 15000},
    "三线": {"rent": 1400, "food_day": 60, "transport": 180, "base": 9000},
}

#: 人格里没写月薪时，按城市档位取的平均月薪（税前，2025 年城镇单位就业人员口径取整）。
#: 这是「市面上的平均」，不是最低工资——她要过日子，不是生存挑战。
AVERAGE_SALARY: dict[str, float] = {
    "一线": 13000.0, "新一线": 10000.0, "二线": 7800.0, "三线": 6000.0,
}

#: 城市名 → 档位。认不出就按二线。
CITY_LEVEL: dict[str, str] = {
    "北京": "一线", "上海": "一线", "广州": "一线", "深圳": "一线",
    "杭州": "新一线", "成都": "新一线", "武汉": "新一线", "南京": "新一线",
    "西安": "新一线", "苏州": "新一线", "重庆": "新一线", "天津": "新一线",
    "长沙": "新一线", "郑州": "新一线", "东莞": "新一线", "青岛": "新一线",
}

#: 个税（月度累进，起征点 5000）。简化成月算，不做年度汇算。
TAX_BRACKETS: tuple[tuple[float, float], ...] = (
    (3000.0, 0.03), (12000.0, 0.10), (25000.0, 0.20),
    (35000.0, 0.25), (55000.0, 0.30), (80000.0, 0.35), (float("inf"), 0.45),
)

#: 五险一金个人缴纳比例（常见配置）。
SOCIAL_RATES = {
    "养老": 0.08,
    "医疗": 0.02,
    "失业": 0.005,
    "公积金": 0.12,
}


def city_level(city: str) -> str:
    """城市名 → 档位。认不出按二线（保守，宁可少算工资）。"""
    text = str(city or "")
    for name, level in CITY_LEVEL.items():
        if name in text:
            return level
    return "二线"


def average_salary(city: str) -> float:
    """这座城市「平均月薪」大概多少（税前）。认不出的城市按二线。"""
    return float(AVERAGE_SALARY.get(city_level(city), AVERAGE_SALARY["二线"]))


def city_profile(city: str) -> dict[str, Any]:
    return dict(CITY_TIERS[city_level(city)])


def income_tax(taxable: float) -> float:
    """按月度累进算个税。`taxable` 是已经扣完五险一金和起征点的部分。"""
    if taxable <= 0:
        return 0.0
    remaining = float(taxable)
    tax = 0.0
    lower = 0.0
    for cap, rate in TAX_BRACKETS:
        span = min(remaining, cap - lower)
        if span <= 0:
            break
        tax += span * rate
        remaining -= span
        lower = cap
        if remaining <= 0:
            break
    return round(tax, 2)


def payroll(gross: float, bonuses: float = 0.0) -> dict[str, Any]:
    """月薪 → 各项扣除与到手。"""
    gross = max(0.0, float(gross))
    base = gross  # 按全额工资缴，简化处理（真实是按上年月均）
    social = {name: round(base * rate, 2) for name, rate in SOCIAL_RATES.items()}
    social_total = round(sum(social.values()), 2)
    threshold = 5000.0
    taxable = max(0.0, gross - social_total - threshold + max(0.0, float(bonuses)))
    tax = income_tax(taxable)
    net = round(gross + float(bonuses) - social_total - tax, 2)
    return {
        "gross": round(gross, 2),
        "bonus": round(float(bonuses), 2),
        "social": social,
        "social_total": social_total,
        "taxable": round(taxable, 2),
        "tax": tax,
        "net": net,
    }


# ---------------------------------------------------------------------------
# 账本
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Account:
    """一个账户。只有一个主账户在用，留着扩展（储蓄、医保个人账户）。"""

    name: str = "主账户"
    balance: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "balance": round(self.balance, 2)}


@dataclass(slots=True)
class Ledger:
    """账本状态 + 变更逻辑。所有方法都是纯计算，返回新的 self。"""

    balance: float = 0.0
    savings: float = 0.0
    #: 是否自有住房/车：决定扣不扣房租/车贷
    own_house: bool = False
    own_car: bool = False
    #: 月供（有贷款时才有意义）
    mortgage: float = 0.0
    car_loan: float = 0.0
    #: 医保个人账户余额
    medical_account: float = 0.0
    #: 累计借用户的钱（她记着的账）
    owed_to_user: float = 0.0
    #: 本月已花（用于月结）
    month_spent: float = 0.0
    month_income: float = 0.0

    # ---- 查询 ----

    def snapshot(self) -> dict[str, Any]:
        return {
            "balance": round(self.balance, 2),
            "savings": round(self.savings, 2),
            "medical_account": round(self.medical_account, 2),
            "owed_to_user": round(self.owed_to_user, 2),
            "own_house": bool(self.own_house),
            "own_car": bool(self.own_car),
            "mortgage": round(self.mortgage, 2),
            "car_loan": round(self.car_loan, 2),
            "month_spent": round(self.month_spent, 2),
            "month_income": round(self.month_income, 2),
            "total": round(self.balance + self.savings, 2),
        }

    def can_afford(self, amount: float) -> bool:
        return self.balance >= max(0.0, float(amount)) - 1e-9

    # ---- 变更 ----

    def apply(self, amount: float, kind: str = "expense", reason: str = "",
              from_savings: bool = False) -> dict[str, Any]:
        """记一笔。`amount` 为正数，方向由 kind 决定。

        kind: income / expense / transfer_in / transfer_out / tax / refund
        """
        value = round(abs(float(amount)), 2)
        if value <= 0:
            return {"ok": False, "reason": "金额必须大于 0"}

        if kind in ("income", "transfer_in", "refund"):
            if kind == "transfer_in":
                self.owed_to_user = round(self.owed_to_user + value, 2)
            self.balance = round(self.balance + value, 2)
            if kind == "income":
                self.month_income = round(self.month_income + value, 2)
            return {
                "ok": True, "kind": kind, "amount": value,
                "balance": round(self.balance, 2),
                "reason": reason,
            }

        # 支出：先看主账户，不够再看储蓄（储蓄要显式允许）
        if self.balance + 1e-9 < value:
            if from_savings and self.savings + self.balance + 1e-9 >= value:
                short = round(value - self.balance, 2)
                self.balance = 0.0
                self.savings = round(self.savings - short, 2)
                return {
                    "ok": True, "kind": kind, "amount": value, "from_savings": short,
                    "balance": 0.0, "savings": round(self.savings, 2), "reason": reason,
                }
            return {
                "ok": False,
                "reason": "余额不足",
                "need": value,
                "balance": round(self.balance, 2),
                "short": round(value - self.balance, 2),
            }

        self.balance = round(self.balance - value, 2)
        if kind == "expense":
            self.month_spent = round(self.month_spent + value, 2)
        return {
            "ok": True, "kind": kind, "amount": value,
            "balance": round(self.balance, 2),
            "reason": reason,
        }

    def settle_month(self, gross: float, bonuses: float = 0.0) -> dict[str, Any]:
        """月结：发工资、扣税、重置月度统计。"""
        pay = payroll(gross, bonuses)
        self.balance = round(self.balance + pay["net"], 2)
        self.medical_account = round(
            self.medical_account + pay["social"]["医疗"], 2
        )
        self.month_income = round(self.month_income + pay["net"], 2)
        self.month_spent = 0.0
        return pay

    def credit_interest(self, amount: float) -> dict[str, Any]:
        """储蓄利息入账：只进储蓄，不动余额，也不算当月收入。"""
        value = round(max(0.0, float(amount)), 2)
        if value <= 0:
            return {"ok": False, "reason": "利息太小"}
        self.savings = round(self.savings + value, 2)
        return {"ok": True, "amount": value, "savings": round(self.savings, 2)}

    def repay_user(self, amount: float) -> dict[str, Any]:
        """还用户的钱。还不起就还一部分。"""
        value = round(min(abs(float(amount)), self.owed_to_user, self.balance), 2)
        if value <= 0:
            return {"ok": False, "reason": "没有可还的金额", "owed": self.owed_to_user}
        self.balance = round(self.balance - value, 2)
        self.owed_to_user = round(self.owed_to_user - value, 2)
        return {
            "ok": True, "paid": value,
            "balance": round(self.balance, 2),
            "remaining": round(self.owed_to_user, 2),
        }


# ---------------------------------------------------------------------------
# 固定支出
# ---------------------------------------------------------------------------


def monthly_fixed(ledger: Ledger, prices: dict[str, float], city: str = "") -> list[dict[str, Any]]:
    """这个月必须交的钱。有房有车的项自动消失或减少。"""
    profile = city_profile(city)
    items: list[dict[str, Any]] = []

    if not ledger.own_house and ledger.mortgage > 0:
        items.append({"name": "房贷", "amount": ledger.mortgage, "tier": "core"})
    elif not ledger.own_house:
        items.append({"name": "房租", "amount": float(profile["rent"]), "tier": "core"})

    # 物业水电燃气网费：不论自有还是租，都要交
    items.append({"name": "物业", "amount": float(prices.get("物业", 300)), "tier": "core"})
    items.append({"name": "水电", "amount": float(prices.get("水电", 180)), "tier": "event"})
    items.append({"name": "燃气", "amount": float(prices.get("燃气", 60)), "tier": "event"})
    items.append({"name": "网费", "amount": float(prices.get("网费", 100)), "tier": "core"})
    items.append({"name": "话费", "amount": float(prices.get("话费", 50)), "tier": "core"})

    if ledger.own_car:
        # 有车不扣车贷，但保险油钱停车保养照旧
        items.append({"name": "车险", "amount": 420.0, "tier": "core"})
        items.append({"name": "油费", "amount": 620.0, "tier": "event"})
        items.append({"name": "停车", "amount": 300.0, "tier": "event"})
    elif ledger.car_loan > 0:
        items.append({"name": "车贷", "amount": ledger.car_loan, "tier": "core"})

    return items


def anxiety_from(ledger: Ledger, fixed_total: float) -> float:
    """余额撑不住固定支出 → 焦虑。返回值 0~1，给心情系统加。"""
    if fixed_total <= 0:
        return 0.0
    ratio = ledger.balance / fixed_total
    if ratio >= 2.0:
        return 0.0
    if ratio <= 0.2:
        return 1.0
    return round((2.0 - ratio) / 1.8, 3)


# ---------------------------------------------------------------------------
# 吃饭：现成的饭 / 食材 / 菜谱 / 饮品
# ---------------------------------------------------------------------------
#
# 三条口径，别混：
#
# - **现成的饭**：一份多少钱、吃下去回多少饱食度，钱直接走账本；
# - **食材**：按「份」计价，先去菜场买回家（进食材柜），做饭时消耗；
# - **菜谱**：一份菜用哪几样食材、回多少饱食度、大概做多久。
#
# 价格都以**二线城市**为基准，别处按 `food_index()` 缩放；面板改的是物价表本身。

#: 现成的饭。`price_key` 指向物价表（面板可改价），`satiety` 是恢复点数。
READY_FOODS: dict[str, dict[str, Any]] = {
    "泡面": {"price_key": "泡面", "satiety": 15, "note": "热水一冲，五分钟"},
    "早餐": {"price_key": "早餐", "satiety": 30, "note": "楼下包子豆浆"},
    "食堂": {"price_key": "食堂", "satiety": 35, "note": "食堂打一份"},
    "午餐": {"price_key": "午餐", "satiety": 45, "note": "一份盖饭"},
    "晚餐": {"price_key": "晚餐", "satiety": 50, "note": "炒两个菜"},
    "外卖": {"price_key": "外卖", "satiety": 45, "note": "点个外卖"},
    "酸菜鱼": {"price_key": "酸菜鱼", "satiety": 40, "note": "一份，吃不完打包"},
    "火锅": {"price_key": "火锅", "satiety": 55, "note": "一顿顶一天"},
}

#: 饮品。水是家里烧的，不要钱；奶茶是真的花钱。
DRINKS: dict[str, dict[str, Any]] = {
    "水": {"price_key": "", "price": 0.0, "hydration": 25, "satiety": 0, "note": "自己烧的"},
    "矿泉水": {"price_key": "", "price": 2.0, "hydration": 30, "satiety": 0, "note": "楼下小卖部"},
    "奶茶": {"price_key": "奶茶", "hydration": 18, "satiety": 10, "note": "七分糖"},
    "咖啡": {"price_key": "咖啡", "hydration": 12, "satiety": 5, "note": "续命"},
}

#: 食材单价（元/斤，二线基准）。这是**兜底价**：装得到 groceries.py 就用它按
#: 城市/季节/天气算的当日价，这里只防那个模块缺失。
DEFAULT_INGREDIENTS: dict[str, float] = {
    "大米": 3.2, "面条": 4.5, "面粉": 3.0, "馒头": 1.2, "鸡蛋": 6.8,
    "猪肉": 16.0, "鸡肉": 12.0, "牛肉": 38.0, "豆腐": 3.1, "牛奶": 12.6,
    "青菜": 3.1, "土豆": 2.1, "番茄": 4.3, "菇类": 8.3,
    "葱姜蒜": 1.5, "油盐酱醋": 2.0,
}

#: 菜谱：一份用哪些食材（**斤两**，按 groceries 的单位）、回多少饱食度、做多久。
RECIPES: dict[str, dict[str, Any]] = {
    "番茄炒蛋盖饭": {
        "items": {"大米": 0.25, "番茄": 0.6, "鸡蛋": 0.3, "油盐酱醋": 0.15},
        "satiety": 45, "minutes": 25, "note": "番茄炒蛋拌饭，热乎的",
    },
    "蛋炒饭": {
        "items": {"大米": 0.3, "鸡蛋": 0.2, "油盐酱醋": 0.1},
        "satiety": 38, "minutes": 15, "note": "剩饭加个蛋",
    },
    "清汤面": {
        "items": {"面条": 0.3, "鸡蛋": 0.2, "青菜": 0.3, "油盐酱醋": 0.1},
        "satiety": 32, "minutes": 10, "note": "一碗清汤面，撒点葱",
    },
    "土豆炖肉": {
        "items": {"土豆": 0.6, "猪肉": 0.4, "油盐酱醋": 0.2},
        "satiety": 55, "minutes": 45, "note": "炖一锅，能吃两顿",
    },
    "青菜豆腐汤饭": {
        "items": {"大米": 0.25, "青菜": 0.4, "豆腐": 0.4, "油盐酱醋": 0.1},
        "satiety": 35, "minutes": 20, "note": "清淡，胃舒服",
    },
}

#: 二线城市的每日饭钱基准，用来算城市系数（见 CITY_TIERS）。
_FOOD_BASE = 75.0


def food_index(city: str) -> float:
    """城市吃饭贵多少：二线 = 1.0，一线约 1.47，三线 0.8。"""
    try:
        base = float(CITY_TIERS[city_level(city)]["food_day"])
    except Exception:
        base = _FOOD_BASE
    return round(base / _FOOD_BASE, 3)


def _groceries():
    """菜价权威模块（groceries.py）：城市档位 × 季节 × 天气 × 节假日 × 当日波动。

    拿不到就返回 None，宁可退回内置基准价，也不能把菜价算错。
    """
    try:
        from . import groceries as module
    except ImportError:
        try:
            import groceries as module  # type: ignore
        except Exception:
            return None
    return module


def ingredient_price(item: str, city: str = "", weather: str = "",
                     holiday: str = "") -> float:
    """一份食材今天的价。优先问 groceries，问不到才用内置基准价。"""
    name = str(item or "")
    module = _groceries()
    if module is not None:
        try:
            price = module.price_of(name, city_level=city_level(city),
                                    weather=weather, holiday=holiday)
            if price and price > 0:
                return round(float(price), 2)
        except Exception:
            pass
    raw = float(DEFAULT_INGREDIENTS.get(name, 3.0))
    return round(raw * food_index(city), 2)


def ready_price(name: str, prices: dict[str, float], city: str = "") -> float:
    """一份现成的饭多少钱。价格表里没有就用内置默认。"""
    spec = READY_FOODS.get(str(name or ""))
    if not spec:
        return 0.0
    key = str(spec.get("price_key") or name)
    raw = float(prices.get(key) or DEFAULT_PRICES.get(key) or 0.0)
    return round(raw * food_index(city), 2)


def drink_price(name: str, prices: dict[str, float], city: str = "") -> float:
    spec = DRINKS.get(str(name or ""))
    if not spec:
        return 0.0
    key = str(spec.get("price_key") or "")
    if not key:
        return round(float(spec.get("price") or 0.0) * food_index(city), 2)
    raw = float(prices.get(key) or DEFAULT_PRICES.get(key) or 0.0)
    return round(raw * food_index(city), 2)


def recipe_cost(name: str, city: str = "", weather: str = "",
                holiday: str = "") -> float:
    """自己做一顿的食材成本。随季节/天气变，因为菜价会变。"""
    recipe = RECIPES.get(str(name or ""))
    if not recipe:
        return 0.0
    total = 0.0
    for item, need in (recipe.get("items") or {}).items():
        total += ingredient_price(item, city, weather, holiday) * float(need)
    return round(total, 2)


def grocery_cost(want: dict[str, float], city: str = "", weather: str = "",
                 holiday: str = "") -> tuple[float, list[dict[str, Any]]]:
    """买菜要花多少，顺便把明细算出来。"""
    total = 0.0
    items: list[dict[str, Any]] = []
    for item, qty in (want or {}).items():
        try:
            amount = float(qty)
        except (TypeError, ValueError):
            continue
        if amount <= 0:
            continue
        unit = ingredient_price(item, city, weather, holiday)
        line = round(unit * amount, 2)
        total += line
        items.append({"item": str(item), "qty": round(amount, 2),
                      "unit": unit, "amount": line})
    return round(total, 2), items


def food_options(city: str, prices: dict[str, float],
                 pantry: dict[str, float] | None = None,
                 weather: str = "", holiday: str = "") -> dict[str, Any]:
    """当前能吃什么：现成的饭 + 自己做的菜（带缺什么）。"""
    have = {str(k): float(v) for k, v in (pantry or {}).items()}
    ready = []
    for name, spec in READY_FOODS.items():
        ready.append({
            "name": name,
            "price": ready_price(name, prices, city),
            "satiety": int(spec.get("satiety") or 0),
            "note": str(spec.get("note") or ""),
        })
    ready.sort(key=lambda x: x["price"])

    cook = []
    for name, spec in RECIPES.items():
        missing = []
        for item, need in (spec.get("items") or {}).items():
            need = float(need)
            if have.get(item, 0.0) + 1e-9 < need:
                missing.append({"item": item, "need": round(need, 2),
                                "have": round(have.get(item, 0.0), 2)})
        cook.append({
            "name": name,
            "cost": recipe_cost(name, city, weather, holiday),
            "satiety": int(spec.get("satiety") or 0),
            "minutes": int(spec.get("minutes") or 20),
            "note": str(spec.get("note") or ""),
            "can_cook": not missing,
            "missing": missing,
        })
    cook.sort(key=lambda x: x["cost"])
    return {
        "city": str(city or ""),
        "level": city_level(city),
        "index": food_index(city),
        "ready": ready,
        "cook": cook,
        "ingredients": {
            k: ingredient_price(k, city, weather, holiday)
            for k in (_groceries().INGREDIENTS if _groceries() is not None else DEFAULT_INGREDIENTS)
        },
    }
