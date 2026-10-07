"""Cross-process API concurrency and conservative daily spending reservations.

Request limits are always active. A CNY ceiling is active only when an admin sets
both the daily ceiling and a worst-case per-attempt price for every used provider.
Each logical call reserves three attempts, covering the current retry/fallback path.
Reservations are deliberately not refunded after failures: this is an upper bound,
not a provider invoice or a token-accurate billing report.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import geolib as G


class LimitReached(RuntimeError):
    pass


def _root() -> Path:
    return Path(os.environ.get("GEOLOOK_API_LIMITS_DIR", G.WORK / ".api-limits"))


def setting(name: str, default: str = "") -> str:
    """Read admin-managed limits afresh so other dashboard processes see changes."""
    path = G.ENV_PATH
    if path.exists():
        for line in reversed(path.read_text("utf-8").splitlines()):
            key, sep, value = line.partition("=")
            if sep and key.removeprefix("export ").strip() == name:
                return value.strip().strip("'\"")
    return os.environ.get(name, default)


def _positive_int(name: str, default: int) -> int:
    raw = setting(name, str(default))
    try:
        value = int(raw)
    except ValueError as e:
        raise LimitReached(f"{name} 必须是正整数") from e
    if value < 1:
        raise LimitReached(f"{name} 必须是正整数")
    return value


def _positive_float(name: str) -> float | None:
    raw = setting(name).strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as e:
        raise LimitReached(f"{name} 必须是正数") from e
    if not 0 < value < float("inf"):
        raise LimitReached(f"{name} 必须是正数")
    return value


def _reserve(provider: str) -> None:
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".ledger.lock").open("a+") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            path = root / f"{date.today().isoformat()}.json"
            try:
                ledger = json.loads(path.read_text("utf-8")) if path.exists() else \
                    {"calls": 0, "reserved_cny": 0.0}
                if not isinstance(ledger, dict) or not isinstance(ledger.get("calls"), int) \
                        or not isinstance(ledger.get("reserved_cny"), (int, float)):
                    raise ValueError("ledger schema")
            except (ValueError, OSError) as e:
                raise LimitReached("全局 API 用量记录损坏，已暂停调用；请管理员检查") from e
            cap = _positive_int("GEOLOOK_API_DAILY_CALL_LIMIT", 300)
            if ledger.get("calls", 0) >= cap:
                raise LimitReached(f"已达全局每日 API 调用上限 {cap} 次，请明天再试或由管理员调整")
            budget = _positive_float("GEOLOOK_API_DAILY_BUDGET_CNY")
            reserve = 0.0
            if budget is not None:
                name = f"GEOLOOK_API_MAX_ATTEMPT_COST_CNY_{provider.upper()}"
                price = _positive_float(name)
                if price is None:
                    raise LimitReached(f"已启用金额上限，但未设置 {name}；为避免超支已暂停此引擎")
                reserve = round(price * 3, 6)
                if ledger.get("reserved_cny", 0.0) + reserve > budget:
                    raise LimitReached("已达全局每日 API 费用预留上限，请明天再试或由管理员调整")
            ledger["calls"] = ledger.get("calls", 0) + 1
            ledger["reserved_cny"] = round(ledger.get("reserved_cny", 0.0) + reserve, 6)
            G.write_json(path, ledger)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def reserve(provider: str):
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    slots = _positive_int("GEOLOOK_API_MAX_CONCURRENCY", 2)
    acquired = None
    while acquired is None:
        for i in range(slots):
            fd = (root / f"slot-{i}.lock").open("a+")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = fd
                break
            except BlockingIOError:
                fd.close()
        if acquired is None:
            time.sleep(0.2)
    try:
        _reserve(provider)
        yield
    finally:
        acquired.close()
