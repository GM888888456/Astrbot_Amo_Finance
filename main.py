# -*- coding: utf-8 -*-
"""我想永远陪着你-金融（Astrbot_Amo_Finance）。

给「我想永远陪着你-终端」做账本。它只做三件事：

1. **记账**：每一笔收入支出都留一条流水，余额永远等于流水之和；
2. **算钱**：个税、五险一金、月结、固定支出（房租物业水电车险）；
3. **被调用**：终端通过 `charge()` / `income()` 调它，不自己做账。

它**不做**：不调模型、不发消息、不判断剧情。所有「该不该花」的判断在终端那边，
这里只回答「钱够不够」和「扣完还剩多少」。
"""

from __future__ import annotations

import asyncio
import json
import random
import os
import time
from datetime import date, datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:
    from astrbot.api.web import error_response, json_response, request as web_request
except Exception:  # pragma: no cover
    error_response = json_response = web_request = None

try:
    from .ledger import (
        Ledger,
        average_salary,
        anxiety_from,
        city_level,
        city_profile,
        monthly_fixed,
        payroll,
        DEFAULT_PRICES,
    )
except Exception:  # pragma: no cover - 以脚本方式加载时
    from ledger import (  # type: ignore
        Ledger,
        average_salary,
        anxiety_from,
        city_level,
        city_profile,
        monthly_fixed,
        payroll,
        DEFAULT_PRICES,
    )

try:
    from .link import probe as _link_probe, summary as _link_summary
except Exception:  # pragma: no cover
    from link import probe as _link_probe, summary as _link_summary  # type: ignore

try:
    from . import market as market_mod
    from . import portfolio as pf
except Exception:  # pragma: no cover - 直接以脚本方式加载时
    import market as market_mod  # type: ignore
    import portfolio as pf  # type: ignore

PLUGIN_NAME = "Astrbot_Amo_Finance"
PLUGIN_VERSION = "1.7.0"
DISPLAY_NAME = "我想永远陪着你-金融"

try:  # 版本号以 metadata.yaml 为准
    import yaml as _meta_yaml

    with open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "metadata.yaml"),
        encoding="utf-8",
    ) as _fh:
        _meta = _meta_yaml.safe_load(_fh) or {}
    if _meta.get("version"):
        PLUGIN_VERSION = str(_meta["version"])
except Exception:
    pass

FOOTER_GROUP = "1055679275"
HISTORY_LIMIT = 300


def _now() -> float:
    return time.time()


def _today() -> str:
    return date.today().isoformat()


def _month() -> str:
    return datetime.now().strftime("%Y-%m")


class AmoFinancePlugin(Star):
    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self._started_at = _now()
        self._ledger = Ledger()
        # 月结 / 固定支出的「上次执行」标记，防止重启重复扣
        self._state: dict[str, Any] = {}
        self._history: list[dict[str, Any]] = []
        self._rng = random.Random()
        self._load()
        self.ensure_tick_task()
        self._register_web_apis()
        logger.info(
            "[amo_fin] %s %s 已加载：余额 %.2f，流水 %d 条"
            % (DISPLAY_NAME, PLUGIN_VERSION, self._ledger.balance, len(self._history))
        )

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _bool(self, key: str, default: bool) -> bool:
        value = self._cfg(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    def _int(self, key: str, default: int) -> int:
        try:
            return int(float(self._cfg(key, default)))
        except Exception:
            return default

    def _float(self, key: str, default: float) -> float:
        try:
            return float(self._cfg(key, default))
        except Exception:
            return default

    def _str(self, key: str, default: str = "") -> str:
        value = self._cfg(key, default)
        return default if value is None else str(value)

    def _prices(self) -> dict[str, float]:
        """物价表：配置里写成 JSON 字符串，解析失败就用内置价格。"""
        raw = self._cfg("prices", "")
        merged = dict(DEFAULT_PRICES)
        data = None
        if isinstance(raw, dict):
            data = raw
        elif isinstance(raw, str) and raw.strip():
            try:
                data = json.loads(raw)
            except Exception:
                data = None
        if isinstance(data, dict):
            for key, value in data.items():
                try:
                    merged[str(key)] = float(value)
                except Exception:
                    continue
        return merged

    def _city(self) -> str:
        """城市：配置优先；留空时向天气取。"""
        value = self._str("city").strip()
        if value:
            return value
        try:
            companion = self._companion()
            if companion is not None:
                weather = getattr(getattr(companion, "state", None), "weather", None) or {}
                city = str((weather or {}).get("city") or "").strip()
                if city:
                    return city
        except Exception:
            pass
        return ""

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _data_dir(self) -> str:
        base = ""
        for getter in ("get_plugin_data_dir", "plugin_data_dir"):
            try:
                value = getattr(self.context, getter, None)
                value = value() if callable(value) else value
                value = str(value or "").strip()
                if value:
                    base = value
                    break
            except Exception:
                continue
        if not base:
            base = os.path.join(os.getcwd(), "data", "plugin_data")
        path = os.path.join(base, PLUGIN_NAME)
        try:
            os.makedirs(path, exist_ok=True)
        except Exception:
            pass
        return path

    def _book_path(self) -> str:
        return os.path.join(self._data_dir(), "book.json")

    def _load(self) -> None:
        try:
            with open(self._book_path(), encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        led = data.get("ledger") or {}
        self._ledger = Ledger(
            balance=float(led.get("balance") or 0.0),
            savings=float(led.get("savings") or 0.0),
            own_house=bool(led.get("own_house")),
            own_car=bool(led.get("own_car")),
            mortgage=float(led.get("mortgage") or 0.0),
            car_loan=float(led.get("car_loan") or 0.0),
            medical_account=float(led.get("medical_account") or 0.0),
            owed_to_user=float(led.get("owed_to_user") or 0.0),
            month_spent=float(led.get("month_spent") or 0.0),
            month_income=float(led.get("month_income") or 0.0),
        )
        self._state = data.get("state") if isinstance(data.get("state"), dict) else {}
        history = data.get("history")
        self._history = list(history) if isinstance(history, list) else []
        del self._history[:-HISTORY_LIMIT]
        # 首次安装：按配置给一笔起点，只做一次
        if not data and not self._state.get("inited"):
            self._ledger.balance = round(self._float("initial_balance", 8000.0), 2)
            self._ledger.savings = round(self._float("initial_savings", 30000.0), 2)
            self._ledger.own_house = self._bool("own_house", False)
            self._ledger.own_car = self._bool("own_car", False)
            self._ledger.mortgage = self._float("mortgage", 0.0)
            self._ledger.car_loan = self._float("car_loan", 0.0)
            self._state["inited"] = True
            self._log({"kind": "init", "amount": self._ledger.balance,
                       "reason": "初始化", "balance": self._ledger.balance})
            self._save()

    def _save(self) -> None:
        payload = {
            "ledger": {
                "balance": self._ledger.balance,
                "savings": self._ledger.savings,
                "own_house": self._ledger.own_house,
                "own_car": self._ledger.own_car,
                "mortgage": self._ledger.mortgage,
                "car_loan": self._ledger.car_loan,
                "medical_account": self._ledger.medical_account,
                "owed_to_user": self._ledger.owed_to_user,
                "month_spent": self._ledger.month_spent,
                "month_income": self._ledger.month_income,
            },
            "state": self._state,
            "history": self._history[-HISTORY_LIMIT:],
        }
        try:
            tmp = self._book_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._book_path())
        except Exception as exc:
            logger.warning("[amo_fin] 账本写入失败: %s" % exc)

    def _log(self, entry: dict[str, Any]) -> None:
        """流水：只落盘，不打日志。正常交易不该刷屏。"""
        entry.setdefault("ts", _now())
        entry.setdefault("time", datetime.now().strftime("%m-%d %H:%M"))
        self._history.append(entry)
        del self._history[:-HISTORY_LIMIT]

    # ------------------------------------------------------------------
    # 给终端用的公开接口（三个入口，不多）
    # ------------------------------------------------------------------

    def balance(self) -> float:
        return round(self._ledger.balance, 2)

    # ------------------------------------------------------------------
    # 薪资：人格里写了就用人格的，没写按城市平均
    # ------------------------------------------------------------------

    def salary_info(self) -> dict[str, Any]:
        """这个月拿多少、哪来的、到手多少。

        优先级：手动设置 > 人格设定 > 城市平均。人格没写薪资是很常见的事，
        这时候不能凭空编一个 25000，也不能不给钱——按她所在城市的平均发。
        """
        manual = self._float("salary", 0)
        persona = 0.0
        try:
            persona = float(self._state.get("salary_persona") or 0)
        except Exception:
            persona = 0.0
        level = city_level(self._city())
        if manual > 0:
            gross, source = manual, "手动设置"
        elif persona > 0:
            gross, source = persona, "人格设定"
        else:
            gross, source = average_salary(self._city()), "城市平均（%s）" % level
        pay = payroll(gross)
        return {
            "gross": round(gross, 2),
            "net": pay["net"],
            "tax": pay["tax"],
            "social": pay["social_total"],
            "source": source,
            "city_level": level,
            "pay_day": max(1, min(28, self._int("pay_day", 10))),
        }

    def set_salary(self, gross: float, source: str = "persona") -> dict[str, Any]:
        """终端从人格里读到的月薪推过来。

        手动配置优先：用户在面板/配置里填过，就不让人格里的旧数字覆盖它。
        """
        try:
            value = round(float(gross or 0), 2)
        except Exception:
            value = 0.0
        if value <= 0:
            return {"ok": False, "reason": "金额不对"}
        if source == "manual":
            self.config["salary"] = value
            try:
                saver = getattr(self.config, "save_config", None)
                if callable(saver):
                    saver()
            except Exception:
                pass
        elif self._float("salary", 0) > 0:
            return {"ok": True, "skipped": True, "reason": "已手动设置月薪",
                    "salary": self.salary_info()}
        else:
            self._state["salary_persona"] = value
            self._save()
        logger.info("[amo_fin] 月薪已设置：%.0f（%s）" % (value, source))
        return {"ok": True, "salary": self.salary_info()}

    def snapshot(self) -> dict[str, Any]:
        data = self._ledger.snapshot()
        data["city"] = self._city()
        data["city_level"] = city_level(self._city())
        data["fixed"] = monthly_fixed(self._ledger, self._prices(), self._city())
        data["fixed_total"] = round(sum(item["amount"] for item in data["fixed"]), 2)
        data["anxiety"] = anxiety_from(self._ledger, data["fixed_total"])
        data["salary"] = self.salary_info()
        data["borrow"] = self.borrow_info()
        try:
            invest = self.invest_summary()
        except Exception as exc:
            logger.debug("[amo_fin] 投资快照失败: %s" % exc)
            invest = {"positions": [], "count": 0, "total_value": 0,
                      "total_cost": 0, "total_pnl": 0, "total_pnl_pct": 0,
                      "day_pnl": 0, "equity": self._ledger.balance,
                      "cash": self._ledger.balance}
        data["invest"] = invest
        return data

    def recent(self, limit: int = 5) -> list[dict[str, Any]]:
        return list(self._history[-max(1, int(limit)):])

    #: 记住多少次幂等号（重启后同一笔不重复扣）。够覆盖「终端重启再重试」就行。
    CHARGE_ID_KEEP = 300

    def _replay_charge(self, request_id: str) -> dict[str, Any] | None:
        """这个幂等号之前扣成功过吗？扣过就把原流水原样返回，不再动钱。"""
        if not request_id:
            return None
        records = self._state.get("charge_ids")
        if not isinstance(records, dict):
            return None
        entry = records.get(request_id)
        if not isinstance(entry, dict):
            return None
        result = dict(entry.get("result") or {})
        if not result.get("ok"):
            return None
        result["repeated"] = True
        return result

    def _remember_charge(self, request_id: str, result: dict[str, Any]) -> None:
        """把成功的扣款记进账本状态（跟 book.json 一起落盘）。"""
        if not request_id or not result.get("ok"):
            return
        records = self._state.get("charge_ids")
        if not isinstance(records, dict):
            records = {}
            self._state["charge_ids"] = records
        records[request_id] = {"ts": _now(), "result": dict(result)}
        if len(records) > self.CHARGE_ID_KEEP:
            oldest = sorted(records, key=lambda k: float((records.get(k) or {}).get("ts") or 0))
            for key in oldest[: max(1, len(records) - self.CHARGE_ID_KEEP)]:
                records.pop(key, None)

    def charge(self, amount: float, reason: str = "", payee: str = "",
               from_savings: bool = False, request_id: str = "") -> dict[str, Any]:
        """花钱。返回 {ok, balance, short?...}。失败不抛异常。

        ``request_id`` 幂等号：同一笔扣过就不扣第二次（终端重启也认）。
        只记成功——失败单不锁死，补钱后还能重试。
        """
        rid = str(request_id or "").strip()
        replay = self._replay_charge(rid)
        if replay is not None:
            return replay
        result = self._ledger.apply(
            amount, "expense", reason=reason, from_savings=from_savings
        )
        if result.get("ok"):
            self._log({
                "kind": "expense", "amount": round(float(amount), 2),
                "reason": reason, "payee": payee,
                "balance": result.get("balance"),
                "request_id": rid,
            })
            self._remember_charge(rid, result)
            self._save()
        else:
            # 扣款失败是要让人看见的
            logger.warning(
                "[amo_fin] 扣款失败：%s 需 %.2f，余额 %.2f，差 %.2f"
                % (reason or "?", float(amount), self._ledger.balance,
                   float(result.get("short") or 0))
            )
            self._log({
                "kind": "expense_failed", "amount": round(float(amount), 2),
                "reason": reason, "payee": payee,
                "short": result.get("short"), "balance": self._ledger.balance,
            })
            self._save()
        return result

    def income(self, amount: float, reason: str = "", source: str = "") -> dict[str, Any]:
        result = self._ledger.apply(amount, "income", reason=reason)
        if result.get("ok"):
            self._log({
                "kind": "income", "amount": round(float(amount), 2),
                "reason": reason, "source": source,
                "balance": result.get("balance"),
            })
            self._save()
        return result

    def transfer_in(self, amount: float, from_user: str = "") -> dict[str, Any]:
        """用户转来的钱：记成「欠着」，不算收入，不计税。"""
        result = self._ledger.apply(amount, "transfer_in", reason="用户转账")
        if result.get("ok"):
            self._log({
                "kind": "transfer_in", "amount": round(float(amount), 2),
                "from": from_user, "balance": result.get("balance"),
            })
            self._save()
        return result

    #: 借钱的两条通道：转账按天封顶，红包单个封顶、累计封顶
    BORROW_TRANSFER_DAILY = 10000.0
    BORROW_RED_MAX = 200.0
    BORROW_RED_TOTAL = 10000.0

    def _borrow_day(self) -> dict[str, Any]:
        today = date.today().isoformat()
        row = self._state.get("borrow_day")
        if not isinstance(row, dict) or row.get("date") != today:
            row = {"date": today, "transfer": 0.0}
            self._state["borrow_day"] = row
        return row

    def borrow_info(self) -> dict[str, Any]:
        """她还能借多少：转账按天，红包按次与累计。"""
        day = self._borrow_day()
        used = float(day.get("transfer") or 0)
        red_used = float(self._state.get("borrow_red_total") or 0)
        return {
            "owed": round(self._ledger.owed_to_user, 2),
            "transfer_today": round(used, 2),
            "transfer_left": round(max(0.0, self.BORROW_TRANSFER_DAILY - used), 2),
            "red_total": round(red_used, 2),
            "red_left": round(max(0.0, self.BORROW_RED_TOTAL - red_used), 2),
            "red_max_once": self.BORROW_RED_MAX,
        }

    def borrow(self, amount: float, kind: str = "transfer", from_user: str = "") -> dict[str, Any]:
        """她开口借的钱：算欠款，不算收入。

        两条通道各自有上限——不设上限的话，一次「借」就能把账本刷爆，
        而且她本来就该为钱发愁，不是想借多少有多少。
        """
        try:
            value = round(float(amount or 0), 2)
        except (TypeError, ValueError):
            return {"ok": False, "reason": "金额不对"}
        if value <= 0:
            return {"ok": False, "reason": "金额不对"}
        channel = "red" if str(kind or "").lower() in ("red", "redpacket", "红包") else "transfer"
        if channel == "red":
            if value > self.BORROW_RED_MAX:
                return {"ok": False, "reason": "单个红包最多 %.0f" % self.BORROW_RED_MAX}
            used = float(self._state.get("borrow_red_total") or 0)
            if used + value > self.BORROW_RED_TOTAL:
                return {"ok": False, "reason": "红包累计到上限了",
                        "left": round(max(0.0, self.BORROW_RED_TOTAL - used), 2)}
        else:
            day = self._borrow_day()
            used = float(day.get("transfer") or 0)
            if used + value > self.BORROW_TRANSFER_DAILY:
                return {"ok": False, "reason": "今天转账到上限了",
                        "left": round(max(0.0, self.BORROW_TRANSFER_DAILY - used), 2)}
        result = self._ledger.apply(value, "transfer_in", reason="借款")
        if not result.get("ok"):
            return result
        if channel == "red":
            self._state["borrow_red_total"] = round(
                float(self._state.get("borrow_red_total") or 0) + value, 2)
        else:
            day = self._borrow_day()
            day["transfer"] = round(float(day.get("transfer") or 0) + value, 2)
        self._log({
            "kind": "borrow", "channel": channel, "amount": value,
            "from": from_user, "owed": round(self._ledger.owed_to_user, 2),
            "balance": result.get("balance"),
        })
        self._save()
        logger.info("[amo_fin] 她借了 %.2f（%s），共欠 %.2f"
                    % (value, "红包" if channel == "red" else "转账", self._ledger.owed_to_user))
        return {"ok": True, "channel": channel, "amount": value,
                "balance": round(self._ledger.balance, 2),
                "owed": round(self._ledger.owed_to_user, 2),
                "left": self.borrow_info()}

    def repay(self, amount: float) -> dict[str, Any]:
        result = self._ledger.repay_user(amount)
        if result.get("ok"):
            self._log({"kind": "repay", "amount": result["paid"],
                       "balance": result.get("balance")})
            self._save()
        return result

    # ------------------------------------------------------------------
    # 定时：发薪、固定支出
    # ------------------------------------------------------------------

    TICK_INTERVAL = 300  # 每 5 分钟看一眼：该发薪/该交固定支出没有

    def ensure_tick_task(self) -> None:
        """起定时器。

        以前 ``tick()`` 写好了但没有任何地方调用——工资永远发不出来，
        固定支出也永远不扣。这里让金融插件自己跑，不依赖终端是否在跑。
        """
        task = getattr(self, "_tick_task", None)
        if task is not None and not task.done():
            return
        try:
            self._tick_task = asyncio.create_task(self._tick_loop())
        except Exception as exc:  # 没有事件循环（单测/导入期）就静默跳过
            logger.debug("[amo_fin] 定时器启动失败: %s" % exc)

    async def _tick_loop(self) -> None:
        # 先等 20 秒：终端在 on_astrbot_loaded 里推人格月薪，别抢在它前面按城市平均发一遍
        await asyncio.sleep(20)
        try:
            self._seed_invest_once()
        except Exception as exc:
            logger.warning("[amo_fin] 首次建仓失败: %s" % exc)
        while True:
            try:
                self.tick()
            except Exception as exc:
                logger.warning("[amo_fin] 月度结算失败: %s" % exc)
            await asyncio.sleep(self.TICK_INTERVAL)

    # ------------------------------------------------------------------
    # 投资：模拟普通人炒股
    # ------------------------------------------------------------------

    def _fx(self) -> dict[str, float]:
        return {
            "CNY": 1.0,
            "HKD": self._float("fx_hkd", 0.92),
            "USD": self._float("fx_usd", 7.15),
        }

    def _watchlist(self) -> list[str]:
        raw = self._cfg("invest_watchlist", list(pf.DEFAULT_WATCHLIST))
        if isinstance(raw, str):
            raw = [item for item in raw.replace("，", ",").split(",") if item.strip()]
        out = []
        for item in raw or []:
            code = market_mod.normalize(str(item))
            if code:
                out.append(code)
        return out or list(pf.DEFAULT_WATCHLIST)

    def _board(self):
        board = getattr(self, "_quote_board", None)
        if board is None:
            board = market_mod.QuoteBoard(ttl=float(self._int("invest_quote_ttl", 60)))
            self._quote_board = board
        return board

    def _positions(self) -> dict[str, dict[str, Any]]:
        invest = self._state.get("invest")
        if not isinstance(invest, dict):
            invest = {}
            self._state["invest"] = invest
        positions = invest.get("positions")
        if not isinstance(positions, dict):
            positions = {}
            invest["positions"] = positions
        return positions

    def _invest_refresh(self, force: bool = False) -> None:
        """刷新持仓的最新价；拿不到行情的股票保留上一次价格，不编。"""
        positions = self._positions()
        if not positions:
            return
        quotes = self._board().quotes(list(positions), force=force)
        changed = False
        for code, pos in positions.items():
            data = quotes.get(code)
            if not data:
                continue
            pos["name"] = data.get("name") or pos.get("name") or code
            pos["last"] = data.get("price")
            pos["prev_close"] = data.get("prev_close")
            changed = True
        if changed:
            self._save()

    def invest_summary(self, force: bool = False) -> dict[str, Any]:
        self._invest_refresh(force=force)
        summary = pf.summarize(self._positions(), self._fx())
        equity = round(self._ledger.balance + summary["total_value"], 2)
        summary["equity"] = equity
        summary["cash"] = round(self._ledger.balance, 2)
        try:
            summary["stance"] = (self.market_stance() or {}).get("stance") or ""
        except Exception:
            summary["stance"] = ""
        summary["leek"] = dict(self._state.get("invest_leek") or {})
        return summary

    def invest_buy(self, symbol: str, qty: int = 0, budget: float = 0.0) -> dict[str, Any]:
        """买入。先看行情、再看钱、再落账——行情拿不到就明确失败。"""
        code = market_mod.normalize(symbol)
        if not code:
            return {"ok": False, "reason": "认不出这只股票"}
        quotes = self._board().quotes([code])
        data = quotes.get(code)
        if not data:
            return {"ok": False, "reason": "行情拿不到，先不买"}
        price = float(data["price"])
        market = market_mod.market_of(code)
        if budget and not qty:
            qty = pf.qty_from_budget(float(budget), price, market)
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            qty = 0
        check = pf.can_buy(qty, price, self._ledger.balance, market)
        if not check.get("ok"):
            return check
        # 单只不超过总资产三成：不许一把梭
        summary = self.invest_summary()
        equity = max(summary["equity"], 1.0)
        holding = self._positions().get(code) or {}
        rate = float(self._fx().get(str(holding.get("currency") or "CNY"), 1.0) or 1.0)
        held_value = float(holding.get("qty") or 0) * float(holding.get("last") or price) * rate
        after = held_value + check["cost"]
        if after / equity > pf.MAX_WEIGHT + 0.001:
            return {"ok": False, "reason": "这只占得太多了，别一把梭",
                    "limit": round(equity * pf.MAX_WEIGHT, 2)}
        result = self._ledger.apply(
            check["cost"], "expense",
            reason="买入 %s %d 股" % (data.get("name") or code, qty),
        )
        if not result.get("ok"):
            return result
        positions = self._positions()
        pos = positions.get(code)
        if pos is None:
            pos = {"symbol": code, "name": data.get("name") or code,
                   "qty": 0, "cost": 0.0, "last": price,
                   "prev_close": data.get("prev_close"),
                   "currency": data.get("currency") or "CNY"}
            positions[code] = pos
        pos["qty"] = int(pos.get("qty") or 0) + qty
        pos["cost"] = round(float(pos.get("cost") or 0) + check["cost"], 2)
        pos["last"] = price
        pos["prev_close"] = data.get("prev_close")
        self._state.setdefault("invest_history", []).append({
            "action": "buy", "symbol": code, "name": data.get("name"),
            "qty": qty, "price": price, "amount": check["cost"], "ts": _now(),
        })
        del self._state["invest_history"][:-200]
        self._save()
        self._report_to_companion()
        return {"ok": True, "symbol": code, "name": data.get("name"), "qty": qty,
                "price": price, "cost": check["cost"],
                "balance": round(self._ledger.balance, 2)}

    def invest_sell(self, symbol: str, qty: int = 0, all_out: bool = False) -> dict[str, Any]:
        code = market_mod.normalize(symbol)
        positions = self._positions()
        pos = positions.get(code) if code else None
        if not pos:
            return {"ok": False, "reason": "没有这只"}
        quotes = self._board().quotes([code])
        data = quotes.get(code)
        if not data:
            return {"ok": False, "reason": "行情拿不到，先不卖"}
        price = float(data["price"])
        market = market_mod.market_of(code)
        holding = int(pos.get("qty") or 0)
        if all_out or not qty:
            qty = holding
        check = pf.can_sell(int(qty or 0), holding, price, market)
        if not check.get("ok"):
            return check
        result = self._ledger.apply(
            check["proceeds"], "income",
            reason="卖出 %s %d 股" % (pos.get("name") or code, int(qty)),
        )
        if not result.get("ok"):
            return result
        qty = int(qty)
        avg_cost = float(pos.get("cost") or 0) / holding if holding else 0.0
        pos["qty"] = holding - qty
        pos["cost"] = round(max(0.0, float(pos.get("cost") or 0) - avg_cost * qty), 2)
        pos["last"] = price
        pos["prev_close"] = data.get("prev_close")
        if pos["qty"] <= 0:
            positions.pop(code, None)
        self._state.setdefault("invest_history", []).append({
            "action": "sell", "symbol": code, "name": pos.get("name"),
            "qty": qty, "price": price, "amount": check["proceeds"], "ts": _now(),
        })
        del self._state["invest_history"][:-200]
        self._save()
        self._report_to_companion()
        return {"ok": True, "symbol": code, "name": pos.get("name"), "qty": qty,
                "price": price, "proceeds": check["proceeds"],
                "balance": round(self._ledger.balance, 2)}

    def _seed_invest_once(self) -> None:
        """第一次装好：先买 100 股，让「我有股票」这件事真实发生。

        用户不用替她买；她自己开户、自己下单。只买一手（100 股），且不超过总资产三成，
        买不起就换下一只；都买不起或行情拿不到就下次再试。
        """
        if self._state.get("invest_seeded"):
            return
        if self._positions():
            self._state["invest_seeded"] = True
            self._save()
            return
        for code in self._watchlist():
            quotes = self._board().quotes([code])
            data = quotes.get(code)
            if not data:
                continue
            price = float(data.get("price") or 0)
            market = market_mod.market_of(code)
            check = pf.can_buy(100, price, self._ledger.balance, market)
            if not check.get("ok"):
                continue
            if check["cost"] > self._ledger.balance * pf.MAX_WEIGHT:
                continue          # 一手就超过三成：这只太贵，换一只
            result = self.invest_buy(code, qty=100)
            if result.get("ok"):
                self._state["invest_seeded"] = True
                self._state["invest_seed_info"] = {
                    "symbol": result.get("symbol"), "name": result.get("name"),
                    "qty": result.get("qty"), "price": result.get("price"),
                    "cost": result.get("cost"), "ts": _now(),
                }
                self._save()
                logger.info("[amo_fin] 第一次装好，买了 100 股 %s（¥%.2f）"
                            % (result.get("name"), float(result.get("cost") or 0)))
                return
        logger.info("[amo_fin] 这次没买成（行情或余额不够），下次再试")

    #: 韭菜档位阈值：涨/跌超过这个百分比就进入相应的情绪档
    LEEK_UP_PCT = 3.0
    LEEK_DOWN_PCT = -3.0

    def _today(self) -> str:
        return date.today().isoformat()

    def market_stance(self) -> dict[str, Any]:
        """今天她是什么「韭菜状态」：过度自信 / 死扛 / 正常。

        一旦当天触发过就锁住：上午跌 4% 死扛，下午涨回来也还是死扛——
        情绪不是实时跟随，这才像人。
        """
        today = self._today()
        record = self._state.get("invest_stance")
        if isinstance(record, dict) and record.get("date") == today:
            return record
        signal = self.mood_signal()
        pct = float(signal.get("day_pnl_pct") or 0)
        stance = ""
        if pct >= self.LEEK_UP_PCT:
            stance = "overconfident"
        elif pct <= self.LEEK_DOWN_PCT:
            stance = "hodl"
        record = {"date": today, "stance": stance, "pct": pct}
        self._state["invest_stance"] = record
        self._save()
        return record

    def _leek_add_position(self) -> None:
        """上头的日子里，她自己加仓——没有审批、没有按钮。

        守死三成上限；卡里没钱就记一笔「想买买不了」，终端据此让她撒娇要钱。
        """
        today = self._today()
        row = self._state.get("invest_leek")
        if isinstance(row, dict) and row.get("date") == today:
            return
        positions = self._positions()
        candidates = []
        if positions:
            candidates.append(max(positions, key=lambda code: int((positions[code] or {}).get("qty") or 0)))
        for code in self._watchlist():
            if code not in candidates:
                candidates.append(code)
        summary = self.invest_summary()
        equity = max(summary["equity"], 1.0)
        record = {"date": today, "kind": "", "reason": "", "ts": _now()}
        for symbol in candidates:
            data = self._board().quotes([symbol]).get(symbol)
            if not data:
                continue
            price = float(data["price"])
            market = market_mod.market_of(symbol)
            held = float((positions.get(symbol) or {}).get("qty") or 0) * price
            room = max(0.0, equity * pf.MAX_WEIGHT - held)
            if room <= 0:
                continue
            budget = min(self._ledger.balance * 0.9, room * 0.95)
            qty = pf.qty_from_budget(budget, price, market)
            if qty <= 0:
                continue
            result = self.invest_buy(symbol, qty=qty)
            record["symbol"] = symbol
            record["name"] = data.get("name")
            record["qty"] = qty
            if result.get("ok"):
                record["kind"] = "bought"
            else:
                record["kind"] = "failed"
                record["reason"] = result.get("reason") or ""
            self._state["invest_leek"] = record
            self._save()
            logger.info("[amo_fin] 上头加仓：%s %s 股 → %s"
                        % (record.get("name"), qty, record["kind"]))
            return
        # 一只都加不了：仓位到顶 or 没钱
        record["kind"] = "no_room" if self._ledger.balance > 1000 else "no_money"
        record["reason"] = "仓位到顶" if record["kind"] == "no_room" else "卡里没钱了"
        self._state["invest_leek"] = record
        self._save()
        logger.info("[amo_fin] 想加仓但加不了：%s" % record["reason"])

    def _invest_tick(self) -> None:
        """收盘后（或插件定时器里）做两件事：刷新行情、按规则决定今天动不动手。"""
        if not self._bool("invest_enable", True):
            return
        self._invest_refresh()
        today = date.today().isoformat()
        day = self._state.get("invest_day")
        if not isinstance(day, dict) or day.get("date") != today:
            # 记今天的开盘权益，用来算「今天赚了还是亏了」
            equity = self._ledger.balance + pf.summarize(self._positions(), self._fx())["total_value"]
            self._state["invest_day"] = {"date": today, "open": round(equity, 2)}
            self._save()
        stance = str((self.market_stance() or {}).get("stance") or "")
        if stance == "overconfident":
            # 上头了：她自己加仓（守三成上限），没人替她点按钮
            self._leek_add_position()

        if self._state.get("invest_auto_date") == today:
            return
        if not self._bool("invest_auto_trade", True):
            return
        self._state["invest_auto_date"] = today
        self._save()
        if stance == "overconfident":
            actions = []
        else:
            actions = pf.suggest_actions(
                self._positions(), self._ledger.balance, self._watchlist(), self._fx(), self._rng,
            )
            if stance == "hodl":
                # 死扛：赚了也不卖、亏了更不卖（这次连止盈都不执行）
                actions = [a for a in actions if a.get("action") != "sell"]
        for item in actions[:2]:
            if item.get("action") == "sell":
                result = self.invest_sell(item["symbol"], item.get("qty") or 0)
            else:
                result = self.invest_buy(item["symbol"], budget=item.get("budget") or 0)
            if result.get("ok"):
                logger.info("[amo_fin] 模拟交易：%s %s（%s）"
                            % (item.get("action"), item.get("symbol"), item.get("why") or ""))

    def mood_signal(self) -> dict[str, Any]:
        """今天的盈亏，给终端拿去影响心情。"""
        summary = self.invest_summary()
        day = self._state.get("invest_day") or {}
        base = float(day.get("open") or 0) if isinstance(day, dict) else 0.0
        equity = summary["equity"]
        day_pnl = round(equity - base, 2) if base else summary["day_pnl"]
        pct = round(day_pnl / base * 100.0, 2) if base else 0.0
        return {
            "day_pnl": day_pnl,
            "day_pnl_pct": pct,
            "total_pnl": summary["total_pnl"],
            "total_pnl_pct": summary["total_pnl_pct"],
            "equity": equity,
            "cash": summary["cash"],
            "positions": summary["count"],
        }

    def tick(self, now: datetime | None = None) -> dict[str, Any]:
        """检查该不该发工资 / 该不该交固定支出。幂等，重启不会重复扣。"""
        now = now or datetime.now()
        today = now.date().isoformat()
        month = now.strftime("%Y-%m")
        actions: list[str] = []

        # 每月 10 号发工资
        pay_day = max(1, min(28, self._int("pay_day", 10)))
        if now.day >= pay_day and self._state.get("last_pay") != month:
            gross = self.salary_info()["gross"]
            bonus = 0.0
            if self._bool("bonus_enable", True) and self._state.get("last_pay") is None:
                bonus = round(gross * 0.1, 2)  # 首月给一笔，之后按月随机
            pay = self._ledger.settle_month(gross, bonus)
            self._state["last_pay"] = month
            self._log({
                "kind": "payroll", "amount": pay["net"], "gross": pay["gross"],
                "bonus": pay["bonus"], "tax": pay["tax"],
                "social": pay["social_total"], "balance": self._ledger.balance,
                "reason": "%s 月工资" % month,
            })
            actions.append("发薪")
            self._save()

        # 每月 1 号交固定支出
        if (self._bool("auto_fixed_expense", True)
                and now.day == 1 and self._state.get("last_fixed") != month):
            items = monthly_fixed(self._ledger, self._prices(), self._city())
            paid, failed = [], []
            for item in items:
                result = self._ledger.apply(item["amount"], "expense",
                                            reason=item["name"])
                if result.get("ok"):
                    paid.append(item["name"])
                    self._log({
                        "kind": "fixed", "amount": item["amount"],
                        "reason": item["name"], "balance": result.get("balance"),
                    })
                else:
                    failed.append(item["name"])
                    logger.warning("[amo_fin] %s 未缴：余额不足" % item["name"])
            self._state["last_fixed"] = month
            self._state["last_fixed_failed"] = failed
            actions.append("固定支出")
            self._save()

        try:
            self._invest_tick()
        except Exception as exc:
            logger.warning("[amo_fin] 投资结算失败: %s" % exc)

        return {"actions": actions, "today": today, "month": month}

    # ------------------------------------------------------------------
    # 和终端联动
    # ------------------------------------------------------------------

    COMPANION_NAMES = ("Astrbot_Amo_forever", "astrbot_plugin_amo_forever", "amo_forever")

    def _companion(self):
        for name in self.COMPANION_NAMES:
            try:
                meta = self.context.get_registered_star(name)
            except Exception:
                meta = None
            star = self._instance(meta)
            if star is not None:
                return star
        try:
            for meta in self.context.get_all_stars():
                marker = ((getattr(meta, "name", "") or "") + " "
                          + (getattr(meta, "module_path", "") or "")).lower()
                if "amo_forever" not in marker:
                    continue
                star = self._instance(meta)
                if star is not None:
                    return star
        except Exception:
            pass
        return None

    @staticmethod
    def _instance(meta):
        if meta is None:
            return None
        for attr in ("star_cls", "instance", "star", "plugin"):
            value = getattr(meta, attr, None)
            if value is not None:
                return value
        return None

    def _report_to_companion(self) -> None:
        """把最新账本推给终端的状态里，让她说话时心里有数。"""
        companion = self._companion()
        if companion is None:
            return
        try:
            state = getattr(companion, "state", None)
            if state is None:
                return
            snap = self.snapshot()
            state.finance = {
                "balance": snap["balance"],
                "fixed_total": snap["fixed_total"],
                "anxiety": snap["anxiety"],
                "owed": snap["owed_to_user"],
                "month_spent": snap["month_spent"],
            }
        except Exception as exc:
            logger.debug("[amo_fin] 上报终端失败: %s" % exc)

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------

    @filter.command("money", alias={"钱包", "余额"})
    async def cmd_money(self, event: AstrMessageEvent):
        """/money：看她还有多少钱。"""
        snap = self.snapshot()
        fixed = "、".join("%s %d" % (i["name"], i["amount"]) for i in snap["fixed"][:4])
        yield event.plain_result(
            "余额 %.2f｜储蓄 %.2f｜本月已花 %.2f\n固定支出 %.2f（%s）"
            % (snap["balance"], snap["savings"], snap["month_spent"],
               snap["fixed_total"], fixed or "无")
        )

    @filter.command("bill", alias={"账单"})
    async def cmd_bill(self, event: AstrMessageEvent):
        """/bill：最近 8 笔流水。"""
        items = self.recent(8)
        if not items:
            yield event.plain_result("还没有账。")
            return
        lines = []
        for item in reversed(items):
            mark = "-" if item.get("kind") in ("expense", "fixed", "repay") else "+"
            lines.append("%s %s%.2f %s" % (
                item.get("time", ""), mark, float(item.get("amount") or 0),
                item.get("reason", "")))
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("setmoney")
    async def cmd_setmoney(self, event: AstrMessageEvent, *args):
        """/setmoney <金额>：直接改余额（管理员）。"""
        if not args:
            yield event.plain_result("用法：/setmoney 5000")
            return
        try:
            value = float(args[0])
        except Exception:
            yield event.plain_result("金额要填数字。")
            return
        delta = round(value - self._ledger.balance, 2)
        self._ledger.balance = round(value, 2)
        self._log({"kind": "adjust", "amount": delta, "reason": "管理员调整",
                   "balance": self._ledger.balance})
        self._save()
        self._report_to_companion()
        yield event.plain_result("余额已设为 %.2f" % self._ledger.balance)

    # ------------------------------------------------------------------
    # 面板
    # ------------------------------------------------------------------

    @staticmethod
    def _json(payload):
        if json_response is not None:
            try:
                return json_response(payload)
            except Exception:
                pass
        return payload

    @staticmethod
    def _fail(message):
        if error_response is not None:
            try:
                return error_response(message)
            except Exception:
                pass
        return {"code": 1, "message": message}

    def _register_web_apis(self) -> None:
        routes = [
            ("/status", self.api_status, ["GET"]),
            ("/health", self.api_health, ["GET"]),
            ("/salary", self.api_salary, ["POST"]),
            ("/invest", self.api_invest, ["GET", "POST"]),
            ("/borrow", self.api_borrow, ["POST"]),
            ("/history", self.api_history, ["GET"]),
            ("/assets", self.api_assets, ["POST"]),
            ("/charge", self.api_charge, ["POST"]),
            ("/income", self.api_income, ["POST"]),
        ]
        for path, handler, methods in routes:
            try:
                self.context.register_web_api("/" + PLUGIN_NAME + path, handler, methods, path)
            except TypeError:
                try:
                    self.context.register_web_api("/" + PLUGIN_NAME + path, methods, path, handler)
                except Exception as exc:
                    logger.warning("[amo_fin] 接口 %s 注册失败: %s" % (path, exc))
            except Exception as exc:
                logger.warning("[amo_fin] 接口 %s 注册失败: %s" % (path, exc))

    @staticmethod
    async def _payload() -> dict[str, Any]:
        if web_request is None:
            return {}
        try:
            raw = await web_request.json(default=None)
        except Exception:
            raw = None
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = None
        return raw if isinstance(raw, dict) else {}

    async def api_health(self):
        """我自己连没连上终端/记忆库。面板顶部那一行就看这个。"""
        return self._json({"code": 0, "data": _link_probe(self)})

    async def api_status(self):
        self.ensure_tick_task()
        snap = self.snapshot()
        return self._json({"code": 0, "data": {
            "name": PLUGIN_NAME, "version": PLUGIN_VERSION, "display_name": DISPLAY_NAME,
            "link": _link_probe(self),
            "city": snap["city"], "city_level": snap["city_level"],
            "balance": snap["balance"], "savings": snap["savings"],
            "owed_to_user": snap["owed_to_user"],
            "month_spent": snap["month_spent"], "month_income": snap["month_income"],
            "fixed": snap["fixed"], "fixed_total": snap["fixed_total"],
            "anxiety": snap["anxiety"], "own_house": snap["own_house"],
            "own_car": snap["own_car"],
            "salary": snap.get("salary") or self.salary_info(),
            "invest": snap.get("invest") or self.invest_summary(),
            "borrow": snap.get("borrow") or self.borrow_info(),
            "uptime": int(_now() - self._started_at),
        }})

    async def api_salary(self):
        """读/改月薪。改了就按手动设置算，不再跟人格或城市平均。"""
        data = await self._payload()
        if "salary" in data:
            try:
                gross = float(data.get("salary") or 0)
            except (TypeError, ValueError):
                return self._json({"code": 1, "message": "月薪要是数字"})
            result = self.set_salary(gross, source="manual")
            if not result.get("ok"):
                return self._json({"code": 1, "message": result.get("reason") or "设置失败"})
        return self._json({"code": 0, "data": self.salary_info()})

    async def api_invest(self):
        """持仓与盈亏。带 ``refresh=1`` 会强制拉一次行情。"""
        data = await self._payload()
        force = bool(data.get("refresh"))
        return self._json({"code": 0, "data": self.invest_summary(force=force)})

    async def api_history(self):
        return self._json({"code": 0, "data": {"items": self.recent(100)}})

    async def api_assets(self):
        """资产登记：有没有房、有没有车、有没有贷款。"""
        data = await self._payload()
        if "own_house" in data:
            self._ledger.own_house = bool(data["own_house"])
        if "own_car" in data:
            self._ledger.own_car = bool(data["own_car"])
        for key, attr in (("mortgage", "mortgage"), ("car_loan", "car_loan"),
                          ("savings", "savings")):
            if key in data:
                try:
                    setattr(self._ledger, attr, round(float(data[key]), 2))
                except Exception:
                    continue
        self._save()
        self._report_to_companion()
        return self._json({"code": 0, "data": self.snapshot()})

    async def api_charge(self):
        data = await self._payload()
        try:
            amount = float(data.get("amount") or 0)
        except Exception:
            return self._fail("金额格式不对")
        result = self.charge(
            amount,
            reason=str(data.get("reason") or ""),
            payee=str(data.get("payee") or ""),
            from_savings=bool(data.get("from_savings")),
            request_id=str(data.get("request_id") or ""),
        )
        self._report_to_companion()
        return self._json({"code": 0, "data": result})

    async def api_borrow(self):
        """她开口要钱：{amount, kind: transfer|red}。算欠款，不算收入。"""
        data = await self._payload()
        try:
            amount = float(data.get("amount") or 0)
        except (TypeError, ValueError):
            return self._fail("金额格式不对")
        result = self.borrow(amount, kind=str(data.get("kind") or "transfer"),
                             from_user=str(data.get("from_user") or ""))
        if not result.get("ok"):
            return self._json({"code": 1, "message": result.get("reason") or "借不了",
                               "data": result})
        self._report_to_companion()
        return self._json({"code": 0, "data": result})

    async def api_income(self):
        data = await self._payload()
        try:
            amount = float(data.get("amount") or 0)
        except Exception:
            return self._fail("金额格式不对")
        result = self.income(amount, reason=str(data.get("reason") or ""),
                             source=str(data.get("source") or ""))
        self._report_to_companion()
        return self._json({"code": 0, "data": result})

    async def terminate(self) -> None:
        self._save()
