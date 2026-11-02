from __future__ import annotations

import asyncio
import importlib
import sys
import types
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class FakeLogger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def install_fake_astrbot_api():
    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    astrbot.__version__ = "4.28.0"
    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    api.logger = FakeLogger()
    star_module = types.ModuleType("astrbot.api.star")

    class FakeContext:
        pass

    class FakeStar:
        def __init__(self, context):
            self.context = context

    def fake_register(*_args, **_kwargs):
        return lambda cls: cls

    star_module.Context = FakeContext
    star_module.Star = FakeStar
    star_module.register = fake_register
    astrbot.api = api
    api.star = star_module
    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.star": star_module,
    })


install_fake_astrbot_api()
import main as plugin  # noqa: E402


class Handler:
    pass


class FakeComponent:
    def __init__(self, *args, **kwargs):
        if args:
            kwargs.setdefault("text", args[0])
        self.__dict__.update(kwargs)


class FakeComp:
    class Record(FakeComponent):
        kind = "record"

    class Image(FakeComponent):
        kind = "image"

    class File(FakeComponent):
        kind = "file"

    class Video(FakeComponent):
        kind = "video"

    class Plain(FakeComponent):
        kind = "plain"

    class At(FakeComponent):
        kind = "at"


Comp = FakeComp


class FakeMedia:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    async def get_file(self):
        raise RuntimeError("simulated Telegram download failure")


class FakeMessage:
    def __init__(self, **kwargs):
        self.chat = types.SimpleNamespace(id=123, type="private")
        self.message_id = 1
        self.from_user = types.SimpleNamespace(id=7, username="tester")
        self.caption = None
        self.caption_entities = []
        self.api_kwargs = {}
        self.__dict__.update(kwargs)

    def to_dict(self):
        data = dict(self.__dict__)
        data.pop("api_kwargs", None)
        data.update(self.api_kwargs)
        return data


class FakeConverted:
    def __init__(self):
        self.message = []
        self.message_str = ""


class FakeClient:
    def __init__(self):
        self.calls = []

    async def _post(self, endpoint, data):
        self.calls.append((endpoint, data))
        return {"ok": True, "result": {"endpoint": endpoint}}


class FakeUpdater:
    def __init__(self) -> None:
        self.running = False
        self.allowed_updates = None
        self.last_kwargs = None
        self.calls = 0

    async def start_polling(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        self.allowed_updates = kwargs.get("allowed_updates")
        self.running = False


class FakeApplication:
    def __init__(self) -> None:
        self.updater = FakeUpdater()
        self.bot_data = {}
        self.handlers = defaultdict(list)

    def add_handler(self, handler, group=0):
        self.handlers[group].append(handler)


class FakeAdapter:
    instances = []

    def __init__(self, *_args, **_kwargs):
        self.application = None
        self.client = FakeClient()
        self._terminating = False
        self.crash_once = True
        self.builds = 0
        self._build_application()
        type(self).instances.append(self)

    def _build_application(self):
        # Anchors deliberately mirror the AstrBot method shape.
        self.application = FakeApplication()
        self.application.add_handler(object())
        self.updater = self.application.updater
        self.builds += 1

    async def _shutdown_application(self, delete_commands=False):
        self.application = None

    async def run(self):
        updater = self.application.updater
        await updater.start_polling(error_callback=None)
        if self.crash_once:
            self.crash_once = False
            raise RuntimeError("simulated polling crash")

    async def message_handler(self, update, context):
        raise RuntimeError("simulated message failure")

    async def convert_message(self, update, context, get_reply=True):
        if getattr(update.message, "voice", None):
            await update.message.voice.get_file()
        return FakeConverted()


class FakeHTTPXRequest:
    """Stands in for telegram.request.HTTPXRequest to observe injected kwargs."""

    created: list = []

    def __init__(
        self,
        connection_pool_size=256,
        read_timeout=5.0,
        write_timeout=5.0,
        connect_timeout=5.0,
        pool_timeout=1.0,
        http_version="1.1",
        socket_options=None,
        proxy=None,
        media_write_timeout=20.0,
        httpx_kwargs=None,
    ) -> None:
        self.proxy = proxy
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        type(self).created.append(self)


class FakeLegacyHTTPXRequest:
    """PTB variant exposing the older proxy_url keyword."""

    def __init__(self, proxy_url=None, connect_timeout=5.0, read_timeout=5.0):
        self.proxy_url = proxy_url
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout


def install_fake_astrbot():
    astrbot = types.ModuleType("astrbot")
    astrbot.__version__ = "4.28.0"
    core = types.ModuleType("astrbot.core")
    platform = types.ModuleType("astrbot.core.platform")
    sources = types.ModuleType("astrbot.core.platform.sources")
    telegram = types.ModuleType("astrbot.core.platform.sources.telegram")
    adapter_module = types.ModuleType("astrbot.core.platform.sources.telegram.tg_adapter")
    adapter_module.TelegramPlatformAdapter = FakeAdapter

    # The proxy patch reaches into the real PTB package via importlib, so the
    # package and its request module must both be importable here.
    telegram_pkg = types.ModuleType("telegram")
    request_module = types.ModuleType("telegram.request")
    request_module.HTTPXRequest = FakeHTTPXRequest
    telegram_pkg.request = request_module

    astrbot.core = core
    core.platform = platform
    platform.sources = sources
    sources.telegram = telegram
    telegram.tg_adapter = adapter_module
    for module in (astrbot, core, platform, sources, telegram, adapter_module, telegram_pkg, request_module):
        sys.modules[module.__name__] = module


async def exercise():
    global plugin
    install_fake_astrbot()
    handler = Handler()
    plugin.register_telegram_handler(handler, group=120)
    instance = plugin.TelegramHardening(
        types.SimpleNamespace(),
        {
            "allowed_updates": "message, callback_query",
            "max_restart": 2,
            "restart_delay": 0,
            "proxy": "http://172.18.0.1:7890",
            "proxy_connect_timeout": 12,
            "proxy_read_timeout": 15,
        },
    )
    await instance.initialize()
    assert instance.status == "applied"
    assert getattr(FakeAdapter, "__tg_hardened__", False)

    # The proxy must reach PTB's transport even though no PTB request object is
    # constructed by the fake adapter: the patch lives on HTTPXRequest.__init__.
    FakeHTTPXRequest.created.clear()
    injected = FakeHTTPXRequest()
    assert injected.proxy == "http://172.18.0.1:7890", "proxy must be injected explicitly"
    assert injected.connect_timeout == 12
    assert injected.read_timeout == 15

    # An explicitly supplied proxy must never be overridden.
    explicit = FakeHTTPXRequest(proxy="socks5://127.0.0.1:1080")
    assert explicit.proxy == "socks5://127.0.0.1:1080"
    direct = FakeHTTPXRequest(proxy=None)
    assert direct.proxy is None, "an explicit None must opt out of global proxy injection"
    empty = FakeHTTPXRequest(proxy="")
    assert empty.proxy == "", "an explicit empty proxy must be preserved"

    # Positional construction must not produce a duplicate proxy keyword.
    positional = FakeHTTPXRequest(256, 5.0, 5.0, 5.0, 1.0, "1.1", None, "http://pos:1")
    assert positional.proxy == "http://pos:1"

    # A partially positional call must not inject over an already-positional
    # read_timeout/connect_timeout (would raise "got multiple values").
    partial = FakeHTTPXRequest(256, 9.0, 5.0, 9.0)
    assert partial.proxy == "http://172.18.0.1:7890"
    assert partial.read_timeout == 9.0, "an explicitly positional read_timeout must survive"
    assert partial.connect_timeout == 9.0, "an explicitly positional connect_timeout must survive"

    assert plugin._proxy_log_target("http://user:secret@proxy.example:7890") == "http://proxy.example:7890"
    assert plugin._proxy_log_target("socks5://name:secret@[2001:db8::1]:1080") == "socks5://[2001:db8::1]:1080"

    legacy_module = types.ModuleType("telegram.request")
    legacy_module.HTTPXRequest = FakeLegacyHTTPXRequest
    sys.modules["telegram.request"] = legacy_module
    assert plugin._install_httpx_proxy_patch("http://proxy:7890", 12, 15)
    legacy = FakeLegacyHTTPXRequest()
    assert legacy.proxy_url == "http://proxy:7890"
    assert legacy.connect_timeout == 12
    assert legacy.read_timeout == 15
    plugin._restore_httpx_proxy_patch()
    assert FakeLegacyHTTPXRequest().proxy_url is None
    request_module = sys.modules["telegram.request"]
    request_module.HTTPXRequest = FakeHTTPXRequest

    # A module reload must recover both the active patch state and original init.
    assert plugin._install_httpx_proxy_patch("http://proxy:7890", 12, 15)
    reloaded_plugin = importlib.reload(plugin)
    assert reloaded_plugin._install_httpx_proxy_patch("http://proxy:8080", 7, 9)
    assert FakeHTTPXRequest().proxy == "http://proxy:8080"
    reloaded_plugin._restore_httpx_proxy_patch()
    assert not getattr(
        FakeHTTPXRequest.__dict__.get("__init__"), "__tg_hardening_proxy_patch__", False
    )
    assert FakeHTTPXRequest().proxy is None
    plugin = reloaded_plugin

    adapter = FakeAdapter({}, {}, None)
    assert adapter.application.updater.allowed_updates is None
    assert handler in adapter.application.handlers[120]

    default_updater = FakeUpdater()
    assert plugin._install_polling_guard(
        types.SimpleNamespace(application=types.SimpleNamespace(updater=default_updater)), None
    )
    await default_updater.start_polling(allowed_updates=["chat_member"])
    assert default_updater.last_kwargs["allowed_updates"] == ["chat_member"]
    configured_updater = FakeUpdater()
    assert plugin._install_polling_guard(
        types.SimpleNamespace(application=types.SimpleNamespace(updater=configured_updater)),
        ("message", "callback_query"),
    )
    await configured_updater.start_polling(allowed_updates=["chat_member"])
    assert configured_updater.last_kwargs["allowed_updates"] == ["message", "callback_query"]

    await adapter.run()
    assert adapter.builds == 2, "the outer run wrapper should rebuild once after the crash"
    assert adapter.application.updater.allowed_updates == ["message", "callback_query"]
    assert adapter.application.updater.calls == 1

    # Message errors are swallowed by the final safety wrapper.
    assert await adapter.message_handler(None, None) is None

    # Media failures retry the untouched converter with that media branch
    # hidden, then append the same kind of empty placeholder as the old patch.
    voice_update = types.SimpleNamespace(message=FakeMessage(voice=FakeMedia()))
    converted = await adapter.convert_message(voice_update, None)
    assert converted.message and converted.message[0].kind == "record"
    assert converted.raw_message is voice_update

    # Some upstream versions return normally but omit a component when
    # Telegram returns file_path=None; the wrapper fills that gap too.
    document_update = types.SimpleNamespace(
        message=FakeMessage(document=FakeMedia(file_name="large.bin"))
    )
    document_converted = await adapter.convert_message(document_update, None)
    assert document_converted.message and document_converted.message[0].kind == "file"

    # Rich-message fallback handles the real PTB shape: string text, nested
    # blocks, and table cells containing a nested {text: ...} object.
    rich_update = types.SimpleNamespace(
        message=FakeMessage(
            api_kwargs={
                "rich_message": {
                    "blocks": [
                        {"type": "heading", "text": "明日课表"},
                        {"type": "table", "cells": [[{"text": "时间"}, {"text": {"type": "bold", "text": "课程"}}]]},
                    ]
                }
            }
        )
    )
    rich_converted = await adapter.convert_message(rich_update, None)
    assert "明日课表" in rich_converted.message_str
    assert "时间 | 课程" in rich_converted.message_str

    # The outbound custom endpoint is injected only when the core does not
    # already expose send_rich_message.
    result = await adapter.send_rich_message(-100, "<table><tr><td>ok</td></tr></table>")
    assert result["ok"] is True
    endpoint, payload = adapter.client.calls[-1]
    assert endpoint == "sendRichMessage"
    assert payload["chat_id"] == -100
    assert payload["rich_message"]["html"].startswith("<table>")

    # A replacement plugin instance takes over global patch ownership. Terminating
    # the old instance must not remove the replacement's adapter or proxy patches.
    replacement = plugin.TelegramHardening(
        types.SimpleNamespace(),
        {
            "proxy": "http://replacement:7890",
            "proxy_connect_timeout": 6,
            "proxy_read_timeout": 8,
        },
    )
    await replacement.initialize()
    assert replacement.status == "already_applied"
    assert FakeHTTPXRequest().proxy == "http://replacement:7890"
    await instance.terminate()
    assert getattr(FakeAdapter, "__tg_hardened__", False)
    still_active = FakeHTTPXRequest()
    assert still_active.proxy == "http://replacement:7890"
    assert still_active.connect_timeout == 6
    assert still_active.read_timeout == 8

    # A failed, not-yet-active replacement must leave the current owner untouched.
    failed_replacement = plugin.TelegramHardening(types.SimpleNamespace(), {"proxy": ""})
    original_anchor_check = plugin._anchor_check
    plugin._anchor_check = lambda _cls: (_ for _ in ()).throw(RuntimeError("replacement anchor failure"))
    try:
        await failed_replacement.initialize()
    finally:
        plugin._anchor_check = original_anchor_check
    assert failed_replacement.status == "skipped_anchor"
    assert getattr(FakeAdapter, "__tg_hardened__", False)
    assert FakeHTTPXRequest().proxy == "http://replacement:7890"

    inactive = plugin.TelegramHardening(types.SimpleNamespace(), {"enabled": False})
    await inactive.initialize()
    await inactive.terminate()
    assert getattr(FakeAdapter, "__tg_hardened__", False)
    assert FakeHTTPXRequest().proxy == "http://replacement:7890"

    # Refreshing the active instance with proxy disabled clears the old proxy;
    # terminating an inactive/older owner cannot alter another instance's state.
    replacement.config = {"proxy": "http://stale:7890", "proxy_enabled": False}
    await replacement.initialize()
    assert not getattr(
        FakeHTTPXRequest.__dict__.get("__init__"), "__tg_hardening_proxy_patch__", False
    )
    assert FakeHTTPXRequest().proxy is None
    await instance.terminate()
    assert getattr(FakeAdapter, "__tg_hardened__", False)

    # Re-enable with a proxy, then clear the proxy value while still enabled.
    replacement.config = {"proxy": "http://re-enabled:7890"}
    await replacement.initialize()
    assert FakeHTTPXRequest().proxy == "http://re-enabled:7890"
    replacement.config = {"proxy": ""}
    await replacement.initialize()
    assert not getattr(
        FakeHTTPXRequest.__dict__.get("__init__"), "__tg_hardening_proxy_patch__", False
    )
    assert FakeHTTPXRequest().proxy is None

    await replacement.terminate()
    assert not getattr(FakeAdapter, "__tg_hardened__", False)
    assert FakeAdapter._build_application.__name__ == "_build_application"
    assert not hasattr(FakeAdapter, "send_rich_message")

    # An anchor failure on a previously active instance must release only that
    # instance's adapter, package-registry, and proxy state.
    replacement.config = {"proxy": "http://anchor-failure:7890"}
    await replacement.initialize()
    original_anchor_check = plugin._anchor_check
    plugin._anchor_check = lambda _cls: (_ for _ in ()).throw(RuntimeError("simulated anchor failure"))
    try:
        await replacement.initialize()
    finally:
        plugin._anchor_check = original_anchor_check
    assert replacement.status == "skipped_anchor"
    assert not getattr(FakeAdapter, "__tg_hardened__", False)
    assert not getattr(
        FakeHTTPXRequest.__dict__.get("__init__"), "__tg_hardening_proxy_patch__", False
    )
    assert not hasattr(sys.modules["telegram"], "_cs_extra_handlers")

    # The active owner can transition to enabled=False and cleanly restore state.
    await replacement.initialize()
    replacement.config = {"enabled": False}
    await replacement.initialize()
    assert replacement.status == "disabled"
    assert not getattr(FakeAdapter, "__tg_hardened__", False)
    assert not getattr(
        FakeHTTPXRequest.__dict__.get("__init__"), "__tg_hardening_proxy_patch__", False
    )

    # Upgrade compatibility: v1.2.1's marked proxy wrapper has no owner field or
    # adapter-owner marker. Disabling proxy in the new instance must adopt/release it.
    legacy_owner = plugin.TelegramHardening(
        types.SimpleNamespace(), {"proxy": "http://legacy-owner:7890"}
    )
    await legacy_owner.initialize()
    legacy_init = FakeHTTPXRequest.__dict__["__init__"]
    legacy_state = getattr(legacy_init, plugin._PROXY_STATE_MARKER)
    legacy_state.pop("owner", None)
    delattr(FakeAdapter, "__tayhetgfix_adapter_owner__")

    # An inactive or failed replacement must not adopt/unpatch that legacy
    # ownerless wrapper before it has passed adapter validation.
    failed_legacy_replacement = plugin.TelegramHardening(
        types.SimpleNamespace(), {"proxy_enabled": False}
    )
    original_anchor_check = plugin._anchor_check
    plugin._anchor_check = lambda _cls: (_ for _ in ()).throw(RuntimeError("legacy replacement failure"))
    try:
        await failed_legacy_replacement.initialize()
    finally:
        plugin._anchor_check = original_anchor_check
    assert failed_legacy_replacement.status == "skipped_anchor"
    assert getattr(
        FakeHTTPXRequest.__dict__.get("__init__"), "__tg_hardening_proxy_patch__", False
    )
    assert FakeHTTPXRequest().proxy == "http://legacy-owner:7890"
    disabled_legacy_replacement = plugin.TelegramHardening(
        types.SimpleNamespace(), {"enabled": False}
    )
    await disabled_legacy_replacement.initialize()
    assert getattr(
        FakeHTTPXRequest.__dict__.get("__init__"), "__tg_hardening_proxy_patch__", False
    )
    assert FakeHTTPXRequest().proxy == "http://legacy-owner:7890"

    upgraded = plugin.TelegramHardening(types.SimpleNamespace(), {"proxy_enabled": False})
    await upgraded.initialize()
    assert upgraded.status == "already_applied"
    assert not getattr(
        FakeHTTPXRequest.__dict__.get("__init__"), "__tg_hardening_proxy_patch__", False
    )
    assert FakeHTTPXRequest().proxy is None
    await upgraded.terminate()
    assert not getattr(FakeAdapter, "__tg_hardened__", False)


if __name__ == "__main__":
    asyncio.run(exercise())
    print("plugin smoke test OK")
