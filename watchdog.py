"""Telegram polling watchdog; never starts a second polling loop.

Only bots explicitly tracked by the active plugin are observed. Recovery is
requested through AstrBot's own event and performed by its existing run loop.
No getMe probes or extra getUpdates requests are sent.
"""
from __future__ import annotations

import asyncio
import functools
import importlib
import math
from dataclasses import dataclass
from typing import Any, Callable

_PATCH = "__tayhetgfix_polling_watchdog__"


@dataclass(frozen=True)
class WatchdogSettings:
    enabled: bool = True
    interval: float = 15.0
    timeout: float = 120.0
    failures: int = 3
    cooldown: float = 120.0


def _number(value: Any, default: float, minimum: float) -> float:
    try:
        parsed = float(value)
    except (ValueError, TypeError, OverflowError):
        return default
    return max(minimum, parsed) if math.isfinite(parsed) else default


def settings_from_config(config: Any, read: Callable, as_bool: Callable) -> WatchdogSettings:
    return WatchdogSettings(
        enabled=as_bool(read(config, "watchdog_enabled", True), True),
        interval=_number(read(config, "watchdog_interval", 15), 15, 1),
        timeout=_number(read(config, "watchdog_timeout", 120), 120, 30),
        failures=int(_number(read(config, "watchdog_failures", 3), 3, 1)),
        cooldown=_number(read(config, "watchdog_cooldown", 120), 120, 30),
    )


@dataclass(eq=False)
class _State:
    adapter: Any
    application: Any = None
    bot: Any = None
    last_success: float = 0.0
    failures: int = 0
    last_recovery: float = float("-inf")
    pending_since: float | None = None
    blocked: bool = False
    recovered: bool = False
    recovery_application: Any = None
    request_timeout: float = 120.0
    rate_limited_until: float = 0.0
    task: asyncio.Task | None = None


class PollingWatchdog:
    def __init__(self, settings: WatchdogSettings, log: Callable) -> None:
        self.settings = settings
        self.log = log
        self.states: dict[int, _State] = {}
        self.patches: dict[type, dict] = {}
        self.retired: set[asyncio.Task] = set()
        self.closed = False

    def configure(self, settings: WatchdogSettings) -> None:
        restarting = self.closed or not self.settings.enabled
        self.settings = settings
        if not settings.enabled:
            self.cancel()
        else:
            self.closed = False
            for state in list(self.states.values()):
                if restarting:
                    state.last_success = asyncio.get_running_loop().time()
                    state.failures = 0
                    state.pending_since = None
                    state.blocked = False
                    state.rate_limited_until = 0.0
                    state.request_timeout = settings.timeout
                self.track(state.adapter)

    @staticmethod
    def _label(adapter: Any) -> str:
        config = getattr(adapter, "config", {})
        # Never log the bot token, proxy URL, or raw exception text.
        return str(config.get("id", "telegram")) if isinstance(config, dict) else "telegram"

    def _patch_bot(self, bot: Any) -> bool:
        cls = type(bot)
        current = getattr(cls, "get_updates", None)
        if not callable(current):
            return False
        shared = getattr(current, _PATCH, None)
        if not isinstance(shared, dict):
            shared = {"original": current, "owners": {}, "wrapper": None,
                      "class": cls, "had_own": "get_updates" in cls.__dict__}

            @functools.wraps(current)
            async def wrapped(bot_self: Any, *args: Any, **kwargs: Any) -> Any:
                owner = shared["owners"].get(id(bot_self))
                if owner is None or owner[1].bot is not bot_self or owner[0].closed:
                    return await shared["original"](bot_self, *args, **kwargs)
                manager, state = owner
                if not manager.settings.enabled or getattr(state.adapter, "_terminating", False):
                    return await shared["original"](bot_self, *args, **kwargs)
                # PTB's long-poll timeout is the second positional argument.
                server_timeout = kwargs.get("timeout", args[1] if len(args) > 1 else 0)
                if callable(getattr(server_timeout, "total_seconds", None)):
                    server_timeout = server_timeout.total_seconds()
                budget = max(manager.settings.timeout, _number(server_timeout, 0, 0) + 30)
                state.request_timeout = budget
                generation = state.application
                try:
                    result = await asyncio.wait_for(
                        shared["original"](bot_self, *args, **kwargs), timeout=budget
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if state.application is generation and state.bot is bot_self:
                        manager._failure(state, exc)
                    if isinstance(exc, TimeoutError):
                        # PTB only retries TelegramError. Translate our local
                        # timeout so PTB retains its normal retry/error path.
                        error_module = importlib.import_module("telegram.error")
                        raise error_module.TimedOut("Telegram polling watchdog timeout") from None
                    raise
                if state.application is generation and state.bot is bot_self:
                    manager._success(state)
                return result

            shared["wrapper"] = wrapped
            setattr(wrapped, _PATCH, shared)
            try:
                setattr(cls, "get_updates", wrapped)
            except (AttributeError, TypeError):
                return False
        self.patches[cls] = shared
        return True

    def _detach_bot(self, state: _State) -> None:
        for shared in self.patches.values():
            entry = shared["owners"].get(id(state.bot))
            if entry is not None and entry[0] is self and entry[1] is state:
                shared["owners"].pop(id(state.bot), None)

    def track(self, adapter: Any) -> bool:
        if self.closed or not self.settings.enabled or getattr(adapter, "_terminating", False):
            return False
        recovery = getattr(adapter, "_polling_recovery_requested", None)
        if not all(callable(getattr(recovery, name, None)) for name in ("set", "is_set")) or not callable(
            getattr(adapter, "_recreate_application", None)
        ):
            self.log("debug", "[tayhetgfix] 当前 Telegram 核心无安全恢复接口，跳过轮询看门狗")
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        application = getattr(adapter, "application", None)
        bot = getattr(application, "bot", None)
        if bot is None:
            bot = getattr(adapter, "client", None)
        if bot is None or not self._patch_bot(bot):
            self.log("warning", "[tayhetgfix] 无法监测 Telegram get_updates，跳过轮询看门狗")
            return False
        state = self.states.setdefault(id(adapter), _State(adapter))
        if state.application is not application or state.bot is not bot:
            self._detach_bot(state)
            state.application = application
            state.bot = bot
            state.last_success = loop.time()
            state.failures = 0
            state.pending_since = None
            state.blocked = False
            state.rate_limited_until = 0.0
            state.request_timeout = self.settings.timeout
        owners = self.patches[type(bot)]["owners"]
        previous = owners.get(id(bot))
        if previous is not None and previous[0] is not self:
            old_manager, old_state = previous
            old_manager._detach_bot(old_state)
            old_manager.states.pop(id(old_state.adapter), None)
            if old_state.task is not None and not old_state.task.done():
                task = old_state.task
                old_manager.retired.add(task)
                task.add_done_callback(old_manager.retired.discard)
                old_state.task = None
                task.cancel()
        owners[id(bot)] = (self, state)
        if state.task is None or state.task.done():
            state.task = loop.create_task(self._monitor(state), name="tayhetgfix-polling-watchdog")
        return True

    def _failure(self, state: _State, exc: Exception) -> None:
        if self.closed or not self.settings.enabled or state.application is not getattr(state.adapter, "application", None):
            return
        errors = importlib.import_module("telegram.error")
        network = getattr(errors, "NetworkError", ())
        bad_request = getattr(errors, "BadRequest", ())
        retry_after = getattr(errors, "RetryAfter", ())
        if isinstance(exc, retry_after):
            # Telegram rate limiting has its own retry_after policy, not a broken
            # proxy. Let PTB handle it without a watchdog restart storm.
            retry = getattr(exc, "retry_after", self.settings.interval)
            if callable(getattr(retry, "total_seconds", None)):
                retry = retry.total_seconds()
            state.rate_limited_until = asyncio.get_running_loop().time() + _number(retry, self.settings.interval, 0)
            state.failures = 0
            return
        if isinstance(exc, (TimeoutError, ConnectionError, OSError)) or (
            isinstance(exc, network) and not isinstance(exc, bad_request)
        ):
            state.failures += 1
            self.log("warning", "[tayhetgfix] Telegram[%s] 轮询网络失败 %d/%d（%s）",
                     self._label(state.adapter), state.failures, self.settings.failures, type(exc).__name__)
        elif not state.blocked:
            state.blocked = True
            self.log("error", "[tayhetgfix] Telegram[%s] 轮询发生非网络错误（%s），暂停看门狗重启；请检查 Token、Webhook 或重复运行的实例",
                     self._label(state.adapter), type(exc).__name__)

    def _success(self, state: _State) -> None:
        if self.closed or state.application is not getattr(state.adapter, "application", None):
            return
        # PTB sends a final getUpdates during stop(). A success from the old
        # generation must not claim that a freshly rebuilt client has recovered.
        confirmed = state.recovered and state.application is not state.recovery_application
        if confirmed:
            self.log("info", "[tayhetgfix] Telegram[%s] 轮询已恢复（getUpdates 正常返回）", self._label(state.adapter))
            state.recovered = False
            state.recovery_application = None
        state.last_success = asyncio.get_running_loop().time()
        state.failures = 0
        state.blocked = False
        state.rate_limited_until = 0.0

    def check(self, state: _State, now: float) -> None:
        adapter = state.adapter
        if self.closed or not self.settings.enabled or state.blocked or getattr(adapter, "_terminating", False):
            return
        # Before polling starts (initialize/getMe/command registration), AstrBot
        # cannot consume its recovery event. Do not misdiagnose startup as idle.
        updater = getattr(getattr(adapter, "application", None), "updater", None)
        if not getattr(updater, "running", False):
            state.last_success = now
            state.failures = 0
            return
        if state.application is not getattr(adapter, "application", None):
            self.track(adapter)
            return
        event = adapter._polling_recovery_requested
        if now < state.rate_limited_until:
            return
        if state.pending_since is not None:
            if event.is_set() or now - state.pending_since < self.settings.cooldown:
                return
            state.pending_since = None
        if event.is_set() or now - state.last_recovery < self.settings.cooldown:
            return
        # PTB resumes only after RetryAfter (+ its own margin). Give the next
        # ordinary long poll a full grace period instead of restarting at expiry.
        silent = now - max(state.last_success, state.rate_limited_until) >= max(self.settings.timeout, state.request_timeout)
        if state.failures < self.settings.failures and not silent:
            return
        reason = "连续网络失败" if state.failures >= self.settings.failures else "长时间无成功轮询（可能卡死）"
        self.request_recovery(state, now, reason)

    def request_recovery(self, state: _State, now: float, reason: str) -> bool:
        adapter = state.adapter
        event = adapter._polling_recovery_requested
        if (self.closed or not self.settings.enabled or getattr(adapter, "_terminating", False)
                or event.is_set() or now - state.last_recovery < self.settings.cooldown):
            return False
        state.last_recovery = now
        state.pending_since = now
        state.recovered = True
        state.recovery_application = state.application
        event.set()
        self.log("warning", "[tayhetgfix] Telegram[%s] 检测到%s，请求核心重建 Telegram application/HTTP 客户端（冷却 %.0f 秒）",
                 self._label(adapter), reason, self.settings.cooldown)
        return True

    async def _monitor(self, state: _State) -> None:
        try:
            while not self.closed and not getattr(state.adapter, "_terminating", False):
                await asyncio.sleep(self.settings.interval)
                self.check(state, asyncio.get_running_loop().time())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log("error", "[tayhetgfix] Telegram 轮询看门狗退出（%s）", type(exc).__name__)
        finally:
            if state.task is asyncio.current_task():
                state.task = None
            if getattr(state.adapter, "_terminating", False):
                self._detach_bot(state)
                self.states.pop(id(state.adapter), None)

    def cancel(self) -> None:
        self.closed = True
        for state in self.states.values():
            self._detach_bot(state)
            if state.task is not None and not state.task.done():
                task = state.task
                self.retired.add(task)
                task.add_done_callback(self.retired.discard)
                state.task = None
                task.cancel()
        for shared in self.patches.values():
            cls = shared["class"]
            if not shared["owners"] and getattr(cls, "get_updates", None) is shared["wrapper"]:
                if shared["had_own"]:
                    setattr(cls, "get_updates", shared["original"])
                else:
                    delattr(cls, "get_updates")
        self.patches.clear()

    async def close(self) -> None:
        self.cancel()
        if self.retired:
            await asyncio.gather(*list(self.retired), return_exceptions=True)
        self.states.clear()
