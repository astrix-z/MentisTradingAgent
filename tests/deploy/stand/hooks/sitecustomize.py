"""Stand-only hook for the PRESET-09a acceptance (run-matrix.py, scenario 33).

Loaded by every backend worker when stand-hooks.yml puts this directory on
PYTHONPATH. While $STAND_STATE/pause-start exists, a Start that has passed
the early fence check and the validation waits right before its write
(``_apply_command``) and marks $STAND_STATE/start-held — the moment the
acceptance asks about: the flag is set now, and the write must still be
refused by the check inside the Lua script. Nothing in the product changes.
"""

from __future__ import annotations

import asyncio
import importlib.abc
import importlib.util
import os
import sys
from typing import Any

STATE = os.environ.get("STAND_STATE", "/stand-state")
TARGET = "app.api.routes.trading"


class _HoldStart(importlib.abc.MetaPathFinder):
    def find_spec(self, name: str, path: Any, target: Any = None) -> Any:
        if name != TARGET:
            return None
        sys.meta_path.remove(self)
        try:
            spec = importlib.util.find_spec(name)
        finally:
            sys.meta_path.insert(0, self)
        if spec is None or spec.loader is None:
            return spec
        loader = spec.loader
        run = loader.exec_module

        def exec_module(module: Any) -> None:
            run(module)
            apply = module._apply_command

            async def held(type_: Any, payload: Any) -> Any:
                if type_ == "start" and os.path.exists(f"{STATE}/pause-start"):
                    os.close(os.open(f"{STATE}/start-held", os.O_CREAT | os.O_WRONLY))
                    while os.path.exists(f"{STATE}/pause-start"):
                        await asyncio.sleep(0.1)
                return await apply(type_, payload)

            module._apply_command = held

        loader.exec_module = exec_module  # type: ignore[method-assign]
        return spec


sys.meta_path.insert(0, _HoldStart())
