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

PLUGIN_NAME = "Astrbot_Amo_Finance"
PLUGIN_VERSION = "1.1.1"
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
        self._load()
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

    def snapshot(self) -> dict[str, Any]:
        data = self._ledger.snapshot()
        data["city"] = self._city()
        data["city_level"] = city_level(self._city())
        data["fixed"] = monthly_fixed(self._ledger, self._prices(), self._city())
        data["fixed_total"] = round(sum(item["amount"] for item in data["fixed"]), 2)
        data["anxiety"] = anxiety_from(self._ledger, data["fixed_total"])
        return data

    def recent(self, limit: int = 5) -> list[dict[str, Any]]:
        return list(self._history[-max(1, int(limit)):])

    def charge(self, amount: float, reason: str = "", payee: str = "",
               from_savings: bool = False) -> dict[str, Any]:
        """花钱。返回 {ok, balance, short?...}。失败不抛异常。"""
        result = self._ledger.apply(
            amount, "expense", reason=reason, from_savings=from_savings
        )
        if result.get("ok"):
            self._log({
                "kind": "expense", "amount": round(float(amount), 2),
                "reason": reason, "payee": payee,
                "balance": result.get("balance"),
            })
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

    def tick(self, now: datetime | None = None) -> dict[str, Any]:
        """检查该不该发工资 / 该不该交固定支出。幂等，重启不会重复扣。"""
        now = now or datetime.now()
        today = now.date().isoformat()
        month = now.strftime("%Y-%m")
        actions: list[str] = []

        # 每月 10 号发工资
        pay_day = max(1, min(28, self._int("pay_day", 10)))
        if now.day >= pay_day and self._state.get("last_pay") != month:
            gross = self._float("salary", 25000.0)
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
            "uptime": int(_now() - self._started_at),
        }})

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
        )
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
