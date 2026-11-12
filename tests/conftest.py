# -*- coding: utf-8 -*-
"""CI 里没有 astrbot：先把这几个模块塞进 sys.modules，避免导入主模块时炸掉。"""

import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (ROOT, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)


def _install_stub():
    if "astrbot" in sys.modules:
        return

    class _Logger:
        def info(self, *a, **k):
            pass

        warning = error = debug = exception = info

    class _Filter:
        class PermissionType:
            ADMIN = "admin"

        @staticmethod
        def _decorator(*a, **k):
            def wrap(func):
                return func

            return wrap

        command = llm_tool = on_llm_response = on_decorating_result = _decorator

    class Star:
        def __init__(self, context=None):
            self.context = context

    class Context:
        pass

    astrbot = types.ModuleType("astrbot")
    astrbot.logger = _Logger()

    api = types.ModuleType("astrbot.api")
    api.logger = astrbot.logger
    api.AstrBotConfig = dict

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = object
    event_mod.filter = _Filter

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Star = Star
    star_mod.Context = Context

    web_mod = types.ModuleType("astrbot.api.web")
    web_mod.json_response = lambda payload: payload
    web_mod.error_response = lambda message: {"code": 1, "message": message}
    web_mod.request = None

    for name, module in (
        ("astrbot", astrbot),
        ("astrbot.api", api),
        ("astrbot.api.event", event_mod),
        ("astrbot.api.star", star_mod),
        ("astrbot.api.web", web_mod),
    ):
        sys.modules[name] = module


_install_stub()
