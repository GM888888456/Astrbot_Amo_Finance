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
        plan_assets,
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
        plan_assets,
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

try:  # 吃饭相关的表和纯函数都在 ledger 里
    from . import ledger as _ledger_mod
except ImportError:  # 直接按文件加载时
    import ledger as _ledger_mod  # type: ignore

PLUGIN_NAME = "Astrbot_Amo_Finance"
PLUGIN_VERSION = "2.2.3"
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

        # 配置自愈：升级/重装/改目录名之后，把空掉的 key 从快照补回来。
        # 快照在 plugin_data/<插件名>/config_snapshot.json（0600），升级不覆盖、
        # schema 重写也删不到它。
        try:
            from .config_guard import heal as _cfg_heal, fallback_data_dir as _cfg_data_dir
        except ImportError:
            try:
                from config_guard import heal as _cfg_heal, fallback_data_dir as _cfg_data_dir
            except Exception:
                _cfg_heal = None
        if _cfg_heal is not None:
            try:
                _guard_dir = os.path.dirname(os.path.abspath(__file__))
                _healed = _cfg_heal(self.config, plugin_name=PLUGIN_NAME,
                                    plugin_dir=_guard_dir,
                                    data_dir=_cfg_data_dir(_guard_dir, PLUGIN_NAME),
                                    logger=logger)
                if _healed.get("restored"):
                    logger.info("[config] 本次启动补回 %d 项配置（%s）"
                                % (len(_healed["restored"]), ", ".join(_healed["restored"])))
            except Exception as exc:
                logger.debug("[config] 配置自愈失败: %s" % exc)
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

    # ------------------------------------------------------------------
    # 「FX 战士」：她自己的激进交易设定
    # ------------------------------------------------------------------

    def _aggressive(self) -> bool:
        return self._bool("invest_aggressive", True)

    def _max_weight(self) -> float:
        default = 0.60 if self._aggressive() else pf.MAX_WEIGHT
        try:
            value = float(self._cfg("invest_max_weight", default) or default)
        except Exception:
            value = default
        return max(0.10, min(0.95, value))

    def _max_positions(self) -> int:
        default = 6 if self._aggressive() else pf.MAX_POSITIONS
        try:
            value = int(float(self._cfg("invest_max_positions", default) or default))
        except Exception:
            value = default
        return max(1, min(20, value))

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

    def set_persona_city(self, city: str) -> dict:
        """终端从人格读到的城市：写进状态，房车/房租按它重算。"""
        city = str(city or "").strip()
        if not city:
            return {"ok": False, "reason": "空城市"}
        changed = str(self._state.get("persona_city") or "") != city
        self._state["persona_city"] = city
        if changed:
            try:
                plan_assets(self._ledger, city, self._salary_net(),
                            persona_car=str(self._state.get("persona_car") or ""),
                            persona_house=str(self._state.get("persona_house") or ""))
                self._save()
            except Exception as exc:
                logger.debug("[amo_fin] 按城市重算房车失败: %s" % exc)
        return {"ok": True, "changed": changed, "city": city}

    def _city(self) -> str:
        """城市：终端推来的人格城市优先，其次天气，最后默认。"""
        persona_city = str(self._state.get("persona_city") or "").strip()
        if persona_city:
            return persona_city
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
            house_kind=str(led.get("house_kind") or ""),
            house_desc=str(led.get("house_desc") or ""),
            house_rent=float(led.get("house_rent") or 0.0),
            car_kind=str(led.get("car_kind") or ""),
            car_desc=str(led.get("car_desc") or ""),
            car_rent=float(led.get("car_rent") or 0.0),
            asset_seed=str(led.get("asset_seed") or ""),
        )
        self._state = data.get("state") if isinstance(data.get("state"), dict) else {}
        history = data.get("history")
        self._history = list(history) if isinstance(history, list) else []
        del self._history[:-HISTORY_LIMIT]
        try:
            if self._backfill_history_reasons():
                self._save()
        except Exception as exc:
            logger.debug("[amo_fin] 流水原因回填失败: %s" % exc)
        try:
            self._backfill_stock_flow()
        except Exception as exc:
            logger.debug("[amo_fin] 股票流水回填失败: %s" % exc)
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
        # 房车没规划过就按工资 + 城市自动安排（老账本也会补上）
        if not str(self._ledger.house_desc or "").strip():
            try:
                plan_assets(self._ledger, self._city(), self._salary_net(),
                            persona_car=str(self._state.get("persona_car") or ""),
                            persona_house=str(self._state.get("persona_house") or ""))
                self._save()
                logger.info("[amo_fin] 房车自动安排：%s｜%s",
                            self._ledger.house_desc, self._ledger.car_desc)
            except Exception as exc:
                logger.warning("[amo_fin] 房车自动安排失败: %s" % exc)

    def _backfill_history_reasons(self) -> bool:
        """老流水里 borrow/transfer_in 没写原因：补成中文，面板不再显示英文 kind。"""
        changed = False
        for entry in self._history:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("kind") or "")
            reason = str(entry.get("reason") or "").strip()
            if kind == "borrow" and not reason:
                channel = str(entry.get("channel") or "transfer")
                entry["reason"] = "借款：%s" % ("红包" if channel == "red" else "转账")
                changed = True
            elif kind == "transfer_in" and reason in ("", "用户转账"):
                entry["reason"] = "借款：他转给我的"
                changed = True
        return changed

    def _backfill_stock_flow(self) -> None:
        """1.9.1 之前股票买卖没写流水：把已有的补进来，只补一次。

        银行要能查到每一笔：以后每笔买卖都会实时写；历史的那几笔在这里补齐。
        """
        if self._state.get("stock_flow_backfilled"):
            return
        rows = self._state.get("invest_history") or []
        for item in rows:
            if not isinstance(item, dict):
                continue
            action = str(item.get("action") or "")
            if action not in ("buy", "sell"):
                continue
            try:
                ts = float(item.get("ts") or 0)
                amount = float(item.get("amount") or 0)
            except Exception:
                continue
            if amount <= 0:
                continue
            self._history.append({
                "kind": "stock_buy" if action == "buy" else "stock_sell",
                "amount": round(amount, 2),
                "reason": ("买入 %s %d 股" if action == "buy" else "卖出 %s %d 股")
                          % (item.get("name") or item.get("symbol"), int(item.get("qty") or 0)),
                "symbol": item.get("symbol"), "qty": item.get("qty"),
                "price": item.get("price"), "balance": None, "ts": ts,
                "time": datetime.fromtimestamp(ts).strftime("%m-%d %H:%M"),
                "backfilled": True,
            })
        self._history.sort(key=lambda row: float(row.get("ts") or 0))
        del self._history[:-HISTORY_LIMIT]
        self._state["stock_flow_backfilled"] = True
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
                "house_kind": self._ledger.house_kind,
                "house_desc": self._ledger.house_desc,
                "house_rent": self._ledger.house_rent,
                "car_kind": self._ledger.car_kind,
                "car_desc": self._ledger.car_desc,
                "car_rent": self._ledger.car_rent,
                "asset_seed": self._ledger.asset_seed,
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

    def set_persona_assets(self, car: str = "", house: str = "") -> dict[str, Any]:
        """终端从人格里读到的房/车（比如「奔驰」）。

        人格写了就用它，别再随机一套。
        """
        car = str(car or "").strip()
        house = str(house or "").strip()
        changed = False
        if car and str(self._state.get("persona_car") or "") != car:
            self._state["persona_car"] = car
            changed = True
        if house and str(self._state.get("persona_house") or "") != house:
            self._state["persona_house"] = house
            changed = True
        if not changed:
            return {"ok": True, "changed": False}
        try:
            plan_assets(self._ledger, self._city(), self._salary_net(), force=True,
                        persona_car=str(self._state.get("persona_car") or ""),
                        persona_house=str(self._state.get("persona_house") or ""))
            self._save()
            logger.info("[amo_fin] 按人格安排房车：%s｜%s",
                        self._ledger.house_desc, self._ledger.car_desc)
        except Exception as exc:
            logger.warning("[amo_fin] 按人格安排房车失败: %s" % exc)
            return {"ok": False, "reason": str(exc)}
        return {"ok": True, "changed": True,
                "house_desc": self._ledger.house_desc, "car_desc": self._ledger.car_desc}

    def _salary_net(self) -> float:
        """到手月薪：房车规划按到手算，别拿税前虚高。"""
        try:
            info = self.salary_info() or {}
            value = info.get("net")
            if value is None:
                value = info.get("gross") or 0
            return float(value or 0.0)
        except Exception:
            return 0.0

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

    def snapshot(self, refresh: bool = True) -> dict[str, Any]:
        data = self._ledger.snapshot()
        data["city"] = self._city()
        data["city_level"] = city_level(self._city())
        data["fixed"] = monthly_fixed(self._ledger, self._prices(), self._city())
        data["fixed_total"] = round(sum(item["amount"] for item in data["fixed"]), 2)
        data["anxiety"] = anxiety_from(self._ledger, data["fixed_total"])
        data["salary"] = self.salary_info()
        data["borrow"] = self.borrow_info()
        try:
            invest = self.invest_summary(refresh=refresh)
        except Exception as exc:
            logger.debug("[amo_fin] 投资快照失败: %s" % exc)
            invest = {"positions": [], "count": 0, "total_value": 0,
                      "total_cost": 0, "total_pnl": 0, "total_pnl_pct": 0,
                      "day_pnl": 0, "equity": self._ledger.balance,
                      "cash": self._ledger.balance}
        data["invest"] = invest
        try:
            data["gift"] = self.money_gift()
        except Exception:
            data["gift"] = {}
        try:
            data["repay"] = self.money_repay()
        except Exception:
            data["repay"] = {}
        return data

    def recent(self, limit: int = 5) -> list[dict[str, Any]]:
        return list(self._history[-max(1, int(limit)):])

    #: 记住多少次幂等号（重启后同一笔不重复扣）。够覆盖「终端重启再重试」就行。
    CHARGE_ID_KEEP = 300

    def _replay_charge(self, request_id: str, amount=None, reason=None,
                       payee=None) -> dict[str, Any] | None:
        """这个幂等号之前扣成功过吗？扣过就把原流水原样返回，不再动钱。

        同一个幂等号必须是**同一笔账**：金额/理由/收款方对不上，说明调用方拿错了号，
        这时返回 ``idempotency_conflict``，绝不能把旧流水静默当成这笔的结果
        （否则旅游那笔能和医疗扣款撞号，账就乱了）。
        """
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

        def conflict(reason_text: str) -> dict[str, Any]:
            return {"ok": False, "code": "idempotency_conflict", "replayed": True,
                    "request_id": request_id, "reason": reason_text,
                    "balance": self._ledger.balance}

        stored_amount = entry.get("amount")
        if amount is not None and stored_amount is not None:
            try:
                if abs(round(float(stored_amount), 2) - round(float(amount), 2)) > 0.005:
                    return conflict("同一幂等号的金额不一致（原来 %.2f，这次 %.2f）"
                                    % (float(stored_amount), float(amount)))
            except (TypeError, ValueError):
                pass
        for label, stored_key, given in (("理由", "reason", reason),
                                         ("收款方", "payee", payee)):
            stored_text = str(entry.get(stored_key) or "").strip()
            given_text = str(given or "").strip()
            if stored_text and given_text and stored_text != given_text:
                return conflict("同一幂等号的%s不一致（原来「%s」，这次「%s」）"
                                % (label, stored_text, given_text))
        result["repeated"] = True
        return result

    def _remember_charge(self, request_id: str, result: dict[str, Any],
                         amount=None, reason: str = "", payee: str = "") -> None:
        """把成功的扣款记进账本状态（跟 book.json 一起落盘）。

        金额/理由/收款方一起存：下次同号重放时才知道是不是同一笔账。
        """
        if not request_id or not result.get("ok"):
            return
        records = self._state.get("charge_ids")
        if not isinstance(records, dict):
            records = {}
            self._state["charge_ids"] = records
        try:
            stored_amount = round(float(amount), 2) if amount is not None else None
        except (TypeError, ValueError):
            stored_amount = None
        records[request_id] = {"ts": _now(), "amount": stored_amount,
                               "reason": str(reason or ""), "payee": str(payee or ""),
                               "result": dict(result)}
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
        replay = self._replay_charge(rid, amount=amount, reason=reason, payee=payee)
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
            self._remember_charge(rid, result, amount=amount, reason=reason, payee=payee)
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
                "reason": "借款：他转给我的",
            })
            self._note_gift(float(amount), "transfer")
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

    #: 收到钱之后多久内她还记得说句好听的
    GIFT_NOTE_TTL = 900

    def _note_gift(self, amount: float, channel: str) -> None:
        self._state["money_gift"] = {
            "ts": _now(), "amount": round(float(amount or 0), 2),
            "channel": channel, "owed": round(self._ledger.owed_to_user, 2),
        }

    def money_gift(self) -> dict[str, Any]:
        """最近一次收到钱的记录（15 分钟内有效），终端用它让她说句好听的。"""
        note = self._state.get("money_gift")
        if not isinstance(note, dict):
            return {}
        try:
            age = _now() - float(note.get("ts") or 0)
        except Exception:
            age = 1e9
        if age < 0 or age > self.GIFT_NOTE_TTL:
            return {}
        return {
            "amount": round(float(note.get("amount") or 0), 2),
            "channel": str(note.get("channel") or "transfer"),
            "owed": round(float(note.get("owed") or 0), 2),
            "seconds": int(age),
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
            "reason": "借款：%s" % ("红包" if channel == "red" else "转账"),
        })
        self._note_gift(value, channel)
        self._save()
        logger.info("[amo_fin] 她借了 %.2f（%s），共欠 %.2f"
                    % (value, "红包" if channel == "red" else "转账", self._ledger.owed_to_user))
        return {"ok": True, "channel": channel, "amount": value,
                "balance": round(self._ledger.balance, 2),
                "owed": round(self._ledger.owed_to_user, 2),
                "left": self.borrow_info()}

    #: 还完钱之后多久内她还记得提一句
    REPAY_NOTE_TTL = 900

    def _note_repay(self, amount: float, remaining: float) -> None:
        self._state["money_repay"] = {
            "ts": _now(), "amount": round(float(amount or 0), 2),
            "remaining": round(float(remaining or 0), 2),
        }

    def money_repay(self) -> dict[str, Any]:
        """最近一次还款记录（15 分钟内有效），终端用它让她说「我还你钱了」。"""
        note = self._state.get("money_repay")
        if not isinstance(note, dict):
            return {}
        try:
            age = _now() - float(note.get("ts") or 0)
        except Exception:
            age = 1e9
        if age < 0 or age > self.REPAY_NOTE_TTL:
            return {}
        return {
            "amount": round(float(note.get("amount") or 0), 2),
            "remaining": round(float(note.get("remaining") or 0), 2),
            "seconds": int(age),
            "ts": float(note.get("ts") or 0),
        }

    def repay(self, amount: float, notify: bool = True) -> dict[str, Any]:
        result = self._ledger.repay_user(amount)
        if result.get("ok"):
            self._log({"kind": "repay", "amount": result["paid"], "reason": "还他的钱",
                       "balance": result.get("balance"),
                       "remaining": result.get("remaining")})
            self._note_repay(result["paid"], result.get("remaining"))
            if notify:
                self._notify_repay(result["paid"], result.get("remaining") or 0)
            self._save()
        return result

    def _notify_repay(self, amount: float, remaining: float) -> None:
        """自动还款后给终端发一条消息；手动工具自己会回，不重复发。"""
        companion = self._companion()
        notifier = getattr(companion, "notify_repay", None) if companion is not None else None
        if not callable(notifier):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            loop.create_task(notifier(float(amount or 0), float(remaining or 0)))
        except Exception as exc:
            logger.debug("[amo_fin] 还款通知失败: %s" % exc)

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
            await asyncio.to_thread(self._seed_invest_once)
        except Exception as exc:
            logger.warning("[amo_fin] 首次建仓失败: %s" % exc)
        while True:
            try:
                await asyncio.to_thread(self.tick)
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

    def fx_rate(self, currency: str) -> float:
        """港币/美元折人民币的汇率：给聊天里的「[转账]100 USD」用。"""
        code = str(currency or "CNY").strip().upper()
        try:
            return float(self._fx().get(code, 1.0) or 1.0)
        except Exception:
            return 1.0

    def _watchlist(self) -> list[str]:
        raw = self._cfg("invest_watchlist", list(pf.DEFAULT_WATCHLIST))
        if isinstance(raw, str):
            raw = [item for item in raw.replace("，", ",").split(",") if item.strip()]
        out = []
        for item in raw or []:
            code = market_mod.normalize(str(item))
            if code and code not in out:
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

    def invest_summary(self, force: bool = False,
                       with_stance: bool = True,
                       refresh: bool = True) -> dict[str, Any]:
        if refresh:
            self._invest_refresh(force=force)
        summary = pf.summarize(self._positions(), self._fx())
        equity = round(self._ledger.balance + summary["total_value"], 2)
        summary["equity"] = equity
        summary["cash"] = round(self._ledger.balance, 2)
        if with_stance:
            try:
                try:
                    stance_row = self.market_stance(refresh=refresh)
                except TypeError:
                    stance_row = self.market_stance()
                summary["stance"] = (stance_row or {}).get("stance") or ""
            except Exception:
                summary["stance"] = ""
        summary["leek"] = dict(self._state.get("invest_leek") or {})
        summary["bankrupt"] = self._bankrupt_from(summary)
        summary["aggressive"] = self._aggressive()
        summary["max_weight"] = round(self._max_weight(), 3)
        summary["max_positions"] = self._max_positions()
        return summary

    def _bankrupt_from(self, summary: dict[str, Any]) -> dict[str, Any]:
        """她离「破产」还有多远——终端据此让她哭。

        现金见底、权益也很小 → broke；浮亏 -12% 以上 → losing；-25% 以上 → ruined。
        """
        try:
            pnl_pct = float(summary.get("total_pnl_pct") or 0)
            cash = float(summary.get("cash") or 0)
            equity = float(summary.get("equity") or 0)
        except Exception:
            pnl_pct = cash = equity = 0.0
        level = "ok"
        if cash < 100 and equity < 1500:
            level = "broke"
        elif pnl_pct <= -25.0:
            level = "ruined"
        elif pnl_pct <= -12.0:
            level = "losing"
        return {"level": level, "pnl_pct": round(pnl_pct, 2),
                "cash": round(cash, 2), "equity": round(equity, 2),
                "owed": round(self._ledger.owed_to_user, 2)}

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
        # 单只上限跟配置走：激进模式放得更宽，但仍有顶
        summary = self.invest_summary()
        equity = max(summary["equity"], 1.0)
        positions = self._positions()
        held_count = len([p for p in positions.values() if int(p.get("qty") or 0) > 0])
        if code not in positions and held_count >= self._max_positions():
            return {"ok": False,
                    "reason": "已经拿了 %d 只，先卖一只再买" % self._max_positions()}
        holding = self._positions().get(code) or {}
        rate = float(self._fx().get(str(holding.get("currency") or "CNY"), 1.0) or 1.0)
        held_value = float(holding.get("qty") or 0) * float(holding.get("last") or price) * rate
        after = held_value + check["cost"]
        if after / equity > self._max_weight() + 0.001:
            return {"ok": False, "reason": "这只占得太多了，别一把梭",
                    "limit": round(equity * self._max_weight(), 2)}
        result = self._ledger.apply(
            check["cost"], "transfer_out",
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
        self._log({
            "kind": "stock_buy", "amount": check["cost"],
            "reason": "买入 %s %d 股" % (data.get("name") or code, qty),
            "symbol": code, "qty": qty, "price": price,
            "balance": result.get("balance"),
        })
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
            check["proceeds"], "refund",
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
        self._log({
            "kind": "stock_sell", "amount": check["proceeds"],
            "reason": "卖出 %s %d 股" % (pos.get("name") or code, qty),
            "symbol": code, "qty": qty, "price": price,
            "cost": round(avg_cost * qty, 2),
            "pnl": round(check["proceeds"] - avg_cost * qty, 2),
            "balance": result.get("balance"),
        })
        self._save()
        self._report_to_companion()
        return {"ok": True, "symbol": code, "name": pos.get("name"), "qty": qty,
                "price": price, "proceeds": check["proceeds"],
                "balance": round(self._ledger.balance, 2)}

    def _seed_invest_once(self) -> None:
        """第一次装好：让她真的「有股票」，不是只有个空账户。

        激进模式下不再只买一手：最多挑 3 只各买一手，受单只上限和现金约束。
        用户不用替她买；她自己开户、自己下单。
        """
        if self._state.get("invest_seeded"):
            return
        if self._positions():
            self._state["invest_seeded"] = True
            self._save()
            return
        bought = 0
        target = min(3, self._max_positions())
        for code in self._watchlist():
            if bought >= target:
                break
            quotes = self._board().quotes([code])
            data = quotes.get(code)
            if not data:
                continue
            price = float(data.get("price") or 0)
            market = market_mod.market_of(code)
            check = pf.can_buy(100, price, self._ledger.balance, market)
            if not check.get("ok"):
                continue
            if check["cost"] > self._ledger.balance * self._max_weight():
                continue          # 一手就超过上限：这只太贵，换一只
            result = self.invest_buy(code, qty=100)
            if result.get("ok"):
                bought += 1
                self._state["invest_seed_info"] = {
                    "symbol": result.get("symbol"), "name": result.get("name"),
                    "qty": result.get("qty"), "price": result.get("price"),
                    "cost": result.get("cost"), "ts": _now(),
                }
                self._save()
                logger.info("[amo_fin] 建仓：买了 100 股 %s（¥%.2f）"
                            % (result.get("name"), float(result.get("cost") or 0)))
        if bought:
            self._state["invest_seeded"] = True
            self._save()
            return
        logger.info("[amo_fin] 这次没买成（行情或余额不够），下次再试")

    #: 韭菜档位阈值：涨/跌超过这个百分比就进入相应的情绪档
    LEEK_UP_PCT = 3.0
    LEEK_DOWN_PCT = -3.0

    def _today(self) -> str:
        return date.today().isoformat()

    def market_stance(self, refresh: bool = True) -> dict[str, Any]:
        """今天她是什么「韭菜状态」：过度自信 / 死扛 / 正常。

        一旦当天触发过就锁住：上午跌 4% 死扛，下午涨回来也还是死扛——
        情绪不是实时跟随，这才像人。
        """
        today = self._today()
        record = self._state.get("invest_stance")
        if isinstance(record, dict) and record.get("date") == today:
            return record
        try:
            signal = self.mood_signal(refresh=refresh)
        except TypeError:
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
            room = max(0.0, equity * self._max_weight() - held)
            if room <= 0:
                continue
            budget = min(self._ledger.balance * 0.95, room * 0.98)
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
        if (not isinstance(day, dict) or day.get("date") != today
                or "open_value" not in day):
            # 只记「开盘持仓市值」：权益里含现金，转账/还款/工资一进来就会被
            # 当成炒股盈利，之前就是这么把 7150 借款算成 +24.54% 的。
            summary = pf.summarize(self._positions(), self._fx())
            open_value = round(max(0.0, summary["total_value"] - summary["day_pnl"]), 2)
            self._state["invest_day"] = {
                "date": today, "open": open_value, "open_value": open_value,
            }
            self._save()
        stance = str((self.market_stance() or {}).get("stance") or "")
        try:
            total_pct = float(self.mood_signal().get("total_pnl_pct") or 0)
        except Exception:
            total_pct = 0.0
        if stance == "overconfident" or (self._aggressive() and total_pct <= -10.0):
            # 上头 / 亏红了眼：她自己加仓（守配置上限），没人替她点按钮
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
                self._positions(), self._ledger.balance, self._watchlist(), self._fx(),
                self._rng, aggressive=self._aggressive(),
                max_weight=self._max_weight(), max_positions=self._max_positions(),
            )
            if stance == "hodl":
                # 死扛：赚了也不卖、亏了更不卖（这次连止盈都不执行）
                actions = [a for a in actions if a.get("action") != "sell"]
        for item in actions[:(4 if self._aggressive() else 2)]:
            if item.get("action") == "sell":
                result = self.invest_sell(item["symbol"], item.get("qty") or 0)
            else:
                result = self.invest_buy(item["symbol"], budget=item.get("budget") or 0)
            if result.get("ok"):
                logger.info("[amo_fin] 模拟交易：%s %s（%s）"
                            % (item.get("action"), item.get("symbol"), item.get("why") or ""))

    def mood_signal(self, refresh: bool = True) -> dict[str, Any]:
        """今天的盈亏，给终端拿去影响心情。

        **只看持仓的行情波动**（`summary["day_pnl"]` = 现价 vs 昨收），
        绝不拿「权益 − 开盘权益」算：那里面混着现金，用户一转钱就会
        被误判成股市大涨（曾把 7150 借款算成 +24.54%、¥5985）。
        """
        summary = self.invest_summary(with_stance=False, refresh=refresh)
        day = self._state.get("invest_day") or {}
        base = float(day.get("open_value") or 0) if isinstance(day, dict) else 0.0
        day_pnl = round(float(summary.get("day_pnl") or 0), 2)
        if base <= 0:
            base = round(float(summary.get("total_value") or 0) - day_pnl, 2)
        pct = round(day_pnl / base * 100.0, 2) if base > 0 else 0.0
        equity = summary["equity"]
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
            interest = self._credit_savings_interest()
            if interest > 0:
                actions.append("利息")
        except Exception as exc:
            logger.warning("[amo_fin] 利息结算失败: %s" % exc)

        try:
            repaid = self._auto_repay()
            if repaid > 0:
                actions.append("还款")
        except Exception as exc:
            logger.warning("[amo_fin] 自动还款失败: %s" % exc)

        try:
            self._invest_tick()
        except Exception as exc:
            logger.warning("[amo_fin] 投资结算失败: %s" % exc)

        return {"actions": actions, "today": today, "month": month}

    # ------------------------------------------------------------------
    # 银行：储蓄利息 / 自动还款
    # ------------------------------------------------------------------

    SAVINGS_RATE_DEFAULT = 1.5
    REPAY_KEEP_MIN = 800.0
    REPAY_RESERVE_EXTRA = 300.0
    REPAY_DAY_MAX = 5000.0
    REPAY_MIN = 50.0

    def savings_rate(self) -> float:
        """储蓄年化利率（%）。借款没有利息——情侣之间不收。"""
        try:
            value = float(self._cfg("savings_rate", self.SAVINGS_RATE_DEFAULT) or 0)
        except Exception:
            value = self.SAVINGS_RATE_DEFAULT
        return max(0.0, min(10.0, value))

    def _credit_savings_interest(self) -> float:
        """储蓄利息：按年化、按天算，停机漏掉的天数一起补。

        只进储蓄，不动余额；同一天只结算一次。
        """
        savings = float(self._ledger.savings or 0)
        rate = self.savings_rate()
        if savings <= 0 or rate <= 0:
            self._state["last_interest_date"] = _today()
            return 0.0
        today = date.today()
        last_raw = self._state.get("last_interest_date")
        try:
            last = (date.fromisoformat(str(last_raw)) if last_raw
                    else date.fromordinal(today.toordinal() - 1))
        except Exception:
            last = date.fromordinal(today.toordinal() - 1)
        days = (today - last).days
        if days <= 0:
            return 0.0
        days = min(days, 366)
        interest = round(savings * rate / 100.0 / 365.0 * days, 2)
        self._state["last_interest_date"] = today.isoformat()
        if interest < 0.01:
            self._save()
            return 0.0
        result = self._ledger.credit_interest(interest)
        if not result.get("ok"):
            return 0.0
        self._log({
            "kind": "interest", "amount": interest,
            "reason": "储蓄利息（年化 %.2f%%，%d 天）" % (rate, days),
            "savings": result.get("savings"),
            "balance": round(self._ledger.balance, 2),
        })
        self._save()
        logger.info("[amo_fin] 储蓄利息 %.2f（年化 %.2f%%，%d 天）" % (interest, rate, days))
        return interest

    def auto_repay_enabled(self) -> bool:
        return self._bool("auto_repay", True)

    def _auto_repay(self) -> float:
        """她有钱了会主动还他：留够固定支出和一点生活费，剩下的拿一半还。

        借款没有利息；一天最多还一次，还清后 owed 归零。
        """
        if not self.auto_repay_enabled():
            return 0.0
        owed = float(self._ledger.owed_to_user or 0)
        if owed <= 0:
            return 0.0
        today = _today()
        if self._state.get("last_repay_date") == today:
            return 0.0
        try:
            fixed_total = sum(item["amount"] for item in
                              monthly_fixed(self._ledger, self._prices(), self._city()))
        except Exception:
            fixed_total = 0.0
        reserve = max(self.REPAY_KEEP_MIN, fixed_total) + self.REPAY_RESERVE_EXTRA
        spare = round(float(self._ledger.balance) - reserve, 2)
        if spare < self.REPAY_MIN * 2:
            return 0.0
        amount = round(min(owed, spare * 0.5, self.REPAY_DAY_MAX), 2)
        if amount < self.REPAY_MIN:
            return 0.0
        result = self.repay(amount)
        if not result.get("ok"):
            return 0.0
        self._state["last_repay_date"] = today
        self._save()
        logger.info("[amo_fin] 她还了他 %.2f，还欠 %.2f" %
                    (float(result.get("paid") or 0), float(result.get("remaining") or 0)))
        return float(result.get("paid") or 0)

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
            snap = self.snapshot(refresh=False)
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
        snap = await asyncio.to_thread(self.snapshot)
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

    # ------------------------------------------------------------------
    # 吃饭：报价 → 审批 → 扣款 → 记饱食度
    # ------------------------------------------------------------------
    # 挂载插件和终端都不许直接改饱食度，只能走 food_propose；钱只在账本里动。

    def _data(self):
        """找记忆库（饱食度、食材柜都在它那儿）。找不到返回 None。"""
        try:
            from . import link as _l
        except ImportError:
            import link as _l  # type: ignore
        try:
            return _l._find(getattr(self, "context", None),
                            _l.DATA_NAMES, _l.DATA_MARKERS, require_mem=True)
        except Exception:
            return None

    def _pantry_map(self) -> dict[str, float]:
        data = self._data()
        getter = getattr(data, "pantry", None) if data is not None else None
        if not callable(getter):
            return {}
        try:
            items = getter() or []
        except Exception:
            return {}
        out: dict[str, float] = {}
        for item in items:
            try:
                out[str(item.get("item"))] = float(item.get("qty") or 0)
            except Exception:
                continue
        return out

    def vitals(self) -> dict[str, Any]:
        """当前饱食度/水分（只读）。"""
        data = self._data()
        getter = getattr(data, "vitals", None) if data is not None else None
        if not callable(getter):
            return {}
        try:
            return getter() or {}
        except Exception:
            return {}

    def _groceries(self):
        """菜价/药价模块（Finance 自己的 groceries.py）。"""
        return getattr(_ledger_mod, "_groceries", lambda: None)()

    def market_today(self) -> dict[str, Any]:
        """今天的菜价：城市档位 × 季节 × 天气 × 节假日，同一天问几次都一样。"""
        module = self._groceries()
        if module is None:
            return {"ok": False, "reason": "没有菜价模块"}
        city = self._city()
        level = _ledger_mod.city_level(city)
        weather = ""
        try:
            companion = self._companion()
            state = getattr(companion, "state", None)
            weather = str(((getattr(state, "weather", None) or {}) or {}).get("text") or "")
        except Exception:
            weather = ""
        try:
            data = module.market_day(city_level=level, weather=weather)
        except Exception as exc:
            logger.debug("[amo_fin] 取菜价失败: %s" % exc)
            return {"ok": False, "reason": str(exc)}
        data["ok"] = True
        data["city"] = city
        return data

    def drug_price(self, key: str, channel: str = "药店") -> float:
        module = self._groceries()
        if module is None:
            return 0.0
        try:
            return float(module.drug_price(key, channel))
        except Exception:
            return 0.0

    def drugs_for(self, illness: str) -> list:
        module = self._groceries()
        if module is None:
            return []
        try:
            return module.drugs_for(illness)
        except Exception:
            return []

    def food_quote(self) -> dict[str, Any]:
        """现在能吃什么、多少钱、能回多少饱食度、自己做饭缺什么料。"""
        city = self._city()
        options = _ledger_mod.food_options(city, self._prices(), self._pantry_map())
        options["balance"] = round(self.balance(), 2)
        options["vitals"] = self.vitals()
        return options

    def food_buy(self, want: dict[str, Any], request_id: str = "") -> dict[str, Any]:
        """买菜：先算钱、扣到钱才往食材柜里放。"""
        city = self._city()
        total, items = _ledger_mod.grocery_cost(want or {}, city)
        if not items:
            return {"ok": False, "code": "bad_request", "reason": "没说要买什么"}
        rid = str(request_id or "").strip() or "food-buy-%d" % int(_now() // 60)
        result = self.charge(total, reason="买菜", payee="菜场", request_id=rid)
        if not result.get("ok"):
            return {
                "ok": False, "code": "no_money", "reason": "买菜的钱不够",
                "total": total, "items": items, "balance": round(self.balance(), 2),
                "short": round(float(result.get("short") or 0), 2),
            }
        data = self._data()
        stored = []
        adder = getattr(data, "pantry_add", None) if data is not None else None
        if callable(adder):
            for line in items:
                try:
                    adder(line["item"], line["qty"])
                    stored.append(line["item"])
                except Exception as exc:
                    logger.debug("[amo_fin] 食材入库失败 %s: %s" % (line.get("item"), exc))
        return {
            "ok": True, "total": total, "items": items, "stored": stored,
            "balance": round(self.balance(), 2),
            "pantry": self._pantry_map(),
        }

    def food_propose(self, name: str, method: str = "order",
                     request_id: str = "") -> dict[str, Any]:
        """进食提案（审批器）。

        - ``order``：买现成的，按物价表扣钱，再回记忆库加饱食度；
        - ``cook``：自己做饭，先看食材柜够不够，够了才扣食材、加饱食度（不再扣钱，
          食材的钱在买的时候已经付过）；
        - ``drink``：喝水/奶茶，加水分（奶茶顺带一点饱食度）；
        - ``grocery``：只买菜，等同 :meth:`food_buy`。

        余额不够一律返回 ``code="no_money"``，**不扣、不加**，让终端把它变成情绪。
        """
        dish = str(name or "").strip()
        if not dish:
            return {"ok": False, "code": "bad_request", "reason": "没说要吃什么"}
        mode = str(method or "order").strip().lower()
        mode = {"eat": "order", "吃": "order", "做饭": "cook", "做": "cook",
                "喝": "drink", "喝水": "drink", "买": "grocery", "买菜": "grocery",
                "auto": "auto", "随便": "auto", "看情况": "auto"}.get(mode, mode)
        if mode == "grocery":
            return self.food_buy({dish: 1.0}, request_id=request_id)

        city = self._city()
        data = self._data()
        weather = ""
        place = ""
        try:
            companion = self._companion()
            state = getattr(companion, "state", None)
            weather = str(((getattr(state, "weather", None) or {}) or {}).get("text") or "")
            activity = getattr(state, "activity", None) or {}
            if callable(getattr(state, "current_activity", None)):
                activity = state.current_activity() or activity
            place = str((activity or {}).get("place") or "")
        except Exception:
            weather = ""
            place = ""
        at_home = any(word in place for word in ("家", "小窝", "住处", "公寓", "宿舍", "卧室"))
        if mode == "auto" and dish in _ledger_mod.DRINKS:
            mode = "drink"
        switched = ""
        if mode == "auto":
            cookable = dish in _ledger_mod.RECIPES
            orderable = dish in _ledger_mod.READY_FOODS
            if not at_home and orderable:
                mode = "order"
                switched = "现在在外面（%s），点外卖" % (place or "外面")
            elif not at_home and cookable:
                return {"ok": False, "code": "not_home", "dish": dish,
                        "reason": "在外面做不了%s，得回家或点外卖" % dish,
                        "switched": "现在在外面（%s）" % (place or "外面")}
            elif cookable and orderable:
                cook_cost = _ledger_mod.recipe_cost(dish, city, weather)
                order_price = _ledger_mod.ready_price(dish, self._prices(), city)
                if order_price > 0 and cook_cost <= order_price:
                    mode = "cook"
                    switched = "今日菜价算下来，自己做约 %.1f 元，外卖 %.1f 元，自己做" % (
                        cook_cost, order_price)
                else:
                    mode = "order"
                    switched = "今日菜价算下来，自己做约 %.1f 元，外卖 %.1f 元，点外卖" % (
                        cook_cost, order_price)
            elif cookable:
                mode = "cook"
                switched = "外卖没有%s，自己做" % dish
            elif orderable:
                mode = "order"
            else:
                return {"ok": False, "code": "unknown", "reason": "菜单里没有这道菜"}
        if mode == "cook" and not at_home:
            return {"ok": False, "code": "not_home", "dish": dish,
                    "reason": "在外面做不了%s，得回家或点外卖" % dish,
                    "switched": "现在在外面（%s）" % (place or "外面")}
        if mode in ("cook", "order"):
            cookable = dish in _ledger_mod.RECIPES
            orderable = dish in _ledger_mod.READY_FOODS
            if mode == "cook" and cookable and orderable:
                cook_cost = _ledger_mod.recipe_cost(dish, city, weather)
                order_price = _ledger_mod.ready_price(dish, self._prices(), city)
                if order_price > 0 and order_price < cook_cost:
                    mode = "order"
                    switched = "做饭约 %.1f 元，外卖 %.1f 元，改点外卖" % (
                        cook_cost, order_price)
            elif mode == "order" and not orderable and cookable:
                if not at_home:
                    return {"ok": False, "code": "not_home", "dish": dish,
                            "reason": "外卖没有%s，在外面也做不了，得回家" % dish,
                            "switched": "现在在外面（%s）" % (place or "外面")}
                mode = "cook"
                switched = "外卖没有%s，改自己做" % dish
        cost = 0.0
        satiety = 0
        hydration = 0
        payee = "饭馆"
        reason = ""

        if mode == "drink":
            spec = _ledger_mod.DRINKS.get(dish)
            if spec is None:
                return {"ok": False, "code": "unknown", "reason": "没有这种喝的"}
            cost = _ledger_mod.drink_price(dish, self._prices(), city)
            hydration = int(spec.get("hydration") or 0)
            satiety = int(spec.get("satiety") or 0)
            payee = "小卖部" if cost > 0 else "家里"
            reason = "喝的：%s" % dish
        elif mode == "cook":
            spec = _ledger_mod.RECIPES.get(dish)
            if spec is None:
                return {"ok": False, "code": "unknown", "reason": "不会做这道菜"}
            pantry = self._pantry_map()
            missing = []
            for item, need in (spec.get("items") or {}).items():
                have = float(pantry.get(item, 0.0))
                if have + 1e-9 < float(need):
                    missing.append({"item": item, "need": round(float(need), 2),
                                    "have": round(have, 2)})
            if missing:
                return {"ok": False, "code": "need_grocery", "reason": "食材不够",
                        "dish": dish, "switched": switched, "missing": missing,
                        "grocery": _ledger_mod.grocery_cost(
                            {m["item"]: m["need"] - m["have"] for m in missing}, city)[0]}
            satiety = int(spec.get("satiety") or 0)
            payee = "家里"
            reason = "自己做饭：%s" % dish
        else:
            spec = _ledger_mod.READY_FOODS.get(dish)
            if spec is None:
                return {"ok": False, "code": "unknown", "reason": "没这道菜"}
            cost = _ledger_mod.ready_price(dish, self._prices(), city)
            satiety = int(spec.get("satiety") or 0)
            reason = "吃饭：%s" % dish

        rid = str(request_id or "").strip()
        if not rid:
            rid = "food-%s-%s-%d" % (mode, dish, int(_now() // 60))
        if cost > 0:
            result = self.charge(cost, reason=reason, payee=payee, request_id=rid)
            if not result.get("ok"):
                return {
                    "ok": False, "code": "no_money", "reason": "没钱了",
                    "dish": dish, "price": round(cost, 2),
                    "short": round(float(result.get("short") or 0), 2),
                    "balance": round(self.balance(), 2), "satiety": satiety,
                }

        if mode == "cook" and data is not None:
            taker = getattr(data, "pantry_take", None)
            if callable(taker):
                for item, need in (_ledger_mod.RECIPES.get(dish, {}).get("items") or {}).items():
                    try:
                        taker(item, float(need))
                    except Exception as exc:
                        logger.debug("[amo_fin] 扣食材失败 %s: %s" % (item, exc))

        after: dict[str, Any] = {}
        if data is not None:
            try:
                if satiety > 0:
                    after = data.eat(satiety, note=dish) or {}
                if hydration > 0:
                    after = data.drink(hydration, note=dish) or after
            except Exception as exc:
                logger.debug("[amo_fin] 记饱食度失败: %s" % exc)

        return {
            "ok": True, "dish": dish, "method": mode,
            "price": round(cost, 2), "satiety": satiety, "hydration": hydration,
            "reason": reason, "balance": round(self.balance(), 2),
            "vitals": after, "pantry": self._pantry_map() if mode == "cook" else None,
            "switched": switched,
        }

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
            ("/food/quote", self.api_food_quote, ["GET", "POST"]),
            ("/food/propose", self.api_food_propose, ["POST"]),
            ("/food/buy", self.api_food_buy, ["POST"]),
            ("/market", self.api_market, ["GET"]),
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

    async def api_market(self):
        return self._json({"code": 0, "data": self.market_today()})

    async def api_food_quote(self):
        return self._json({"code": 0, "data": self.food_quote()})

    async def api_food_propose(self):
        payload = await self._payload()
        name = str(payload.get("name") or payload.get("dish") or "").strip()
        method = str(payload.get("method") or "order").strip()
        return self._json(self.food_propose(name, method,
                                           str(payload.get("request_id") or "")))

    async def api_food_buy(self):
        payload = await self._payload()
        want = payload.get("items") if isinstance(payload.get("items"), dict) else None
        if not want:
            name = str(payload.get("item") or "").strip()
            if not name:
                return self._json({"ok": False, "code": "bad_request", "reason": "没说要买什么"})
            try:
                qty = float(payload.get("qty") or 1)
            except (TypeError, ValueError):
                qty = 1.0
            want = {name: qty}
        return self._json(self.food_buy(want, str(payload.get("request_id") or "")))

    async def api_health(self):
        """我自己连没连上终端/记忆库。面板顶部那一行就看这个。"""
        return self._json({"code": 0, "data": _link_probe(self)})

    async def api_status(self):
        self.ensure_tick_task()
        snap = await asyncio.to_thread(self.snapshot)
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
            "house_kind": snap.get("house_kind"), "house_desc": snap.get("house_desc"),
            "house_rent": snap.get("house_rent"), "mortgage": snap.get("mortgage"),
            "car_kind": snap.get("car_kind"), "car_desc": snap.get("car_desc"),
            "car_rent": snap.get("car_rent"), "car_loan": snap.get("car_loan"),
            "persona_car": str(self._state.get("persona_car") or ""),
            "persona_house": str(self._state.get("persona_house") or ""),
            "salary": snap.get("salary") or self.salary_info(),
            "invest": snap.get("invest") or {},
            "borrow": snap.get("borrow") or self.borrow_info(),
            "gift": snap.get("gift") or {},
            "repay": snap.get("repay") or {},
            "savings_rate": self.savings_rate(),
            "auto_repay": self.auto_repay_enabled(),
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
        invest = await asyncio.to_thread(self.invest_summary, force)
        return self._json({"code": 0, "data": invest})

    async def api_history(self):
        return self._json({"code": 0, "data": {"items": self.recent(100)}})

    async def api_assets(self):
        """资产登记：有没有房、有没有车、有没有贷款。"""
        data = await self._payload()
        if "own_house" in data:
            self._ledger.own_house = bool(data["own_house"])
        if "own_car" in data:
            self._ledger.own_car = bool(data["own_car"])
        # 月供/租金不再由面板填写：按工资+城市自动规划（全款=自，没勾=租/贷）
        if "savings" in data:
            try:
                self._ledger.savings = round(float(data["savings"]), 2)
            except Exception:
                pass
        try:
            plan_assets(self._ledger, self._city(), self._salary_net(),
                        force=("own_house" in data or "own_car" in data),
                        persona_car=str(self._state.get("persona_car") or ""),
                        persona_house=str(self._state.get("persona_house") or ""))
        except Exception as exc:
            logger.debug("[amo_fin] 房车重新规划失败: %s" % exc)
        self._save()
        self._report_to_companion()
        snap = await asyncio.to_thread(self.snapshot)
        return self._json({"code": 0, "data": snap})

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
