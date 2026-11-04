"""AstrBot lifecycle wrapper tests using local fakes only.

Run with ``python -m unittest discover -s tests -p test_lifecycle.py -v``.
No installed AstrBot, PTB or network connection is required.
"""
from __future__ import annotations

import asyncio
import sys
import types
import unittest
from collections import defaultdict
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for path in (ROOT, TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

# Reuse the existing smoke test's minimal AstrBot API shim for main's import.
import test_plugin as smoke  # noqa: E402
import main as plugin  # noqa: E402


class NetworkError(Exception):
    pass


class TimedOut(NetworkError):
    pass


class BadRequest(NetworkError):
    pass


class InvalidToken(Exception):
    pass


class Conflict(Exception):
    pass


def make_bot_type():
    class Bot:
        async def get_updates(self, *args, **kwargs):
            return ()

    return Bot


class Updater:
    def __init__(self):
        self.running = False
        self.calls = []

    async def start_polling(self, **kwargs):
        self.calls.append(kwargs)


class Application:
    def __init__(self, bot):
        self.bot = bot
        self.updater = Updater()
        self.running = False
        self.handlers = defaultdict(list)

    def add_handler(self, handler, group=0):
        self.handlers[group].append(handler)


def make_adapter_type(bot_type):
    class LifecycleAdapter:
        def __init__(self):
            self.config = {"id": "test-telegram"}
            self._terminating = False
            self._polling_recovery_requested = asyncio.Event()
            self.builds = 0
            self.callbacks = []
            self.shutdowns = 0
            self._build_application()

        def _build_application(self):
            self.application = Application(bot_type())
            self.application.add_handler(object())
            self.client = self.application.bot
            self.builds += 1

        async def _shutdown_application(self, *, delete_commands=False):
            self.shutdowns += 1
            self.application.updater.running = False
            self.application.running = False

        async def _recreate_application(self):
            if self._terminating:
                return
            await self._shutdown_application(delete_commands=False)
            self._build_application()

        async def terminate(self):
            self._terminating = True
            await self._shutdown_application(delete_commands=True)

        async def run(self):
            updater = self.application.updater
            await updater.start_polling(error_callback=self._on_polling_error)

        async def message_handler(self, update, context):
            return "message_handler"

        def _on_polling_error(self, error):
            self.callbacks.append(error)
            return "native-callback"

    return LifecycleAdapter


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.Bot = make_bot_type()
        self.Adapter = make_adapter_type(self.Bot)
        self.original_bot_get_updates = self.Bot.get_updates
        self.tasks = []
        self.instances = []
        self.managers = []
        self.patches = []
        errors = types.ModuleType("telegram.error")
        for error_type in (NetworkError, TimedOut, BadRequest, InvalidToken, Conflict):
            setattr(errors, error_type.__name__, error_type)
        telegram = types.ModuleType("telegram")
        telegram.__path__ = []
        telegram.error = errors
        patcher = mock.patch.dict(sys.modules, {"telegram": telegram, "telegram.error": errors})
        patcher.start()
        self.patches.append(patcher)

    async def asyncTearDown(self):
        try:
            for task in self.tasks:
                if not task.done():
                    task.cancel()
            if self.tasks:
                await asyncio.gather(*self.tasks, return_exceptions=True)
            for instance in reversed(self.instances):
                await instance.terminate()
            record = plugin._record_for(self.Adapter)
            if record:
                manager = record.get("runtime", {}).get("watchdog")
                plugin._unpatch_adapter(self.Adapter)
                if manager is not None:
                    await manager.close()
            for manager in reversed(self.managers):
                await manager.close()
            self.assertIs(self.Bot.get_updates, self.original_bot_get_updates)
        finally:
            for patcher in reversed(self.patches):
                patcher.stop()

    def start_task(self, awaitable):
        task = asyncio.create_task(awaitable)
        self.tasks.append(task)
        return task

    def patch(self, *args, **kwargs):
        patcher = mock.patch(*args, **kwargs)
        self.patches.append(patcher)
        return patcher.start()

    def install_wrappers(self, *, shutdown=None, manager=None):
        originals = {
            "_build_application": self.Adapter._build_application,
            "_shutdown_application": shutdown or self.Adapter._shutdown_application,
            "_on_polling_error": self.Adapter._on_polling_error,
        }
        wrappers = {
            "_build_application": plugin._make_build_wrapper(originals["_build_application"]),
            "_shutdown_application": plugin._make_shutdown_wrapper(originals["_shutdown_application"]),
            "_on_polling_error": plugin._make_polling_error_wrapper(originals["_on_polling_error"]),
        }
        runtime = {"enabled": True}
        if manager is not None:
            runtime["watchdog"] = manager
        setattr(self.Adapter, "__tg_hardening_record__", {
            "originals": originals, "wrappers": wrappers, "runtime": runtime,
            "watchdog_revision": 1,
        })
        for name, wrapper in wrappers.items():
            setattr(self.Adapter, name, wrapper)
        return wrappers

    def manager(self):
        from watchdog import PollingWatchdog, WatchdogSettings
        manager = PollingWatchdog(WatchdogSettings(interval=3600), lambda *args: None)
        self.managers.append(manager)
        return manager

    def fake_imports(self):
        package = types.ModuleType(plugin.TELEGRAM_PACKAGE)
        package.__path__ = []
        adapter_module = types.ModuleType(plugin.ADAPTER_MODULE)
        adapter_module.TelegramPlatformAdapter = self.Adapter
        patcher = mock.patch.dict(sys.modules, {
            plugin.TELEGRAM_PACKAGE: package,
            plugin.ADAPTER_MODULE: adapter_module,
        })
        patcher.start()
        self.patches.append(patcher)
        # Avoid touching HTTPX's process-wide constructor in these unit tests.
        self.patch("main._configure_httpx_proxy", return_value=False)
        self.patch("main._restore_httpx_proxy_patch", return_value=None)
        return package

    def new_plugin(self, context=None, **config):
        options = {"proxy_enabled": False, "watchdog_interval": 3600}
        options.update(config)
        instance = plugin.TelegramHardening(context or types.SimpleNamespace(), options)
        self.instances.append(instance)
        return instance

    async def test_shutdown_calls_are_serialized_per_adapter(self):
        first_entered, release = asyncio.Event(), asyncio.Event()
        active = 0
        maximum = 0
        calls = 0

        async def shutdown(adapter, *, delete_commands=False):
            nonlocal active, maximum, calls
            calls += 1
            active += 1
            maximum = max(maximum, active)
            if calls == 1:
                first_entered.set()
                await release.wait()
            await asyncio.sleep(0)
            adapter.application.running = False
            adapter.application.updater.running = False
            active -= 1

        adapter = self.Adapter()
        self.install_wrappers(shutdown=shutdown)
        task_a = self.start_task(adapter._shutdown_application(delete_commands=False))
        await first_entered.wait()
        task_b = self.start_task(adapter._shutdown_application(delete_commands=True))
        await asyncio.sleep(0)
        self.assertEqual(calls, 1)
        self.assertTrue(getattr(adapter, "__tayhetgfix_shutdown_pending__"))
        with self.assertRaises(plugin._PatchError):
            adapter._build_application()
        release.set()
        await asyncio.gather(task_a, task_b)
        self.assertEqual(calls, 2)
        self.assertEqual(maximum, 1)
        self.assertFalse(getattr(adapter, "__tayhetgfix_shutdown_pending__"))
        adapter._build_application()
        self.assertEqual(adapter.builds, 2)

    async def test_shutdown_exception_keeps_pending_and_blocks_build(self):
        async def shutdown(adapter, **kwargs):
            raise RuntimeError("stop failed")

        adapter = self.Adapter()
        self.install_wrappers(shutdown=shutdown)
        with self.assertRaisesRegex(RuntimeError, "stop failed"):
            await adapter._shutdown_application(delete_commands=False)
        self.assertTrue(getattr(adapter, "__tayhetgfix_shutdown_pending__"))
        with self.assertRaises(plugin._PatchError):
            adapter._build_application()
        self.assertEqual(adapter.builds, 1)

    async def test_shutdown_cancellation_keeps_pending_and_blocks_build(self):
        entered = asyncio.Event()

        async def shutdown(adapter, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        adapter = self.Adapter()
        self.install_wrappers(shutdown=shutdown)
        task = self.start_task(adapter._shutdown_application(delete_commands=False))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(getattr(adapter, "__tayhetgfix_shutdown_pending__"))
        with self.assertRaises(plugin._PatchError):
            adapter._build_application()
        self.assertEqual(adapter.builds, 1)

    async def test_swallowed_stop_failure_with_pending_polling_task_blocks_build(self):
        adapter = self.Adapter()
        polling_task = self.start_task(asyncio.Event().wait())
        setattr(adapter.application.updater, "_Updater__polling_task", polling_task)

        async def swallowing_shutdown(adapter, **kwargs):
            # Mirrors PTB stop clearing running before task settlement and core
            # suppressing the failure; even losing the attribute must not hide it.
            adapter.application.updater.running = False
            adapter.application.running = False
            setattr(adapter.application.updater, "_Updater__polling_task", None)

        self.install_wrappers(shutdown=swallowing_shutdown)
        with self.assertRaisesRegex(plugin._PatchError, "shutdown did not finish"):
            await adapter._shutdown_application(delete_commands=False)
        self.assertTrue(getattr(adapter, "__tayhetgfix_shutdown_pending__"))
        with self.assertRaises(plugin._PatchError):
            adapter._build_application()
        self.assertEqual(adapter.builds, 1)

    async def test_settled_polling_task_allows_build_after_shutdown(self):
        adapter = self.Adapter()
        polling_task = self.start_task(asyncio.sleep(0))
        await polling_task
        setattr(adapter.application.updater, "_Updater__polling_task", polling_task)
        self.install_wrappers()
        await adapter._shutdown_application(delete_commands=False)
        adapter._build_application()
        self.assertEqual(adapter.builds, 2)

    async def test_terminate_racing_recreate_cannot_build_after_shutdown(self):
        entered, release = asyncio.Event(), asyncio.Event()
        shutdown_calls = 0

        async def shutdown(adapter, **kwargs):
            nonlocal shutdown_calls
            shutdown_calls += 1
            if shutdown_calls == 1:
                entered.set()
                await release.wait()
            adapter.application.updater.running = False
            adapter.application.running = False

        adapter = self.Adapter()
        self.install_wrappers(shutdown=shutdown)
        recreate = self.start_task(adapter._recreate_application())
        await entered.wait()
        terminate = self.start_task(adapter.terminate())
        await asyncio.sleep(0)
        self.assertTrue(adapter._terminating)
        release.set()
        await asyncio.gather(recreate, terminate)
        self.assertEqual(adapter.builds, 1)
        self.assertEqual(shutdown_calls, 2)

    async def test_safe_rebuild_does_not_build_when_shutdown_raises(self):
        adapter = self.Adapter()

        async def shutdown(**kwargs):
            raise RuntimeError("shutdown cannot finish")

        adapter._shutdown_application = shutdown
        with self.assertRaisesRegex(RuntimeError, "shutdown cannot finish"):
            await plugin._safe_rebuild(adapter)
        self.assertEqual(adapter.builds, 1)

    async def test_safe_rebuild_does_not_build_when_shutdown_is_cancelled(self):
        adapter = self.Adapter()
        entered = asyncio.Event()

        async def shutdown(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        adapter._shutdown_application = shutdown
        task = self.start_task(plugin._safe_rebuild(adapter))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(adapter.builds, 1)

    async def test_run_wrapper_stops_when_rebuild_shutdown_fails(self):
        adapter = self.Adapter()
        calls = 0

        async def shutdown(adapter, **kwargs):
            raise RuntimeError("old polling cannot stop")

        self.install_wrappers(shutdown=shutdown)
        runtime = plugin._runtime_for(adapter)
        runtime.update(max_restart=3, restart_delay=0)

        async def original_run(adapter):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("run crashed")
            return "unsafe reentry"

        wrapped_run = plugin._make_run_wrapper(original_run)
        with self.assertRaisesRegex(RuntimeError, "old polling cannot stop"):
            await wrapped_run(adapter)
        self.assertEqual(calls, 1, "failed shutdown must not re-enter native run on old application")
        self.assertEqual(adapter.builds, 1)

    async def test_shutdown_still_running_blocks_build_without_polling_task(self):
        adapter = self.Adapter()
        adapter.application.running = True

        async def swallowing_shutdown(adapter, **kwargs):
            return None

        self.install_wrappers(shutdown=swallowing_shutdown)
        with self.assertRaisesRegex(plugin._PatchError, "shutdown did not finish"):
            await adapter._shutdown_application(delete_commands=False)
        self.assertTrue(getattr(adapter, "__tayhetgfix_shutdown_pending__"))
        with self.assertRaises(plugin._PatchError):
            adapter._build_application()
        self.assertEqual(adapter.builds, 1)

    async def test_successful_shutdown_retry_can_clear_previous_pending(self):
        adapter = self.Adapter()
        calls = 0

        async def shutdown(adapter, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("first stop failed")

        self.install_wrappers(shutdown=shutdown)
        with self.assertRaises(RuntimeError):
            await adapter._shutdown_application(delete_commands=False)
        self.assertTrue(getattr(adapter, "__tayhetgfix_shutdown_pending__"))
        await adapter._shutdown_application(delete_commands=False)
        self.assertFalse(getattr(adapter, "__tayhetgfix_shutdown_pending__"))
        adapter._build_application()
        self.assertEqual(adapter.builds, 2)

    async def test_safe_rebuild_successfully_shuts_down_before_build(self):
        adapter = self.Adapter()
        self.install_wrappers()
        adapter.application.running = True
        adapter.application.updater.running = True
        old_application = adapter.application
        await plugin._safe_rebuild(adapter)
        self.assertEqual(adapter.shutdowns, 1)
        self.assertEqual(adapter.builds, 2)
        self.assertIsNot(adapter.application, old_application)
        self.assertFalse(old_application.running)
        self.assertFalse(old_application.updater.running)

    async def test_safe_rebuild_accepts_legacy_shutdown_without_keywords(self):
        adapter = self.Adapter()
        calls = []

        async def shutdown():
            calls.append("closed")

        adapter._shutdown_application = shutdown
        await plugin._safe_rebuild(adapter)
        self.assertEqual(calls, ["closed"])
        self.assertEqual(adapter.builds, 2)

    async def test_safe_rebuild_checks_terminating_after_await(self):
        adapter = self.Adapter()

        async def shutdown(**kwargs):
            await asyncio.sleep(0)
            adapter._terminating = True

        adapter._shutdown_application = shutdown
        await plugin._safe_rebuild(adapter)
        self.assertEqual(adapter.builds, 1)

    async def test_active_application_or_updater_refuses_build(self):
        adapter = self.Adapter()
        self.install_wrappers()
        for attribute in ("application", "updater"):
            with self.subTest(attribute=attribute):
                target = adapter.application if attribute == "application" else adapter.application.updater
                target.running = True
                with self.assertRaises(plugin._PatchError):
                    adapter._build_application()
                self.assertEqual(adapter.builds, 1)
                target.running = False

    async def test_terminating_build_is_noop(self):
        adapter = self.Adapter()
        self.install_wrappers()
        adapter._terminating = True
        self.assertIsNone(adapter._build_application())
        self.assertEqual(adapter.builds, 1)

    async def test_network_callback_suppression_requires_registered_active_manager(self):
        adapter = self.Adapter()
        manager = self.manager()
        self.install_wrappers(manager=manager)
        # Unregistered adapters retain native diagnostics/counters.
        self.assertEqual(adapter._on_polling_error(NetworkError("offline")), "native-callback")
        self.assertEqual(len(adapter.callbacks), 1)
        self.assertTrue(manager.track(adapter))
        for error in (NetworkError("offline"), TimedOut("timeout"), BadRequest("bad request")):
            self.assertIsNone(adapter._on_polling_error(error))
        self.assertEqual(len(adapter.callbacks), 1)
        for error in (InvalidToken("token"), Conflict("duplicate")):
            self.assertEqual(adapter._on_polling_error(error), "native-callback")
        self.assertEqual(len(adapter.callbacks), 3)
        manager.cancel()
        self.assertEqual(adapter._on_polling_error(NetworkError("manager closed")), "native-callback")
        self.assertEqual(len(adapter.callbacks), 4)

    async def test_bad_request_cannot_trigger_native_network_error_counter(self):
        adapter = self.Adapter()
        manager = self.manager()
        self.install_wrappers(manager=manager)
        self.assertTrue(manager.track(adapter))
        state = manager.states[id(adapter)]
        adapter.application.updater.running = True
        native_failures = []

        def native_counter(adapter, error):
            if isinstance(error, NetworkError):
                native_failures.append(error)
                if len(native_failures) >= 3:
                    adapter._polling_recovery_requested.set()

        self.Adapter._on_polling_error = plugin._make_polling_error_wrapper(native_counter)
        for _ in range(4):
            error = BadRequest("invalid getUpdates arguments")
            # This is the real ordering: get_updates instrumentation observes
            # failure first, then PTB invokes the native polling callback.
            manager._failure(state, error)
            adapter._on_polling_error(error)
            manager.check(state, asyncio.get_running_loop().time() + 10000)
        self.assertTrue(state.blocked)
        self.assertEqual(state.failures, 0)
        self.assertEqual(native_failures, [])
        self.assertFalse(adapter._polling_recovery_requested.is_set())

    async def test_same_name_error_without_network_mro_is_not_suppressed(self):
        adapter = self.Adapter()
        manager = self.manager()
        self.install_wrappers(manager=manager)
        self.assertTrue(manager.track(adapter))
        unrelated_timeout = type("TimedOut", (Exception,), {})("not PTB network error")
        self.assertEqual(adapter._on_polling_error(unrelated_timeout), "native-callback")
        self.assertEqual(adapter.callbacks, [unrelated_timeout])

    async def test_existing_platform_is_attached_without_rebuild(self):
        self.fake_imports()
        adapter = self.Adapter()
        before = adapter.application
        context = types.SimpleNamespace(get_all_platforms=lambda: [object(), adapter])
        instance = self.new_plugin(context)
        await instance.initialize()
        self.assertEqual(instance.status, "applied")
        manager = plugin._runtime_for(self.Adapter)["watchdog"]
        self.assertIn(id(adapter), manager.states)
        self.assertIs(adapter.application, before)
        self.assertEqual(adapter.builds, 1)
        self.assertIsNotNone(manager.states[id(adapter)].task)
        self.assertIsNot(self.Bot.get_updates, self.original_bot_get_updates)

    async def test_live_attach_requests_one_native_rebuild_for_captured_callback(self):
        self.fake_imports()
        adapter = self.Adapter()
        captured_callback = adapter._on_polling_error
        adapter.application.updater.running = True
        adapter.application.running = True
        instance = self.new_plugin(types.SimpleNamespace(get_all_platforms=lambda: [adapter]))
        await instance.initialize()
        manager = plugin._runtime_for(self.Adapter)["watchdog"]
        state = manager.states[id(adapter)]
        self.assertTrue(adapter._polling_recovery_requested.is_set())
        requested = state.last_recovery
        self.assertIsNot(captured_callback.__func__, self.Adapter._on_polling_error)
        await instance.initialize()
        self.assertEqual(state.last_recovery, requested)
        self.assertEqual(adapter.builds, 1, "plugin never rebuilds concurrently itself")
        await adapter._recreate_application()
        adapter._polling_recovery_requested.clear()
        adapter.application.updater.running = True
        adapter.application.running = True
        self.assertIs(adapter.__tayhetgfix_built_application__, adapter.application)
        self.assertIs(adapter.__tayhetgfix_built_callback__, self.Adapter._on_polling_error)
        instance._attach_existing_platforms(self.Adapter)
        self.assertFalse(adapter._polling_recovery_requested.is_set())
        self.assertEqual(adapter.builds, 2)

    async def test_disabled_watchdog_does_not_request_live_attach_rebuild(self):
        self.fake_imports()
        adapter = self.Adapter()
        adapter.application.updater.running = True
        instance = self.new_plugin(types.SimpleNamespace(get_all_platforms=lambda: [adapter]),
                                   watchdog_enabled=False)
        await instance.initialize()
        self.assertFalse(adapter._polling_recovery_requested.is_set())
        self.assertEqual(adapter.builds, 1)

    async def test_duplicate_initialize_reuses_manager_and_wrapper_identities(self):
        self.fake_imports()
        adapter = self.Adapter()
        instance = self.new_plugin(types.SimpleNamespace(get_all_platforms=lambda: [adapter]))
        await instance.initialize()
        record = plugin._record_for(self.Adapter)
        manager = record["runtime"]["watchdog"]
        wrappers = dict(record["wrappers"])
        monitor = manager.states[id(adapter)].task
        await instance.initialize()
        self.assertEqual(instance.status, "already_applied")
        refreshed = plugin._record_for(self.Adapter)
        self.assertIs(refreshed["runtime"]["watchdog"], manager)
        self.assertIs(manager.states[id(adapter)].task, monitor)
        for name, wrapper in wrappers.items():
            self.assertIs(getattr(self.Adapter, name), wrapper)

    async def test_old_plugin_terminate_cannot_close_current_owners_manager(self):
        package = self.fake_imports()
        adapter = self.Adapter()
        context = types.SimpleNamespace(get_all_platforms=lambda: [adapter])
        old_plugin = self.new_plugin(context)
        await old_plugin.initialize()
        old_manager = plugin._runtime_for(self.Adapter)["watchdog"]
        new_plugin = self.new_plugin(context)
        await new_plugin.initialize()
        current_manager = plugin._runtime_for(self.Adapter)["watchdog"]
        self.assertIs(current_manager, old_manager)
        monitor = current_manager.states[id(adapter)].task
        await old_plugin.terminate()
        self.assertFalse(current_manager.closed)
        self.assertFalse(monitor.done())
        self.assertTrue(getattr(self.Adapter, "__tg_hardened__"))
        self.assertIs(getattr(self.Adapter, "__tayhetgfix_adapter_owner__"), new_plugin._proxy_owner)
        self.assertIs(getattr(package, "__tayhetgfix_registry_state__")["owner"], new_plugin._proxy_owner)

    async def test_current_terminate_awaits_monitor_and_restores_methods(self):
        package = self.fake_imports()
        adapter = self.Adapter()
        original_build = self.Adapter._build_application
        instance = self.new_plugin(types.SimpleNamespace(get_all_platforms=lambda: [adapter]))
        await instance.initialize()
        manager = plugin._runtime_for(self.Adapter)["watchdog"]
        task = manager.states[id(adapter)].task
        await asyncio.sleep(0)
        await instance.terminate()
        self.assertTrue(manager.closed)
        self.assertEqual(manager.states, {})
        self.assertEqual(manager.retired, set())
        self.assertTrue(task.done())
        self.assertTrue(task.cancelled())
        self.assertIs(self.Adapter._build_application, original_build)
        self.assertIs(self.Bot.get_updates, self.original_bot_get_updates)
        self.assertIsNone(plugin._record_for(self.Adapter))
        self.assertFalse(hasattr(package, "__tayhetgfix_registry_state__"))
        self.assertEqual(instance.status, "terminated")


if __name__ == "__main__":
    unittest.main()
