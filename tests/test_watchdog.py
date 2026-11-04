"""Deterministic watchdog unit tests; no Telegram requests or AstrBot needed.

Run with ``python -m unittest discover -s tests -p test_watchdog.py -v``.
The fake error hierarchy deliberately keeps BadRequest under NetworkError,
matching PTB, so protocol errors cannot accidentally trigger proxy recovery.
"""
from __future__ import annotations

import asyncio
import sys
import types
import unittest
from collections import deque
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from watchdog import PollingWatchdog, WatchdogSettings  # noqa: E402


class TelegramError(Exception):
    pass


class NetworkError(TelegramError):
    pass


class TimedOut(NetworkError):
    pass


class BadRequest(NetworkError):
    pass


class InvalidToken(TelegramError):
    pass


class Conflict(TelegramError):
    pass


class RetryAfter(TelegramError):
    def __init__(self, retry_after):
        super().__init__("rate limited")
        self.retry_after = retry_after


def fake_telegram_modules():
    package = types.ModuleType("telegram")
    package.__path__ = []
    errors = types.ModuleType("telegram.error")
    for cls in (TelegramError, NetworkError, TimedOut, BadRequest, InvalidToken, Conflict, RetryAfter):
        setattr(errors, cls.__name__, cls)
    package.error = errors
    return {"telegram": package, "telegram.error": errors}


def make_bot_type():
    # A new class per test prevents class monkeypatch state crossing tests.
    class Bot:
        def __init__(self):
            self.outcomes = deque()
            self.calls = []

        async def get_updates(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            outcome = self.outcomes.popleft() if self.outcomes else ()
            if isinstance(outcome, BaseException):
                raise outcome
            if callable(outcome):
                return await outcome()
            return outcome

    return Bot


class Adapter:
    def __init__(self, bot, *, name="telegram", running=True):
        self.config = {"id": name}
        self._terminating = False
        self._polling_recovery_requested = asyncio.Event()
        self.recreate_calls = 0
        self.replace_application(bot, running=running)

    def replace_application(self, bot, *, running=True):
        self.application = types.SimpleNamespace(
            bot=bot, updater=types.SimpleNamespace(running=running)
        )
        self.client = bot

    async def _recreate_application(self):
        # The watchdog must never call this directly: only set the event.
        self.recreate_calls += 1
        raise AssertionError("watchdog must leave rebuilding to the core run loop")


class WatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module_patch = mock.patch.dict(sys.modules, fake_telegram_modules())
        self.module_patch.start()
        self.Bot = make_bot_type()
        self.original_get_updates = self.Bot.get_updates
        self.settings = WatchdogSettings(
            enabled=True, interval=3600.0, timeout=10.0, failures=3, cooldown=60.0
        )
        self.logs = []
        self.managers = []
        self.requests = []
        self.manager = self.new_manager()

    async def asyncTearDown(self):
        try:
            for task in self.requests:
                if not task.done():
                    task.cancel()
            if self.requests:
                await asyncio.gather(*self.requests, return_exceptions=True)
            for manager in reversed(self.managers):
                await manager.close()
        finally:
            self.module_patch.stop()
        self.assertIs(self.Bot.get_updates, self.original_get_updates)

    def new_manager(self, settings=None):
        manager = PollingWatchdog(settings or self.settings, lambda *args: self.logs.append(args))
        self.managers.append(manager)
        return manager

    def track(self, *, bot=None, manager=None, name="telegram", running=True):
        manager = manager or self.manager
        bot = bot or self.Bot()
        adapter = Adapter(bot, name=name, running=running)
        self.assertTrue(manager.track(adapter))
        return adapter, manager.states[id(adapter)]

    def assert_no_recovery(self, adapter):
        self.assertFalse(adapter._polling_recovery_requested.is_set())
        self.assertEqual(adapter.recreate_calls, 0)

    async def test_empty_get_updates_return_is_success_and_clears_failures(self):
        adapter, state = self.track()
        state.last_success = asyncio.get_running_loop().time() - 100
        state.failures = 2
        state.blocked = True
        state.recovered = True
        state.rate_limited_until = asyncio.get_running_loop().time() + 100
        before = asyncio.get_running_loop().time()

        result = await adapter.client.get_updates()

        self.assertEqual(result, ())
        self.assertGreaterEqual(state.last_success, before)
        self.assertEqual(state.failures, 0)
        self.assertFalse(state.blocked)
        self.assertFalse(state.recovered)
        self.assertEqual(state.rate_limited_until, 0)
        self.assertTrue(any("getUpdates" in record[1] for record in self.logs))
        self.assert_no_recovery(adapter)

    async def test_network_failure_threshold_only_sets_one_recovery_event(self):
        adapter, state = self.track()
        now = asyncio.get_running_loop().time()
        for count in range(1, self.settings.failures + 1):
            adapter.client.outcomes.append(NetworkError("proxy down"))
            with self.assertRaises(NetworkError):
                await adapter.client.get_updates()
            self.assertEqual(state.failures, count)
            self.manager.check(state, now)
            self.assertEqual(adapter._polling_recovery_requested.is_set(), count == 3)
        recovery_time = state.last_recovery
        self.manager.check(state, now + 1)
        self.assertEqual(state.last_recovery, recovery_time)
        self.assertEqual(adapter.recreate_calls, 0)

    async def test_silent_polling_timeout_requests_recovery(self):
        adapter, state = self.track()
        now = asyncio.get_running_loop().time()
        state.last_success = now - self.settings.timeout - 1
        self.manager.check(state, now)
        self.assertTrue(adapter._polling_recovery_requested.is_set())
        self.assertTrue(state.recovered)
        self.assertEqual(adapter.recreate_calls, 0)

    async def test_startup_is_not_misdiagnosed_as_stale_polling(self):
        adapter, state = self.track(running=False)
        now = asyncio.get_running_loop().time()
        state.last_success = now - 1000
        self.manager.check(state, now)
        self.assertEqual(state.last_success, now)
        self.assert_no_recovery(adapter)

    async def test_application_rebuild_preserves_adapter_cooldown(self):
        adapter, state = self.track()
        now = asyncio.get_running_loop().time()
        state.failures = self.settings.failures
        self.manager.check(state, now)
        self.assertEqual(state.last_recovery, now)
        adapter._polling_recovery_requested.clear()
        old_bot = state.bot
        adapter.replace_application(self.Bot())
        self.assertTrue(self.manager.track(adapter))
        self.assertIs(self.manager.states[id(adapter)], state)
        self.assertIsNot(state.bot, old_bot)
        self.assertEqual(state.last_recovery, now)
        state.failures = self.settings.failures

        self.manager.check(state, now + self.settings.cooldown - 1)
        self.assert_no_recovery(adapter)
        self.manager.check(state, now + self.settings.cooldown + 1)
        self.assertTrue(adapter._polling_recovery_requested.is_set())
        self.assertEqual(adapter.recreate_calls, 0)

    async def test_old_request_success_cannot_reset_new_application_state(self):
        adapter, state = self.track()
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_success():
            entered.set()
            await release.wait()
            return ()

        old_bot = adapter.client
        old_bot.outcomes.append(delayed_success)
        request = asyncio.create_task(old_bot.get_updates())
        self.requests.append(request)
        await entered.wait()
        adapter.replace_application(self.Bot())
        self.assertTrue(self.manager.track(adapter))
        state.failures = 2
        state.last_success = 123.0
        release.set()
        self.assertEqual(await request, ())
        self.assertEqual(state.failures, 2)
        self.assertEqual(state.last_success, 123.0)
        self.assert_no_recovery(adapter)

    async def test_old_request_failure_cannot_poison_new_application_state(self):
        adapter, state = self.track()
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_failure():
            entered.set()
            await release.wait()
            raise NetworkError("old proxy connection failed")

        adapter.client.outcomes.append(delayed_failure)
        request = asyncio.create_task(adapter.client.get_updates())
        self.requests.append(request)
        await entered.wait()
        adapter.replace_application(self.Bot())
        self.assertTrue(self.manager.track(adapter))
        release.set()
        with self.assertRaises(NetworkError):
            await request
        self.assertEqual(state.failures, 0)
        self.assert_no_recovery(adapter)

    async def test_protocol_errors_block_watchdog_recovery(self):
        for error_type in (BadRequest, InvalidToken, Conflict):
            with self.subTest(error_type=error_type.__name__):
                adapter, state = self.track(name=error_type.__name__)
                state.failures = self.settings.failures
                adapter.client.outcomes.append(error_type("not a proxy fault"))
                with self.assertRaises(error_type):
                    await adapter.client.get_updates()
                self.assertTrue(state.blocked)
                self.manager.check(state, state.last_success + 10000)
                self.assert_no_recovery(adapter)

    async def test_retry_after_waits_without_claiming_a_success(self):
        for retry_after in (45, timedelta(seconds=45)):
            with self.subTest(retry_after=retry_after):
                adapter, state = self.track()
                now = asyncio.get_running_loop().time()
                state.last_success = now - 1000
                previous_success = state.last_success
                state.failures = self.settings.failures
                adapter.client.outcomes.append(RetryAfter(retry_after))
                with self.assertRaises(RetryAfter):
                    await adapter.client.get_updates()
                self.assertEqual(state.last_success, previous_success)
                self.assertEqual(state.failures, 0)
                self.assertGreaterEqual(state.rate_limited_until, now + 45)
                self.manager.check(state, state.rate_limited_until - 0.001)
                self.assert_no_recovery(adapter)
                self.manager.check(state, state.rate_limited_until + 0.001)
                self.assert_no_recovery(adapter)
                polling_grace = max(self.settings.timeout, state.request_timeout)
                self.manager.check(state, state.rate_limited_until + polling_grace + 0.001)
                self.assertTrue(adapter._polling_recovery_requested.is_set())

    async def test_request_cancellation_propagates_without_counting_failure(self):
        adapter, state = self.track()
        entered, inner_cancelled = asyncio.Event(), asyncio.Event()

        async def pending_request():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                inner_cancelled.set()

        adapter.client.outcomes.append(pending_request)
        request = asyncio.create_task(adapter.client.get_updates())
        self.requests.append(request)
        await entered.wait()
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.assertTrue(inner_cancelled.is_set())
        self.assertEqual(state.failures, 0)
        self.assert_no_recovery(adapter)

    async def test_close_cancels_monitor_and_restores_owned_class_method(self):
        adapter, state = self.track()
        task = state.task
        self.assertIsNotNone(task)
        self.assertIsNot(self.Bot.get_updates, self.original_get_updates)
        # Let the monitor enter its try/finally before cancelling it.
        await asyncio.sleep(0)
        await self.manager.close()
        self.assertTrue(task.done())
        self.assertTrue(task.cancelled())
        self.assertEqual(self.manager.states, {})
        self.assertEqual(self.manager.retired, set())
        self.assertIs(self.Bot.get_updates, self.original_get_updates)
        await adapter.client.get_updates()
        self.assert_no_recovery(adapter)

    async def test_multiple_bots_are_isolated_and_untracked_bot_is_untouched(self):
        adapter_a, state_a = self.track(name="A")
        adapter_b, state_b = self.track(name="B")
        untracked = self.Bot()
        for _ in range(self.settings.failures):
            adapter_a.client.outcomes.append(NetworkError("A offline"))
            with self.assertRaises(NetworkError):
                await adapter_a.client.get_updates()
        adapter_b.client.outcomes.append(())
        await adapter_b.client.get_updates()
        untracked.outcomes.append(NetworkError("untracked"))
        with self.assertRaises(NetworkError):
            await untracked.get_updates()
        now = asyncio.get_running_loop().time()
        self.manager.check(state_a, now)
        self.manager.check(state_b, now)
        self.assertTrue(adapter_a._polling_recovery_requested.is_set())
        self.assert_no_recovery(adapter_b)
        self.assertEqual(state_b.failures, 0)
        self.assertEqual(len(self.manager.states), 2)

    async def test_disabling_then_enabling_resumes_same_adapter_monitor(self):
        adapter, state = self.track()
        old_task = state.task
        state.last_success -= 10000
        state.failures = self.settings.failures
        state.blocked = True
        self.manager.configure(replace(self.settings, enabled=False))
        self.assertTrue(self.manager.closed)
        self.assertIs(self.Bot.get_updates, self.original_get_updates)
        self.manager.configure(self.settings)
        self.assertFalse(self.manager.closed)
        self.assertIs(self.manager.states[id(adapter)], state)
        new_task = state.task
        self.assertIsNotNone(new_task)
        self.assertIsNot(new_task, old_task)
        self.assertFalse(state.blocked)
        self.assertEqual(state.failures, 0)
        self.manager.check(state, asyncio.get_running_loop().time())
        self.assert_no_recovery(adapter)
        await asyncio.sleep(0)
        self.assertFalse(new_task.done())
        adapter.client.outcomes.append(NetworkError("observed after re-enable"))
        with self.assertRaises(NetworkError):
            await adapter.client.get_updates()
        self.assertEqual(state.failures, 1)

    async def test_close_then_reenable_can_track_existing_adapter_again(self):
        adapter, _state = self.track()
        await self.manager.close()
        self.assertFalse(self.manager.track(adapter))
        self.manager.configure(self.settings)
        self.assertTrue(self.manager.track(adapter))
        self.assertIsNotNone(self.manager.states[id(adapter)].task)

    async def test_other_manager_keeps_class_patch_when_first_owner_closes(self):
        adapter_a, _state_a = self.track(name="old-owner")
        manager_b = self.new_manager()
        adapter_b, state_b = self.track(manager=manager_b, name="new-owner")
        wrapper = self.Bot.get_updates
        await self.manager.close()
        self.assertIs(self.Bot.get_updates, wrapper)
        await adapter_a.client.get_updates()
        adapter_b.client.outcomes.append(NetworkError("B offline"))
        with self.assertRaises(NetworkError):
            await adapter_b.client.get_updates()
        self.assertEqual(state_b.failures, 1)
        await manager_b.close()
        self.assertIs(self.Bot.get_updates, self.original_get_updates)

    async def test_same_bot_handover_survives_old_owner_close(self):
        adapter, _state = self.track()
        manager_b = self.new_manager()
        self.assertTrue(manager_b.track(adapter))
        new_state = manager_b.states[id(adapter)]
        await self.manager.close()
        self.assertIsNot(self.Bot.get_updates, self.original_get_updates)
        adapter.client.outcomes.append(NetworkError("new owner observes"))
        with self.assertRaises(NetworkError):
            await adapter.client.get_updates()
        self.assertEqual(new_state.failures, 1)

    async def test_close_does_not_restore_over_third_party_replacement(self):
        _adapter, _state = self.track()

        async def external_get_updates(_bot, *args, **kwargs):
            return ("external",)

        self.Bot.get_updates = external_get_updates
        try:
            await self.manager.close()
            self.assertIs(self.Bot.get_updates, external_get_updates)
        finally:
            self.Bot.get_updates = self.original_get_updates

    async def test_long_poll_timedelta_sets_adequate_request_budget(self):
        adapter, state = self.track()
        await adapter.client.get_updates(timeout=timedelta(seconds=120))
        self.assertEqual(state.request_timeout, 150)
        now = asyncio.get_running_loop().time()
        state.last_success = now - 100
        self.manager.check(state, now)
        self.assert_no_recovery(adapter)

    async def test_local_timeout_is_translated_to_ptb_timeout(self):
        adapter, state = self.track()
        adapter.client.outcomes.append(TimeoutError("local deadline"))
        with self.assertRaises(TimedOut):
            await adapter.client.get_updates()
        self.assertEqual(state.failures, 1)
        self.assert_no_recovery(adapter)

    async def test_real_wait_for_deadline_cancels_request_and_translates_timeout(self):
        adapter, state = self.track()
        cancelled = asyncio.Event()

        async def hung_request():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        # The production budget intentionally has a >=30s margin. Speed up only
        # wait_for's timer, preserving its real cancellation semantics.
        real_wait_for = asyncio.wait_for

        async def short_wait_for(awaitable, *, timeout):
            self.assertGreaterEqual(timeout, 30)
            return await real_wait_for(awaitable, timeout=0.001)

        adapter.client.outcomes.append(hung_request)
        with mock.patch("watchdog.asyncio.wait_for", side_effect=short_wait_for):
            with self.assertRaises(TimedOut):
                await adapter.client.get_updates()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(state.failures, 1)
        self.assert_no_recovery(adapter)

    async def test_inherited_get_updates_does_not_leave_subclass_override(self):
        class ChildBot(self.Bot):
            pass

        self.assertNotIn("get_updates", ChildBot.__dict__)
        adapter, state = self.track(bot=ChildBot())
        self.assertIn("get_updates", ChildBot.__dict__)
        self.assertEqual(await adapter.client.get_updates(), ())
        self.assertEqual(state.failures, 0)
        await self.manager.close()
        self.assertNotIn("get_updates", ChildBot.__dict__)
        self.assertIs(ChildBot.get_updates, self.original_get_updates)

    async def test_inherited_patch_shared_by_managers_restores_defining_class(self):
        for parent_manager_closes_first in (True, False):
            with self.subTest(parent_manager_closes_first=parent_manager_closes_first):
                # Use independent types for each close order so leaked base
                # wrappers or subclass overrides cannot hide behind teardown.
                BaseBot = make_bot_type()
                original = BaseBot.get_updates

                class ChildBot(BaseBot):
                    pass

                parent_manager = self.new_manager()
                child_manager = self.new_manager()
                self.track(bot=BaseBot(), manager=parent_manager)
                child_adapter, child_state = self.track(bot=ChildBot(), manager=child_manager)
                self.assertNotIn("get_updates", ChildBot.__dict__)
                self.assertEqual(await child_adapter.client.get_updates(), ())
                self.assertEqual(child_state.failures, 0)
                first, second = (
                    (parent_manager, child_manager)
                    if parent_manager_closes_first
                    else (child_manager, parent_manager)
                )
                await first.close()
                self.assertIsNot(BaseBot.get_updates, original)
                self.assertNotIn("get_updates", ChildBot.__dict__)
                await second.close()
                self.assertIs(BaseBot.get_updates, original)
                self.assertIs(ChildBot.get_updates, original)
                self.assertNotIn("get_updates", ChildBot.__dict__)

    async def test_old_generation_shutdown_success_does_not_confirm_recovery(self):
        adapter, state = self.track()
        old_application = adapter.application
        old_bot = adapter.client
        state.failures = self.settings.failures
        self.manager.check(state, asyncio.get_running_loop().time())
        self.assertTrue(adapter._polling_recovery_requested.is_set())
        self.assertTrue(state.recovered)
        self.logs.clear()

        # PTB stop() issues a final getUpdates with the OLD bot. Its success
        # cannot prove a replacement application/HTTP client has recovered.
        self.assertEqual(await old_bot.get_updates(timeout=0), ())
        self.assertIs(adapter.application, old_application)
        self.assertTrue(state.recovered)
        self.assertFalse(any(record[0] == "info" for record in self.logs))

        adapter.replace_application(self.Bot())
        adapter._polling_recovery_requested.clear()
        self.assertTrue(self.manager.track(adapter))
        self.assertTrue(state.recovered)
        self.assertEqual(await adapter.client.get_updates(), ())
        self.assertFalse(state.recovered)
        self.assertEqual(sum(record[0] == "info" for record in self.logs), 1)
        self.assertEqual(await adapter.client.get_updates(), ())
        self.assertEqual(sum(record[0] == "info" for record in self.logs), 1)

    async def test_monitor_detaches_terminated_adapter(self):
        manager = self.new_manager(replace(self.settings, interval=0.001))
        adapter, state = self.track(manager=manager)
        task = state.task
        adapter._terminating = True
        await task
        self.assertIsNone(state.task)
        self.assertNotIn(id(adapter), manager.states)
        for shared in manager.patches.values():
            self.assertNotIn(id(adapter.client), shared["owners"])
        self.assert_no_recovery(adapter)

    async def test_pending_event_prevents_duplicate_recovery_after_cooldown(self):
        adapter, state = self.track()
        now = asyncio.get_running_loop().time()
        state.failures = self.settings.failures
        self.manager.check(state, now)
        self.assertTrue(adapter._polling_recovery_requested.is_set())
        self.manager.check(state, now + 10 * self.settings.cooldown)
        self.assertEqual(state.last_recovery, now)
        self.assertEqual(state.pending_since, now)

    async def test_success_after_protocol_error_reenables_monitoring(self):
        adapter, state = self.track()
        adapter.client.outcomes.append(Conflict("another poller"))
        with self.assertRaises(Conflict):
            await adapter.client.get_updates()
        self.assertTrue(state.blocked)
        await adapter.client.get_updates()
        self.assertFalse(state.blocked)
        state.failures = self.settings.failures
        self.manager.check(state, asyncio.get_running_loop().time())
        self.assertTrue(adapter._polling_recovery_requested.is_set())

    async def test_unsupported_core_is_skipped_without_patching(self):
        unsupported = types.SimpleNamespace(
            application=types.SimpleNamespace(bot=self.Bot()), _terminating=False
        )
        self.assertFalse(self.manager.track(unsupported))
        self.assertEqual(self.manager.states, {})
        self.assertIs(self.Bot.get_updates, self.original_get_updates)


if __name__ == "__main__":
    unittest.main()
