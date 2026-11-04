"""Offline integration with real PTB; no token, network, or AstrBot installation.

Install python-telegram-bot==22.7 in a test environment, then run:
python -m unittest discover -s tests -p test_ptb_integration.py -v
Tests skip explicitly when PTB is absent; unit tests need only the stdlib.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from telegram.error import NetworkError, TimedOut
    from telegram.ext import Application, ExtBot
    from telegram.request import BaseRequest
    PTB_AVAILABLE = True
except ImportError:
    PTB_AVAILABLE = False
    BaseRequest = object

from watchdog import PollingWatchdog, WatchdogSettings  # noqa: E402


class OfflineRequest(BaseRequest):
    """Feed real Bot/Updater through PTB's normal request/result parsing."""

    def __init__(self):
        self.mode = "ok"
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.polled = asyncio.Event()
        self.cancelled = asyncio.Event()

    @property
    def read_timeout(self):
        return 1.0

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append(endpoint)
        if endpoint == "getMe":
            result = {"id": 12345, "is_bot": True, "first_name": "Offline", "username": "offline_bot"}
        elif endpoint == "getUpdates":
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.polled.set()
            try:
                if self.mode == "hang":
                    await asyncio.Event().wait()
                await asyncio.sleep(0.005)
                if self.mode == "offline":
                    raise NetworkError("simulated proxy offline")
                result = []
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            finally:
                self.active -= 1
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()


@unittest.skipUnless(PTB_AVAILABLE, "optional integration requires python-telegram-bot")
class PTBIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # This imports only test_plugin's fake AstrBot API, not its smoke exercise
        # (which installs fake telegram modules). Telegram here stays real.
        import test_plugin
        self.plugin = test_plugin.plugin
        self.original = ExtBot.get_updates
        self.owned_original = ExtBot.__dict__.get("get_updates")
        self.log = []
        self.manager = PollingWatchdog(
            WatchdogSettings(interval=3600, timeout=120, failures=1, cooldown=120),
            lambda *args: self.log.append(args),
        )
        self.applications = []
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        for application in reversed(self.applications):
            if application.updater.running:
                await application.updater.stop()
            if application.running:
                await application.stop()
            await application.shutdown()
        await self.manager.close()
        self.assertIs(ExtBot.get_updates, self.original)
        self.assertIs(ExtBot.__dict__.get("get_updates"), self.owned_original)
        self.assertFalse(any(t.get_name() == "tayhetgfix-polling-watchdog" for t in asyncio.all_tasks()))

    def application(self):
        api, polling = OfflineRequest(), OfflineRequest()
        application = (Application.builder().token("12345:offline-test-token")
                       .request(api).get_updates_request(polling).build())
        self.applications.append(application)
        return application, api, polling

    async def test_real_updater_recovers_through_single_native_owner(self):
        manager = self.manager
        plugin = self.plugin
        fixture = self

        class Adapter:
            def __init__(self):
                self.config = {"id": "real-ptb"}
                self._terminating = False
                self._polling_recovery_requested = asyncio.Event()
                self.builds = 0
                self._build_application()

            def _build_application(self):
                self.application, self.api, self.polling = fixture.application()
                self.client = self.application.bot
                self.builds += 1
                manager.track(self)

            async def _shutdown_application(self, *, delete_commands=False):
                if self.application.updater.running:
                    await self.application.updater.stop()
                if self.application.running:
                    await self.application.stop()
                await self.application.shutdown()

            async def _recreate_application(self):
                await self._shutdown_application(delete_commands=False)
                self._build_application()
                self._polling_recovery_requested.clear()

        Adapter._shutdown_application = plugin._make_shutdown_wrapper(Adapter._shutdown_application)
        Adapter._build_application = plugin._make_build_wrapper(Adapter._build_application)
        adapter = Adapter()
        state = manager.states[id(adapter)]
        old_application, old_polling = adapter.application, adapter.polling
        old_polling.mode = "offline"
        await old_application.initialize()
        await old_application.start()
        error_observed = asyncio.Event()
        await old_application.updater.start_polling(timeout=0, error_callback=lambda error: error_observed.set())
        # Wait for PTB to consume the injected failure before stopping its loop;
        # this avoids a PTB stop/error same-tick race with an unclaimed task.
        await asyncio.wait_for(error_observed.wait(), timeout=2)
        self.assertGreaterEqual(state.failures, 1)
        manager.check(state, asyncio.get_running_loop().time())
        self.assertTrue(adapter._polling_recovery_requested.is_set())
        last_recovery = state.last_recovery
        old_polling.mode = "ok"  # PTB stop's final cleanup getUpdates succeeds.
        await adapter._recreate_application()  # Same owner that runs core.run()
        self.assertTrue(state.recovered, "old cleanup is not a fresh-client recovery")
        self.assertFalse(any("轮询已恢复" in entry[1] for entry in self.log))
        self.assertEqual(adapter.builds, 2)
        self.assertFalse(old_application.updater.running)
        self.assertEqual(old_polling.active, 0)
        self.assertLessEqual(old_polling.max_active, 1)
        self.assertFalse(adapter._polling_recovery_requested.is_set())
        # A success from the new bot, including [], confirms recovery.
        await adapter.application.initialize()
        await adapter.application.start()
        await adapter.application.updater.start_polling(timeout=0)
        for _ in range(100):
            if not state.recovered:
                break
            await asyncio.sleep(0.01)
        self.assertFalse(state.recovered)
        self.assertEqual(state.failures, 0)
        self.assertTrue(any("轮询已恢复" in entry[1] for entry in self.log))
        self.assertLessEqual(adapter.polling.max_active, 1)
        self.assertEqual(state.last_recovery, last_recovery)
        await adapter._shutdown_application(delete_commands=False)

    async def test_getme_success_does_not_mask_hung_getupdates(self):
        application, api, polling = self.application()
        adapter = types.SimpleNamespace(
            application=application, client=application.bot, _terminating=False,
            _polling_recovery_requested=asyncio.Event(), _recreate_application=lambda: None,
            config={"id": "hung"},
        )
        self.assertTrue(self.manager.track(adapter))
        await application.initialize()
        state = self.manager.states[id(adapter)]
        previous = state.last_success
        await application.bot.get_me()
        self.assertEqual(state.last_success, previous)
        polling.mode = "hang"
        real_wait_for = asyncio.wait_for

        async def fast_deadline(awaitable, *, timeout):
            self.assertGreaterEqual(timeout, 30)
            return await real_wait_for(awaitable, timeout=0.02)

        with mock.patch("watchdog.asyncio.wait_for", side_effect=fast_deadline):
            with self.assertRaises(TimedOut):
                await application.bot.get_updates(timeout=0)
        self.assertTrue(polling.cancelled.is_set())
        self.assertEqual(polling.active, 0)
        self.assertEqual(state.failures, 1)
        self.assertEqual(state.last_success, previous)
        self.assertIn("getMe", api.calls)
        self.assertIn("getUpdates", polling.calls)
        self.assertNotIn("getUpdates", api.calls)

    async def test_live_attach_replaces_captured_callback_via_native_owner(self):
        manager = self.manager
        plugin = self.plugin
        fixture = self

        class Adapter:
            def __init__(self):
                self.config = {"id": "live-attach"}
                self._terminating = False
                self._polling_recovery_requested = asyncio.Event()
                self._build_application()

            def _build_application(self):
                self.application, self.api, self.polling = fixture.application()
                self.client = self.application.bot

            async def _shutdown_application(self, *, delete_commands=False):
                if self.application.updater.running:
                    await self.application.updater.stop()
                if self.application.running:
                    await self.application.stop()
                await self.application.shutdown()

            async def _recreate_application(self):
                await self._shutdown_application(delete_commands=False)
                self._build_application()
                self._polling_recovery_requested.clear()

        adapter = Adapter()
        await adapter.application.initialize()
        await adapter.application.start()
        original_callbacks = []
        await adapter.application.updater.start_polling(
            timeout=0, error_callback=original_callbacks.append
        )

        Adapter.__tg_hardening_record__ = {"runtime": {"enabled": True, "watchdog": manager}}
        Adapter._build_application = plugin._make_build_wrapper(Adapter._build_application)
        Adapter._shutdown_application = plugin._make_shutdown_wrapper(Adapter._shutdown_application)
        plugin_instance = types.SimpleNamespace(
            context=types.SimpleNamespace(get_all_platforms=lambda: [adapter]),
        )
        plugin.TelegramHardening._attach_existing_platforms(plugin_instance, Adapter)
        self.assertTrue(adapter._polling_recovery_requested.is_set())
        self.assertTrue(adapter.application.updater.running)
        self.assertTrue(adapter.application.running)

        await adapter._recreate_application()
        self.assertFalse(original_callbacks, "legacy callback was not modified in place")
        await adapter.application.initialize()
        await adapter.application.start()
        observed_errors = []
        await adapter.application.updater.start_polling(timeout=0, error_callback=observed_errors.append)
        adapter.polling.mode = "offline"
        for _ in range(100):
            if manager.states[id(adapter)].failures:
                break
            await asyncio.sleep(0.01)
        self.assertGreaterEqual(manager.states[id(adapter)].failures, 1)
        for _ in range(100):
            if observed_errors:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(observed_errors, "new polling must use PTB's injected error callback")
        plugin.TelegramHardening._attach_existing_platforms(plugin_instance, Adapter)
        self.assertFalse(adapter._polling_recovery_requested.is_set())

    async def test_close_leaves_core_request_to_its_owner(self):
        application, _api, polling = self.application()
        adapter = types.SimpleNamespace(
            application=application, client=application.bot, _terminating=False,
            _polling_recovery_requested=asyncio.Event(), _recreate_application=lambda: None,
        )
        self.assertTrue(self.manager.track(adapter))
        await application.initialize()
        polling.mode = "hang"
        request = asyncio.create_task(application.bot.get_updates(timeout=0))
        self.tasks.append(request)
        await polling.polled.wait()
        await self.manager.close()
        self.assertFalse(request.done(), "plugin unload must not cancel core polling")
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.assertTrue(polling.cancelled.is_set())
        self.assertEqual(polling.active, 0)


if __name__ == "__main__":
    unittest.main()
