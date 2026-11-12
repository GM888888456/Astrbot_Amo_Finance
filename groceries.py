# -*- coding: utf-8 -*-
"""菜价与市价：纯计算，不碰网络、不碰 AstrBot。

`DEFAULT_PRICES` 里只有「一顿饭多少钱」，回答不了她真正会遇到的问题：

- 今天青菜多少钱一斤？下雨是不是贵了？
- 自己做饭比点外卖省多少？
- 五块钱能买什么？

这个模块回答这些。三条设计原则：

1. **同一天价格相同。** 随机数种子取日期，同一天里问几次都是同一个价——
   否则她会「早上青菜 4 块、下午 6 块」，像随机数不像市场。
2. **价格只由可解释的因子相乘得出**：城市档位 × 季节 × 天气 × 节假日 × 当日小幅波动。
   每个因子都能单独测。
3. **不编产地、不编品牌。** 只给品类价格，具体买什么由调用方决定。
"""

from __future__ import annotations

import datetime as _dt
import random
from typing import Any

# ---------------------------------------------------------------------------
# 食材基准价：二线城市、常见规格（元）
# ---------------------------------------------------------------------------

#: name -> (单位, 基准价)。基准价是「二线城市平季」的价格，其余靠系数调。
INGREDIENTS: dict[str, tuple[str, float]] = {
    # 主食
    "大米": ("斤", 3.2),
    "面条": ("斤", 4.5),
    "面粉": ("斤", 3.0),
    "馒头": ("个", 1.2),
    # 蛋白
    "鸡蛋": ("斤", 6.8),
    "猪肉": ("斤", 16.0),
    "鸡肉": ("斤", 12.0),
    "牛肉": ("斤", 38.0),
    "草鱼": ("斤", 11.0),
    "鲫鱼": ("斤", 13.0),
    "豆腐": ("块", 3.0),
    "牛奶": ("升", 12.0),
    # 蔬菜
    "青菜": ("斤", 3.5),
    "白菜": ("斤", 1.8),
    "土豆": ("斤", 2.2),
    "番茄": ("斤", 4.5),
    "黄瓜": ("斤", 3.8),
    "青椒": ("斤", 4.2),
    "胡萝卜": ("斤", 2.6),
    "洋葱": ("斤", 2.4),
    "香菇": ("斤", 9.0),
    # 调味（按次摊）
    "葱姜蒜": ("次", 1.5),
    "油盐酱醋": ("次", 2.0),
    # 水果
    "苹果": ("斤", 6.0),
    "香蕉": ("斤", 4.5),
    "当季水果": ("斤", 8.0),
}

#: 叶菜类：受季节和天气影响最大（下雨天没人愿出门，菜贩子加价）。
LEAFY = ("青菜", "白菜", "黄瓜", "番茄", "青椒", "香菇")

#: 城市档位系数（相对二线）。和 ledger.CITY_TIERS 的 food_day 比例一致。
CITY_FACTOR: dict[str, float] = {
    "一线": 1.28,
    "新一线": 1.12,
    "二线": 1.00,
    "三线": 0.88,
}

#: 季节系数：冬季叶菜反季最贵，夏秋本地菜便宜。
SEASON_FACTOR: dict[str, dict[str, float]] = {
    "冬": {"菜": 1.22, "肉": 1.06, "果": 1.15},
    "春": {"菜": 1.05, "肉": 1.00, "果": 1.05},
    "夏": {"菜": 0.90, "肉": 0.98, "果": 0.92},
    "秋": {"菜": 0.95, "肉": 1.00, "果": 1.00},
}

#: 天气系数（只作用于叶菜）。按天气文案里的字眼判断，认不出就不加价。
WEATHER_FACTOR: tuple[tuple[tuple[str, ...], float], ...] = (
    (("暴雨", "大雨", "暴雨", "台风", "雷阵雨"), 1.20),
    (("雨", "阵雨", "小雨", "中雨"), 1.10),
    (("雪", "暴雪", "雨夹雪"), 1.18),
    (("高温", "酷暑", "炎热"), 1.06),
)

#: 节假日系数。春节最狠。
HOLIDAY_FACTOR: tuple[tuple[tuple[str, ...], float], ...] = (
    (("春节", "除夕", "初一", "初二", "元宵"), 1.20),
    (("中秋", "国庆", "端午", "元旦", "劳动节"), 1.12),
)

#: 当日波动上限（±）。同一天恒定，不同天不同。
DAILY_JITTER = 0.06


def _category(name: str) -> str:
    """食材归到「菜 / 肉 / 果」三类，决定了季节系数怎么取。"""
    if name in LEAFY:
        return "菜"
    if name in ("猪肉", "鸡肉", "牛肉", "鸡蛋", "草鱼", "鲫鱼", "豆腐", "牛奶"):
        return "肉"
    if name in ("苹果", "香蕉", "当季水果"):
        return "果"
    return "菜"


def season_of(on: _dt.date) -> str:
    return {12: "冬", 1: "冬", 2: "冬", 3: "春", 4: "春", 5: "春",
            6: "夏", 7: "夏", 8: "夏"}.get(on.month, "秋")


def weather_factor(weather_text: str, name: str) -> float:
    """下雨/下雪只让叶菜贵，肉蛋不受影响。"""
    if name not in LEAFY:
        return 1.0
    text = str(weather_text or "")
    for words, factor in WEATHER_FACTOR:
        if any(word in text for word in words):
            return factor
    return 1.0


def holiday_factor(holiday_text: str) -> float:
    text = str(holiday_text or "")
    for words, factor in HOLIDAY_FACTOR:
        if any(word in text for word in words):
            return factor
    return 1.0


def _jitter(seed_text: str, name: str, span: float = DAILY_JITTER) -> float:
    """按「日期+食材」取一个稳定的波动系数，同一天恒定。"""
    rng = random.Random("%s|%s" % (seed_text, name))
    return 1.0 + rng.uniform(-span, span)


def price_of(name: str, *, city_level: str = "二线", on: _dt.date | None = None,
             weather: str = "", holiday: str = "") -> float:
    """一个食材今天的价格（元）。不认识的食材返回 0。"""
    item = INGREDIENTS.get(str(name or ""))
    if not item:
        return 0.0
    unit, base = item
    if unit == "次":
        return round(base, 2)
    day = on or _dt.date.today()
    factor = CITY_FACTOR.get(city_level, 1.0)
    factor *= SEASON_FACTOR.get(season_of(day), {}).get(_category(name), 1.0)
    factor *= weather_factor(weather, name)
    factor *= holiday_factor(holiday)
    factor *= _jitter(day.isoformat(), name)
    return round(base * factor, 1)


def market_day(*, city_level: str = "二线", on: _dt.date | None = None,
               weather: str = "", holiday: str = "") -> dict[str, Any]:
    """今天整张菜价表 + 涨得最凶 / 最便宜的几个。"""
    day = on or _dt.date.today()
    prices = {
        name: price_of(name, city_level=city_level, on=day, weather=weather, holiday=holiday)
        for name in INGREDIENTS
    }
    changes = []
    for name, value in prices.items():
        base = INGREDIENTS[name][1]
        if base:
            changes.append((name, round((value / base - 1) * 100, 1)))
    changes.sort(key=lambda item: item[1], reverse=True)
    return {
        "date": day.isoformat(),
        "season": season_of(day),
        "city_level": city_level,
        "weather": weather,
        "holiday": holiday,
        "prices": prices,
        "up": [item for item in changes if item[1] > 3][:3],
        "down": [item for item in changes if item[1] < -3][-3:],
    }


# ---------------------------------------------------------------------------
# 家常菜：做了什么、花多少钱
# ---------------------------------------------------------------------------

#: 菜名 -> (食材用量清单, 是否算「一顿正餐」)。
#: 用量用 (食材, 份量) 表示，份量是斤/块/个/次的数量。
DISHES: dict[str, dict[str, Any]] = {
    "番茄炒蛋": {"items": (("番茄", 0.5), ("鸡蛋", 0.4), ("葱姜蒜", 1), ("油盐酱醋", 1)), "meal": True},
    "青菜豆腐": {"items": (("青菜", 0.5), ("豆腐", 1), ("葱姜蒜", 1), ("油盐酱醋", 1)), "meal": True},
    "土豆烧肉": {"items": (("土豆", 0.6), ("猪肉", 0.4), ("葱姜蒜", 1), ("油盐酱醋", 1)), "meal": True},
    "青椒肉丝": {"items": (("青椒", 0.4), ("猪肉", 0.3), ("葱姜蒜", 1), ("油盐酱醋", 1)), "meal": True},
    "红烧鲫鱼": {"items": (("鲫鱼", 0.8), ("葱姜蒜", 1), ("油盐酱醋", 1)), "meal": True},
    "鸡蛋面": {"items": (("面条", 0.3), ("鸡蛋", 0.2), ("青菜", 0.2), ("油盐酱醋", 1)), "meal": True},
    "白粥咸菜": {"items": (("大米", 0.2),), "meal": False},
    "泡面": {"items": (("面条", 0.2),), "meal": False, "instant_price": 5.0},
    "煮饺子": {"items": (("面粉", 0.4), ("猪肉", 0.3), ("白菜", 0.5)), "meal": True},
}

#: 下馆子/外卖相对食材成本的倍数（含人工、房租、平台抽成、包装）。
RESTAURANT_MARKUP = 2.6
TAKEOUT_MARKUP = 2.9


def dish_cost(dish: str, prices: dict[str, float] | None = None) -> dict[str, Any]:
    """做这道菜的食材成本。`prices` 传 market_day()['prices']，不传按基准价。"""
    info = DISHES.get(str(dish or ""))
    if not info:
        return {"dish": dish, "cost": 0.0, "items": [], "known": False}
    if info.get("instant_price"):
        return {"dish": dish, "cost": float(info["instant_price"]),
                "items": [("速食成品", 1)], "known": True}
    prices = prices or {}
    rows = []
    total = 0.0
    for name, amount in info["items"]:
        unit, base = INGREDIENTS.get(name, ("份", 0.0))
        unit_price = float(prices.get(name) or base)
        if unit == "次":
            spend = unit_price * float(amount)
        else:
            spend = unit_price * float(amount)
        total += spend
        rows.append((name, amount, unit, round(spend, 1)))
    return {
        "dish": dish,
        "cost": round(total, 1),
        "items": rows,
        "known": True,
        "meal": bool(info.get("meal", True)),
    }


def eating_out(dish: str, prices: dict[str, float] | None = None,
               takeout: bool = False) -> dict[str, Any]:
    """同一道菜在外面吃多少钱。自己做饭能省多少也一并算出来。"""
    home = dish_cost(dish, prices)
    if not home.get("known"):
        return {"dish": dish, "known": False}
    markup = TAKEOUT_MARKUP if takeout else RESTAURANT_MARKUP
    out = round(home["cost"] * markup + (3.0 if takeout else 0.0), 1)
    return {
        "dish": dish,
        "home": home["cost"],
        "out": out,
        "save": round(out - home["cost"], 1),
        "markup": markup,
        "takeout": takeout,
        "known": True,
    }


def cheapest_dishes(count: int = 3, prices: dict[str, float] | None = None) -> list[dict[str, Any]]:
    """今天最省的正餐。她没钱的时候该看这个。"""
    rows = []
    for dish, info in DISHES.items():
        if not info.get("meal"):
            continue
        cost = dish_cost(dish, prices)
        rows.append({"dish": dish, "cost": cost["cost"]})
    rows.sort(key=lambda item: item["cost"])
    return rows[:count]


#: 一天吃几顿、每顿按什么算。用来把「今天的饭钱」估出来。
DAY_PLAN = ("早餐", "午餐", "晚餐")
DAY_FOOD_RATIO = {"早餐": 0.55, "午餐": 1.0, "晚餐": 1.05}


def day_food_cost(prices: dict[str, float] | None = None, tier_food_day: float = 75.0,
                  cooking: bool = True) -> dict[str, Any]:
    """一天的饭钱：自己做饭 vs 全部外食。

    `tier_food_day` 来自 ledger.CITY_TIERS 的 food_day，作为「外食基准」。
    自己做饭按食材成本 × 顿数，省下的部分一并返回。
    """
    dishes = cheapest_dishes(3, prices)
    if not cooking or not dishes:
        return {"cooking": False, "total": round(tier_food_day, 1), "save": 0.0}
    per_meal = sum(item["cost"] for item in dishes) / max(1, len(dishes))
    total = 0.0
    for meal in DAY_PLAN:
        total += per_meal * DAY_FOOD_RATIO.get(meal, 1.0)
    total = round(total, 1)
    return {
        "cooking": True,
        "total": total,
        "out_total": round(tier_food_day, 1),
        "save": round(max(0.0, tier_food_day - total), 1),
        "dishes": [item["dish"] for item in dishes],
    }


# ---------------------------------------------------------------------------
# 药价：药品目录放这里，因为它也是「市价」的一部分
# ---------------------------------------------------------------------------

#: 非处方药在药店的价格区间（元）。上架价 = 区间中位数 × 渠道系数。
#: otc=True 是自己能买的，False 必须凭处方（门诊开药才给）。
DRUGS: dict[str, dict[str, Any]] = {
    "感冒药": {"name": "感冒灵颗粒", "price": (18, 32), "otc": True, "for": ("cold", "light_discomfort"),
             "dose": "一天三次，一次一袋", "days": 3, "note": "吃了会犯困，别开车"},
    "退烧药": {"name": "布洛芬", "price": (12, 24), "otc": True, "for": ("fever",),
             "dose": "38.5 度以上才吃，间隔 6 小时", "days": 2, "note": "空腹吃伤胃"},
    "退烧贴": {"name": "退热贴", "price": (9, 16), "otc": True, "for": ("fever",),
             "dose": "贴额头，四小时换一次", "days": 2, "note": "物理降温，不治病"},
    "肠胃药": {"name": "蒙脱石散", "price": (16, 28), "otc": True, "for": ("gastro",),
             "dose": "一天三次，饭前空腹", "days": 3, "note": "吃多了会便秘"},
    "藿香正气": {"name": "藿香正气水", "price": (10, 20), "otc": True, "for": ("gastro",),
              "dose": "一次一支，一天两次", "days": 2, "note": "含酒精，不能和头孢一起吃"},
    "胃药": {"name": "奥美拉唑", "price": (20, 36), "otc": True, "for": ("gastro",),
           "dose": "早饭前一片", "days": 5, "note": "长期吃要复查"},
    "消炎药": {"name": "阿莫西林", "price": (24, 42), "otc": False, "for": ("fever", "appendix"),
             "dose": "遵医嘱，必须吃完一个疗程", "days": 5, "note": "处方药，乱吃会耐药"},
    "止痛药": {"name": "布洛芬缓释胶囊", "price": (12, 22), "otc": True, "for": ("fever", "fracture"),
             "dose": "疼的时候吃，一天不超过三次", "days": 3, "note": "只止痛，不治伤"},
    "创可贴": {"name": "创可贴", "price": (4, 9), "otc": True, "for": ("fracture",),
             "dose": "擦干净伤口再贴", "days": 1, "note": ""},
    "红药水": {"name": "碘伏", "price": (6, 12), "otc": True, "for": ("fracture",),
             "dose": "每天消毒一次", "days": 3, "note": ""},
    "云南白药": {"name": "云南白药气雾剂", "price": (26, 45), "otc": True, "for": ("fracture",),
              "dose": "喷患处，一天三次", "days": 7, "note": "破皮的地方别喷"},
    "红糖姜茶": {"name": "红糖姜茶", "price": (6, 12), "otc": True, "for": ("exhaustion",),
              "dose": "趁热喝，一天一杯", "days": 3, "note": "痛经也能喝，不算药"},
    "维生素": {"name": "维生素 C 片", "price": (10, 20), "otc": True, "for": ("exhaustion", "light_discomfort"),
             "dose": "一天一片", "days": 10, "note": ""},
    "生理盐水": {"name": "生理盐水冲洗液", "price": (6, 12), "otc": True, "for": ("light_discomfort",),
              "dose": "冲洗鼻腔或伤口", "days": 1, "note": ""},
}

#: 买药渠道：医保定点药店 / 网上买 / 医院药房。
DRUG_CHANNEL = {
    "药店": 1.0,      # 医保定点，能刷医保但不打折
    "网购": 0.82,     # 便宜两成，但要等两天
    "医院": 1.05,     # 医院药房略贵，好处是当场拿到
}


def drug_price(key: str, channel: str = "药店") -> float:
    """一种药今天的价格。渠道不同折扣不同。"""
    info = DRUGS.get(str(key or ""))
    if not info:
        return 0.0
    low, high = info["price"]
    base = (low + high) / 2.0
    return round(base * DRUG_CHANNEL.get(channel, 1.0), 1)


def drugs_for(illness: str) -> list[dict[str, Any]]:
    """这种病该吃什么药。处方药也在里面，`otc=False` 表示得先看医生。"""
    rows = []
    for key, info in DRUGS.items():
        if illness in info.get("for", ()):
            rows.append({
                "key": key,
                "name": info["name"],
                "price": drug_price(key),
                "otc": info["otc"],
                "dose": info["dose"],
                "days": info["days"],
                "note": info["note"],
            })
    rows.sort(key=lambda item: (not item["otc"], item["price"]))
    return rows


def drug_bill(keys: list[str], channel: str = "药店") -> dict[str, Any]:
    """按药单算钱。"""
    rows = [{"key": key, "name": DRUGS.get(key, {}).get("name", key),
             "price": drug_price(key, channel)} for key in keys if key in DRUGS]
    return {"channel": channel, "items": rows,
            "total": round(sum(row["price"] for row in rows), 1)}
