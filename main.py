from __future__ import annotations

"""AstrBot Telegram hardening plugin.

This plugin deliberately patches the Telegram adapter at runtime instead of editing
AstrBot's installed source files.  The patch is defensive and reversible:

* adapters are patched only after method/anchor checks pass;
* wrappers are idempotent and survive plugin hot-reload bookkeeping;
* ``terminate`` restores the original class methods;
* unsupported AstrBot versions can be excluded from configuration.

The public ``register_telegram_handler`` helper is intentionally small so another
plugin can register a python-telegram-bot handler without creating a private core
module.  The registry is also exposed on the Telegram source package as
``_cs_extra_handlers`` for compatibility with older plugins; this is an in-memory
attribute, never a file written into AstrBot.
"""

import asyncio
import functools
import importlib
import inspect
import os
import re
import sys
from collections.abc import Iterable, Mapping
from typing import Any, Callable
from urllib.parse import urlsplit

from astrbot.api import logger
from astrbot.api.star import Context, Star, register


PLUGIN_NAME = "astrbot_plugin_tayhetgfix"
PLUGIN_VERSION = "1.2.3"
ADAPTER_MODULE = "astrbot.core.platform.sources.telegram.tg_adapter"
TELEGRAM_PACKAGE = "astrbot.core.platform.sources.telegram"
# None preserves PTB's upstream default (all allowed update types except
# chat_member, message_reaction and message_reaction_count).
DEFAULT_ALLOWED_UPDATES: tuple[str, ...] | None = None
DEFAULT_MAX_RESTART = 3
DEFAULT_PROXY_CONNECT_TIMEOUT = 20.0
DEFAULT_PROXY_READ_TIMEOUT = 20.0
class _Missing:
    """Stable-by-qualified-name marker so records survive module reloads."""


_MISSING = _Missing()
_MEDIA_FIELDS = ("voice", "audio", "photo", "sticker", "document", "video", "video_note")


def _is_missing(value: Any) -> bool:
    value_type = type(value)
    return value is _MISSING or (
        value_type.__module__ == __name__ and value_type.__qualname__ == "_Missing"
    )

# Order matters: Bot API traffic is HTTPS, so an explicit HTTPS/ALL proxy must win
# over a bare HTTP one.  AstrBot mirrors ``http_proxy`` into ``https_proxy`` at
# startup, so in practice both names carry the same value.
_PROXY_ENV_KEYS = (
    "https_proxy",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
    "http_proxy",
    "HTTP_PROXY",
)
_HTTPX_PATCH_MARKER = "__tg_hardening_proxy_patch__"

# Positional slot (excluding ``self``) of every HTTPXRequest parameter this plugin
# may inject.  Needed because a caller can legitimately pass these positionally,
# and injecting the same name as a keyword would raise "got multiple values".
_PROXY_PARAM_POSITIONS = {
    "read_timeout": 1,
    "connect_timeout": 3,
    "proxy": 7,
}

# The package-level registry is installed during initialize().  Keeping a local
# list as a fallback makes the helper useful in tests and before initialization.
_HANDLER_REGISTRY: list[Any] = []


class _PatchError(RuntimeError):
    """Raised internally when a safe patch precondition is not met."""


def register_telegram_handler(handler: Any, *, group: int = 0) -> Any:
    """Register a python-telegram-bot handler for future Telegram applications.

    ``handler`` can be any object accepted by ``Application.add_handler``.  The
    entry is registered after AstrBot's own message handler, and is added to every
    newly rebuilt Telegram application.  A ``(handler, group)`` tuple is stored
    internally, while the legacy list API still accepts bare handlers.
    """

    if handler is None:
        raise ValueError("handler must not be None")
    try:
        normalized_group = int(group)
    except (TypeError, ValueError) as exc:
        raise ValueError("group must be an integer") from exc

    entry = (handler, normalized_group)
    if not any(_same_handler(item, handler) and _handler_group(item) == normalized_group for item in _HANDLER_REGISTRY):
        _HANDLER_REGISTRY.append(entry)
    return handler


# Friendly aliases for other local plugins.
add_telegram_handler = register_telegram_handler
register_handler = register_telegram_handler


def _same_handler(item: Any, handler: Any) -> bool:
    candidate, _ = _normalize_handler_entry(item)
    return candidate is handler or candidate == handler


def _normalize_handler_entry(item: Any) -> tuple[Any, int]:
    if isinstance(item, tuple) and len(item) == 2:
        candidate, group = item
        try:
            return candidate, int(group)
        except (TypeError, ValueError):
            return candidate, 0
    return item, 0


def _handler_group(item: Any) -> int:
    return _normalize_handler_entry(item)[1]


def _read_config(config: Any, key: str, default: Any) -> Any:
    if config is None:
        return default
    try:
        if isinstance(config, Mapping):
            value = config.get(key, default)
        else:
            getter = getattr(config, "get", None)
            value = getter(key, default) if callable(getter) else default
    except Exception:
        return default
    return default if value is None else value


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled", "是", "开启"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled", "否", "关闭"}:
            return False
    return default if value is None else bool(value)


def _allowed_updates(value: Any) -> tuple[str, ...] | None:
    """Normalize explicit update types; None preserves PTB's upstream default."""

    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str):
        raw = re.split(r"[,，\s]+", value.strip())
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, Mapping)):
        raw = list(value)
    else:
        return None

    result: list[str] = []
    for item in raw:
        name = str(item).strip()
        if name and name not in result:
            result.append(name)
    return tuple(result) or None


def _max_restart(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_MAX_RESTART
    return max(0, parsed)


def _restart_delay(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 1.0
    return max(0.0, parsed)


# ── Telegram 出站代理 ────────────────────────────────────────────────────────
# AstrBot only exports ``http_proxy``/``https_proxy`` as process environment
# variables.  python-telegram-bot builds its bot request and its ``getUpdates``
# request from ``HTTPXRequest``, and httpx only honours those variables when the
# client is created with ``trust_env`` untouched.  Relying on that indirection is
# what makes Telegram polling fail with ``httpx.ConnectError`` behind a proxy, so
# the proxy is injected explicitly into every ``HTTPXRequest`` instead.
_PROXY_STATE: dict[str, Any] = {
    "proxy": "",
    "connect_timeout": DEFAULT_PROXY_CONNECT_TIMEOUT,
    "read_timeout": DEFAULT_PROXY_READ_TIMEOUT,
    "owner": None,
}
_DIRECT_PROXY_OWNER = "__tayhetgfix_direct_owner__"
_HTTPX_ORIGINAL_INIT: Any = None
_PROXY_STATE_MARKER = "__tg_hardening_proxy_state__"
_PROXY_ORIGINAL_MARKER = "__tg_hardening_original_init__"


def _resolve_proxy(config: Any) -> str:
    """Resolve the proxy URL from plugin config, then the process environment."""

    explicit = str(_read_config(config, "proxy", "") or "").strip()
    if explicit:
        return explicit
    for key in _PROXY_ENV_KEYS:
        value = os.environ.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _proxy_timeout(value: Any, default: float) -> float | None:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else None


def _proxy_log_target(proxy: str) -> str:
    """Return only the non-sensitive scheme/host/port portion of a proxy URL."""

    try:
        parsed = urlsplit(proxy)
        host = parsed.hostname
        if not host:
            return "configured proxy"
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme}://{host}{port}"
    except ValueError:
        return "configured proxy"


def _install_httpx_proxy_patch(
    proxy: str,
    connect_timeout: float | None,
    read_timeout: float | None,
    owner: Any = None,
) -> bool:
    """Install or refresh the process-wide proxy using a single owner token."""

    global _HTTPX_ORIGINAL_INIT, _PROXY_STATE

    owner = _DIRECT_PROXY_OWNER if owner is None else owner
    try:
        request_module = importlib.import_module("telegram.request")
    except Exception:
        _logger_call("debug", "[tayhetgfix] 未找到 telegram.request，跳过代理注入")
        return False
    request_cls = getattr(request_module, "HTTPXRequest", None)
    if not isinstance(request_cls, type):
        _logger_call("warning", "[tayhetgfix] telegram.request.HTTPXRequest 不可用，跳过代理注入")
        return False

    current_init = request_cls.__dict__.get("__init__", None)
    shared_state = getattr(current_init, _PROXY_STATE_MARKER, None)
    is_patched = bool(getattr(current_init, _HTTPX_PATCH_MARKER, False))
    if is_patched and isinstance(shared_state, dict):
        _PROXY_STATE = shared_state
        original = getattr(current_init, _PROXY_ORIGINAL_MARKER, None)
        if original is not None:
            _HTTPX_ORIGINAL_INIT = original
        if not proxy:
            _restore_httpx_proxy_patch(owner)
            return False
        shared_state.update(
            proxy=proxy,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            owner=owner,
        )
        _logger_call("info", "[tayhetgfix] 已刷新 Telegram 出站代理: %s", _proxy_log_target(proxy))
        return True

    # A request to clear/release a lease must not unpatch another owner.
    if not proxy:
        return False

    original_init = request_cls.__init__
    try:
        params = [
            name
            for name in inspect.signature(original_init).parameters
            if name != "self"
        ]
        positions = {name: index for index, name in enumerate(params)}
    except (TypeError, ValueError):
        positions = dict(_PROXY_PARAM_POSITIONS)
    proxy_param = next((name for name in ("proxy", "proxy_url") if name in positions), None)
    if proxy_param is None:
        _logger_call("warning", "[tayhetgfix] PTB HTTPXRequest 无 proxy/proxy_url 参数，跳过代理注入")
        return False

    state = _PROXY_STATE
    state.update(
        proxy=proxy,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        owner=owner,
    )

    def _already_positional(name: str, args: tuple[Any, ...]) -> bool:
        position = positions.get(name)
        return position is not None and len(args) > position

    @functools.wraps(original_init)
    def patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        target = state.get("proxy") or ""
        if target and proxy_param not in kwargs and not _already_positional(proxy_param, args):
            kwargs[proxy_param] = target
        if target:
            for key in ("connect_timeout", "read_timeout"):
                if key in kwargs or _already_positional(key, args) or key not in positions:
                    continue
                value = state.get(key)
                if value is not None:
                    kwargs[key] = value
        original_init(self, *args, **kwargs)

    setattr(patched_init, _HTTPX_PATCH_MARKER, True)
    setattr(patched_init, _PROXY_STATE_MARKER, state)
    setattr(patched_init, _PROXY_ORIGINAL_MARKER, original_init)
    request_cls.__init__ = patched_init
    _HTTPX_ORIGINAL_INIT = original_init
    _logger_call("info", "[tayhetgfix] 已注入 Telegram 出站代理: %s", _proxy_log_target(proxy))
    return True


def _restore_httpx_proxy_patch(owner: Any = None) -> None:
    """Release the proxy patch only when called by its current owner."""

    global _HTTPX_ORIGINAL_INIT, _PROXY_STATE

    owner = _DIRECT_PROXY_OWNER if owner is None else owner
    try:
        request_module = importlib.import_module("telegram.request")
        request_cls = getattr(request_module, "HTTPXRequest", None)
    except Exception:
        request_cls = None

    current_init = (
        request_cls.__dict__.get("__init__", None)
        if isinstance(request_cls, type)
        else None
    )
    shared_state = getattr(current_init, _PROXY_STATE_MARKER, None)
    if isinstance(shared_state, dict):
        _PROXY_STATE = shared_state
    state = _PROXY_STATE
    if state.get("owner") is not owner:
        return

    if getattr(current_init, _HTTPX_PATCH_MARKER, False):
        original = getattr(current_init, _PROXY_ORIGINAL_MARKER, None)
        if original is None:
            original = _HTTPX_ORIGINAL_INIT
        if original is not None and isinstance(request_cls, type):
            request_cls.__init__ = original
    state.update(proxy="", owner=None)
    _HTTPX_ORIGINAL_INIT = None


def _configure_httpx_proxy(
    proxy: str,
    enabled: bool,
    owner: Any,
    previous_owner: Any,
    connect_timeout: float | None,
    read_timeout: float | None,
) -> bool:
    """Apply proxy settings after the adapter patch has initialized successfully."""

    request_module = sys.modules.get("telegram.request")
    request_cls = getattr(request_module, "HTTPXRequest", None)
    current_init = request_cls.__dict__.get("__init__") if isinstance(request_cls, type) else None
    state = getattr(current_init, _PROXY_STATE_MARKER, None)
    if getattr(current_init, _HTTPX_PATCH_MARKER, False) and isinstance(state, dict):
        state_owner = state.get("owner")
        owns_previous_patch = previous_owner is not None and state_owner is previous_owner
        is_legacy_unowned_patch = previous_owner is None and state_owner is None
        if owns_previous_patch or is_legacy_unowned_patch:
            state["owner"] = owner

    if not enabled or not proxy:
        _restore_httpx_proxy_patch(owner)
        return False
    return _install_httpx_proxy_patch(proxy, connect_timeout, read_timeout, owner=owner)


def _astrbot_version() -> str:
    try:
        module = importlib.import_module("astrbot")
    except Exception:
        return "unknown"
    for attr in ("__version__", "VERSION", "version"):
        value = getattr(module, attr, None)
        if value:
            return str(value).strip()
    return "unknown"


def _version_token_matches(version: str, token: str) -> bool:
    version = version.strip().lstrip("vV")
    token = token.strip().lstrip("vV")
    if not token:
        return False
    if token.endswith("*"):
        return version.startswith(token[:-1])
    return version == token or version.startswith(token + ".")


def _incompatible_version(config: Any, version: str) -> bool:
    raw = _read_config(config, "incompatible_versions", None)
    if raw is None:
        raw = _read_config(config, "skip_versions", [])
    if isinstance(raw, str):
        tokens = re.split(r"[,，\s]+", raw.strip())
    elif isinstance(raw, Iterable) and not isinstance(raw, (bytes, bytearray, Mapping)):
        tokens = list(raw)
    else:
        tokens = []
    return any(_version_token_matches(version, str(token)) for token in tokens)


def _method_source(method: Any) -> str:
    try:
        return inspect.getsource(method)
    except (OSError, TypeError):
        return ""


def _first_media(message: Any) -> tuple[str | None, Any]:
    """Return the first Telegram media field present on a message."""

    if message is None:
        return None, None
    for field in _MEDIA_FIELDS:
        try:
            value = getattr(message, field, None)
        except Exception:
            continue
        if value:
            return field, value
    return None, None


class _MaskedTelegramMessage:
    """Read-through message proxy with one media field hidden.

    AstrBot's upstream ``convert_message`` contains several independent media
    branches.  If one download raises before it can construct an
    ``AstrBotMessage``, retrying against this proxy lets the unchanged upstream
    converter build the normal envelope (sender, reply, chat metadata, etc.)
    without entering the failed branch.  We then add the same empty component
    used by the historical hardening patch.
    """

    def __init__(self, original: Any, masked_field: str) -> None:
        self._original = original
        self._masked_field = masked_field

    def __getattr__(self, name: str) -> Any:
        if name == self._masked_field:
            return None
        return getattr(self._original, name)

    def __bool__(self) -> bool:
        return bool(self._original)

    def to_dict(self) -> dict[str, Any]:
        converter = getattr(self._original, "to_dict", None)
        if callable(converter):
            try:
                result = converter()
            except Exception:
                result = {}
            if isinstance(result, dict):
                result = dict(result)
                result.pop(self._masked_field, None)
                return result
        return {self._masked_field: None}


class _MaskedTelegramUpdate:
    def __init__(self, original: Any, masked_field: str) -> None:
        self._original = original
        self.message = _MaskedTelegramMessage(getattr(original, "message", None), masked_field)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)


def _replace_update_call(
    args: tuple[Any, ...], kwargs: dict[str, Any], update: Any
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    if args:
        new_args = list(args)
        new_args[0] = update
        return tuple(new_args), kwargs
    new_kwargs = dict(kwargs)
    new_kwargs["update"] = update
    return args, new_kwargs


def _original_globals(original: Callable[..., Any]) -> dict[str, Any]:
    value = getattr(original, "__globals__", None)
    return value if isinstance(value, dict) else {}


def _component_class(original: Callable[..., Any], name: str) -> Any:
    components = _original_globals(original).get("Comp")
    return getattr(components, name, None)


def _append_caption_fallback(result: Any, message: Any, original: Callable[..., Any]) -> None:
    caption = getattr(message, "caption", None)
    if not isinstance(caption, str) or not caption:
        return
    chain = getattr(result, "message", None)
    if not isinstance(chain, list):
        chain = []
        setattr(result, "message", chain)
    if getattr(result, "message_str", "") == caption:
        return
    plain = _component_class(original, "Plain")
    if callable(plain):
        chain.append(plain(caption))
    setattr(result, "message_str", caption)

    at = _component_class(original, "At")
    for entity in getattr(message, "caption_entities", None) or ():
        if getattr(entity, "type", None) != "mention" or not callable(at):
            continue
        try:
            name = caption[entity.offset + 1 : entity.offset + entity.length]
            chain.append(at(qq=name, name=name))
        except Exception:
            continue


def _has_media_component(
    result: Any, media_kind: str, original: Callable[..., Any]
) -> bool:
    chain = getattr(result, "message", None)
    if not isinstance(chain, (list, tuple)):
        return False
    component_name = {
        "voice": "Record",
        "audio": "Record",
        "photo": "Image",
        "sticker": "Image",
        "document": "File",
        "video": "Video",
        "video_note": "Video",
    }.get(media_kind)
    component_class = _component_class(original, component_name) if component_name else None
    for item in chain:
        if component_class is not None:
            try:
                if isinstance(item, component_class):
                    return True
            except TypeError:
                pass
        if component_name and item.__class__.__name__.lower() == component_name.lower():
            return True
    return False


def _append_media_placeholder(
    result: Any,
    telegram_message: Any,
    media_kind: str,
    original: Callable[..., Any],
) -> None:
    chain = getattr(result, "message", None)
    if not isinstance(chain, list):
        chain = []
        setattr(result, "message", chain)

    component: Any = None
    if media_kind in ("voice", "audio"):
        record = _component_class(original, "Record")
        if callable(record):
            component = record(file="", url="")
            try:
                component.path = ""
            except Exception:
                pass
    elif media_kind in ("photo", "sticker"):
        image = _component_class(original, "Image")
        if callable(image):
            component = image(file="", url="")
    elif media_kind == "document":
        file_component = _component_class(original, "File")
        if callable(file_component):
            media = getattr(telegram_message, media_kind, None)
            file_name = getattr(media, "file_name", None) or "telegram_document"
            component = file_component(file="", name=file_name, url="")
    elif media_kind in ("video", "video_note"):
        video = _component_class(original, "Video")
        if callable(video):
            component = video(file="", path="")

    if component is not None:
        chain.append(component)

    if media_kind == "sticker":
        media = getattr(telegram_message, media_kind, None)
        emoji = getattr(media, "emoji", None)
        if emoji:
            text = f"Sticker: {emoji}"
            plain = _component_class(original, "Plain")
            if callable(plain):
                chain.append(plain(text))
            setattr(result, "message_str", text)
    elif media_kind in ("audio", "photo", "document", "video"):
        _append_caption_fallback(result, telegram_message, original)


def _rich_inline(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        if "text" in value:
            return _rich_inline(value.get("text"))
        for key in ("value", "content", "label"):
            if key in value:
                text = _rich_inline(value.get(key))
                if text:
                    return text
        return ""
    if isinstance(value, (list, tuple)):
        return "".join(_rich_inline(item) for item in value)
    return ""


def _flatten_rich_blocks(blocks: Any) -> str:
    """Flatten AstrBot/Telegram rich-message blocks, including tables."""

    if not isinstance(blocks, (list, tuple)):
        return ""
    output: list[str] = []
    for block in blocks:
        if isinstance(block, str):
            if block.strip():
                output.append(block)
            continue
        if not isinstance(block, Mapping):
            continue

        parts: list[str] = []
        inline = _rich_inline(block.get("text"))
        if inline:
            parts.append(inline)

        cells = block.get("cells")
        if isinstance(cells, (list, tuple)):
            rows: list[str] = []
            for row in cells:
                if not isinstance(row, (list, tuple)):
                    continue
                values = [_rich_inline(cell) for cell in row]
                values = [value for value in values if value]
                if values:
                    rows.append(" | ".join(values))
            if rows:
                parts.append("\n".join(rows))

        nested = block.get("blocks")
        if isinstance(nested, (list, tuple)):
            nested_text = _flatten_rich_blocks(nested)
            if nested_text:
                parts.append(nested_text)

        for key in ("children", "content"):
            child = block.get(key)
            if isinstance(child, (list, tuple)):
                child_text = _flatten_rich_blocks(child)
                if child_text:
                    parts.append(child_text)

        if not parts:
            for key in ("summary", "caption"):
                text = _rich_inline(block.get(key))
                if text:
                    parts.append(text)
                    break
        if parts:
            output.append("\n".join(parts))
    return "\n".join(item for item in output if item.strip())


def _rich_message_text(message: Any) -> str:
    """Extract plain text from rich_message in PTB to_dict/api_kwargs shapes."""

    raw: dict[str, Any] = {}
    converter = getattr(message, "to_dict", None)
    if callable(converter):
        try:
            candidate = converter()
            if isinstance(candidate, dict):
                raw = candidate
        except Exception:
            pass

    sources: list[Any] = [raw]
    api_kwargs = raw.get("api_kwargs")
    if isinstance(api_kwargs, Mapping):
        sources.append(api_kwargs)
    object_api_kwargs = getattr(message, "api_kwargs", None)
    if isinstance(object_api_kwargs, Mapping):
        sources.append(object_api_kwargs)
    rich_attr = getattr(message, "rich_message", None)
    if rich_attr is not None:
        sources.append(rich_attr)

    seen: set[int] = set()
    queue = list(sources)
    while queue:
        source = queue.pop(0)
        if id(source) in seen:
            continue
        seen.add(id(source))
        if isinstance(source, str):
            if source.strip():
                return source
            continue
        if not isinstance(source, Mapping):
            continue

        rich = source.get("rich_message")
        if rich is not None:
            queue.append(rich)
        for key in ("text", "markdown", "plain"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value
        blocks = source.get("blocks")
        text = _flatten_rich_blocks(blocks)
        if text.strip():
            return text
        cells = source.get("cells")
        if cells:
            text = _flatten_rich_blocks([{"cells": cells}])
            if text.strip():
                return text
        for value in source.values():
            if isinstance(value, Mapping) and (
                "rich_message" in value or "blocks" in value
            ):
                queue.append(value)
    return ""


def _attach_rich_fallback(result: Any, update: Any, original: Callable[..., Any]) -> bool:
    if result is None or getattr(result, "message_str", ""):
        return False
    chain = getattr(result, "message", None)
    if chain:
        return False
    text = _rich_message_text(getattr(update, "message", None))
    if not text.strip():
        return False
    plain = _component_class(original, "Plain")
    if not callable(plain):
        return False
    if not isinstance(chain, list):
        chain = []
        setattr(result, "message", chain)
    chain.append(plain(text))
    setattr(result, "message_str", text)
    _logger_call("info", "[tayhetgfix] 已从 rich_message 提取文本（%d 字符）", len(text))
    return True


def _anchor_check(adapter_cls: type) -> None:
    required = {
        "_build_application": ("application", "add_handler"),
        "run": ("updater", "start_polling"),
        "message_handler": ("message_handler",),
    }
    missing = [name for name in required if not callable(getattr(adapter_cls, name, None))]
    if missing:
        raise _PatchError(f"missing Telegram adapter methods: {', '.join(missing)}")

    # Source inspection is best effort: frozen/optimized deployments may not expose
    # source.  If source is available, require a recognizable anchor before patching.
    weak_anchors: list[str] = []
    for name, anchors in required.items():
        source = _method_source(getattr(adapter_cls, name))
        if source and not any(anchor in source for anchor in anchors):
            weak_anchors.append(name)
    if weak_anchors:
        raise _PatchError(
            "Telegram adapter anchors not recognized: " + ", ".join(weak_anchors)
        )


def _logger_call(level: str, message: str, *args: Any, exc_info: Any = None) -> None:
    method = getattr(logger, level, None)
    if not callable(method):
        return
    try:
        if exc_info is None:
            method(message, *args)
        else:
            method(message, *args, exc_info=exc_info)
    except TypeError:
        # Some AstrBot logger shims do not accept ``exc_info``.
        method(message, *args)


def _record_for(adapter_cls: type) -> dict[str, Any] | None:
    record = getattr(adapter_cls, "__tg_hardening_record__", None)
    return record if isinstance(record, dict) else None


def _runtime_for(instance_or_cls: Any) -> dict[str, Any]:
    adapter_cls = instance_or_cls if isinstance(instance_or_cls, type) else type(instance_or_cls)
    record = _record_for(adapter_cls)
    runtime = record.get("runtime") if record else None
    return runtime if isinstance(runtime, dict) else {}


def _iter_registry_values(adapter_module: Any, telegram_package: Any) -> Iterable[Any]:
    seen: set[int] = set()
    candidates = (
        _HANDLER_REGISTRY,
        getattr(telegram_package, "__tg_hardening_handler_registry__", None),
        getattr(telegram_package, "_cs_extra_handlers", None),
        getattr(telegram_package, "__tg_hardening_handlers__", None),
        getattr(adapter_module, "__tg_hardening_handlers__", None),
        getattr(adapter_module, "_cs_extra_handlers", None),
    )
    for candidate in candidates:
        if candidate is None or isinstance(candidate, (str, bytes, Mapping)):
            continue
        try:
            values = list(candidate)
        except TypeError:
            continue
        for item in values:
            marker = id(item)
            if marker in seen:
                continue
            seen.add(marker)
            yield item


def _application_has_handler(application: Any, handler: Any, group: int) -> bool:
    handlers = getattr(application, "handlers", None)
    if isinstance(handlers, Mapping):
        for current_group, entries in handlers.items():
            if int(current_group) != group:
                continue
            for existing in entries or ():
                if existing is handler or existing == handler:
                    return True
    # Test doubles and older PTB versions may expose a flat list.
    if isinstance(handlers, (list, tuple)):
        return any(existing is handler or existing == handler for existing in handlers)
    return False


def _register_external_handlers(instance: Any, adapter_module: Any, telegram_package: Any) -> int:
    application = getattr(instance, "application", None)
    add_handler = getattr(application, "add_handler", None)
    if application is None or not callable(add_handler):
        return 0

    count = 0
    for raw_entry in _iter_registry_values(adapter_module, telegram_package):
        handler, group = _normalize_handler_entry(raw_entry)
        if handler is None or _application_has_handler(application, handler, group):
            continue
        try:
            add_handler(handler, group=group)
        except TypeError:
            # A few test doubles and old PTB releases only accept the handler.
            try:
                add_handler(handler)
            except Exception as exc:
                _logger_call("warning", "[tayhetgfix] 注册额外 handler 失败: %r (%s)", handler, exc)
                continue
        except Exception as exc:
            _logger_call("warning", "[tayhetgfix] 注册额外 handler 失败: %r (%s)", handler, exc)
            continue
        count += 1
    return count


def _install_polling_guard(
    instance: Any, allowed_updates: tuple[str, ...] | None
) -> bool:
    application = getattr(instance, "application", None)
    updater = getattr(application, "updater", None)
    start_polling = getattr(updater, "start_polling", None)
    if updater is None or not callable(start_polling):
        return False
    if getattr(start_polling, "__tg_hardening_wrapper__", False):
        return True

    original = start_polling

    @functools.wraps(original)
    async def guarded_start_polling(*args: Any, **kwargs: Any) -> Any:
        if allowed_updates is not None:
            kwargs["allowed_updates"] = list(allowed_updates)
        result = original(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    setattr(guarded_start_polling, "__tg_hardening_wrapper__", True)
    setattr(guarded_start_polling, "__tg_hardening_original__", original)
    try:
        setattr(updater, "start_polling", guarded_start_polling)
    except Exception as exc:
        _logger_call("warning", "[tayhetgfix] 无法包装 Telegram polling: %s", exc)
        return False
    return True


def _post_build_application(instance: Any) -> None:
    runtime = _runtime_for(instance)
    if not runtime.get("enabled", False):
        return
    module = runtime.get("adapter_module")
    package = runtime.get("telegram_package")
    if module is None or package is None:
        return
    _install_polling_guard(instance, runtime.get("allowed_updates", DEFAULT_ALLOWED_UPDATES))
    count = _register_external_handlers(instance, module, package)
    if count:
        _logger_call("debug", "[tayhetgfix] 已注册 %d 个额外 Telegram handler", count)


async def _await_if_needed(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _safe_rebuild(instance: Any) -> None:
    shutdown = getattr(instance, "_shutdown_application", None)
    if callable(shutdown):
        try:
            try:
                result = shutdown(delete_commands=False)
            except TypeError:
                result = shutdown()
            await _await_if_needed(result)
        except Exception as exc:
            _logger_call("warning", "[tayhetgfix] 崩溃后关闭 Telegram application 失败: %s", exc)

    build = getattr(instance, "_build_application", None)
    if not callable(build):
        raise _PatchError("Telegram adapter no longer exposes _build_application")
    result = build()
    await _await_if_needed(result)


def _make_build_wrapper(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(self, *args, **kwargs)
        # _build_application is synchronous in supported AstrBot versions.  If a
        # future version makes it async, leave its result untouched rather than
        # attempting to await from a sync wrapper.
        if inspect.isawaitable(result):
            return result
        _post_build_application(self)
        return result

    setattr(wrapped, "__tg_hardening_wrapper__", True)
    return wrapped


def _make_message_wrapper(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    async def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return await original(self, *args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _logger_call(
                "error",
                "[tayhetgfix] Telegram message_handler 异常已拦截: %s",
                exc,
                exc_info=True,
            )
            return None

    setattr(wrapped, "__tg_hardening_wrapper__", True)
    return wrapped


def _make_convert_wrapper(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    async def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        update = args[0] if args else kwargs.get("update")
        message = getattr(update, "message", None)
        media_kind, media = _first_media(message)
        runtime = _runtime_for(self)

        try:
            result = await original(self, *args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Only retry as a media failure when the update actually contains a
            # media object.  Unrelated converter bugs must remain visible to the
            # outer message_handler safety net instead of being silently hidden.
            if not media_kind or not runtime.get("media_download_fallback", True):
                raise
            _logger_call(
                "warning",
                "[tayhetgfix] Telegram %s 下载/转换失败，改用空占位: %s",
                media_kind,
                exc,
                exc_info=True,
            )
            masked_update = _MaskedTelegramUpdate(update, media_kind)
            retry_args, retry_kwargs = _replace_update_call(args, kwargs, masked_update)
            try:
                result = await original(self, *retry_args, **retry_kwargs)
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger_call(
                    "error",
                    "[tayhetgfix] Telegram %s 失败后的安全重试也失败",
                    media_kind,
                    exc_info=True,
                )
                raise
            if result is not None:
                _append_media_placeholder(result, message, media_kind, original)
                # Do not leak the masked proxy as the raw update seen by downstream
                # event handlers; preserve the real Telegram update.
                try:
                    result.raw_message = update
                except Exception:
                    pass
                return result

        if (
            result is not None
            and media_kind
            and runtime.get("media_download_fallback", True)
            and not getattr(message, "text", None)
            and not _has_media_component(result, media_kind, original)
        ):
            # Upstream versions may return normally when file_path is None
            # without adding a component. Match the historical patch by
            # retaining the message with an empty media placeholder.
            _append_media_placeholder(result, message, media_kind, original)

        if runtime.get("rich_message_fallback", True):
            _attach_rich_fallback(result, update, original)
        return result

    setattr(wrapped, "__tg_hardening_wrapper__", True)
    return wrapped


def _make_send_rich_message_wrapper() -> Callable[..., Any]:
    @functools.wraps(_send_rich_message)
    async def wrapped(
        self: Any,
        chat_id: str | int,
        html: str,
        *,
        disable_notification: bool | None = None,
    ) -> dict[str, Any]:
        return await _send_rich_message(
            self,
            chat_id,
            html,
            disable_notification=disable_notification,
        )

    setattr(wrapped, "__tg_hardening_wrapper__", True)
    return wrapped


async def _send_rich_message(
    self: Any,
    chat_id: str | int,
    html: str,
    *,
    disable_notification: bool | None = None,
) -> dict[str, Any]:
    """Send custom Rich Message markup through the adapter's existing bot."""

    if not str(chat_id).strip():
        raise ValueError("chat_id must not be empty")
    if not isinstance(html, str) or not html.strip():
        raise ValueError("rich message html must not be empty")

    client = getattr(self, "client", None)
    post = getattr(client, "_post", None)
    if not callable(post):
        # A few AstrBot revisions expose the ExtBot as ``bot`` instead of
        # ``client``.  Prefer the documented adapter client but accept both.
        client = getattr(self, "bot", None)
        post = getattr(client, "_post", None)
    if not callable(post):
        raise RuntimeError("Telegram adapter does not expose bot client._post")

    result = post(
        "sendRichMessage",
        data={
            "chat_id": chat_id,
            "rich_message": {"html": html},
            "disable_notification": disable_notification,
        },
    )
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, dict):
        raise TypeError(
            "Telegram sendRichMessage returned an unexpected response type: "
            f"{type(result).__name__}"
        )
    return result


def _make_run_wrapper(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    async def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        attempts = 0
        while True:
            try:
                return await original(self, *args, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempts += 1
                runtime = _runtime_for(self)
                limit = int(runtime.get("max_restart", DEFAULT_MAX_RESTART))
                _logger_call(
                    "error",
                    "[tayhetgfix] Telegram run 崩溃（第 %d 次）: %s",
                    attempts,
                    exc,
                    exc_info=True,
                )
                if getattr(self, "_terminating", False) or attempts > limit:
                    _logger_call(
                        "error",
                        "[tayhetgfix] 已达到 max_restart=%d，停止外层自愈重启",
                        limit,
                    )
                    raise
                try:
                    await _safe_rebuild(self)
                except Exception as rebuild_exc:
                    _logger_call(
                        "error",
                        "[tayhetgfix] 崩溃后重建 Telegram application 失败: %s",
                        rebuild_exc,
                        exc_info=True,
                    )
                    if attempts >= limit:
                        raise
                delay = float(runtime.get("restart_delay", 1.0))
                if delay:
                    await asyncio.sleep(delay)

    setattr(wrapped, "__tg_hardening_wrapper__", True)
    return wrapped


def _install_package_registry(package: Any, owner: Any = None) -> tuple[Any, bool, Any]:
    """Install an in-memory registry and preserve the original package state."""

    state = getattr(package, "__tayhetgfix_registry_state__", None)
    if not isinstance(state, dict):
        state = {
            "had_attr": hasattr(package, "_cs_extra_handlers"),
            "old_value": getattr(package, "_cs_extra_handlers", None),
            "owner": owner,
        }
        setattr(package, "__tayhetgfix_registry_state__", state)

    had_attr, old_value = state["had_attr"], state["old_value"]
    registry = getattr(package, "__tg_hardening_handler_registry__", None)
    if not isinstance(registry, list):
        registry = old_value if isinstance(old_value, list) else _HANDLER_REGISTRY
        setattr(package, "__tg_hardening_handler_registry__", registry)

    # Merge helpers registered before initialize(), preserving identity so old
    # plugins that append to package._cs_extra_handlers keep working.
    for item in _HANDLER_REGISTRY:
        if not any(existing is item or existing == item for existing in registry):
            registry.append(item)
    setattr(package, "_cs_extra_handlers", registry)
    return registry, had_attr, old_value


def _claim_package_registry(package: Any, owner: Any) -> None:
    state = getattr(package, "__tayhetgfix_registry_state__", None)
    if isinstance(state, dict):
        state["owner"] = owner


def _restore_package_registry(
    package: Any,
    previous: tuple[bool, Any] | None,
    owner: Any = None,
) -> None:
    state = getattr(package, "__tayhetgfix_registry_state__", None)
    if isinstance(state, dict):
        if state.get("owner") is not owner:
            return
        had_attr, old_value = state["had_attr"], state["old_value"]
    elif previous is not None:
        had_attr, old_value = previous
    else:
        return

    if had_attr:
        setattr(package, "_cs_extra_handlers", old_value)
    else:
        try:
            delattr(package, "_cs_extra_handlers")
        except AttributeError:
            pass
    for marker in ("__tg_hardening_handler_registry__", "__tayhetgfix_registry_state__"):
        try:
            delattr(package, marker)
        except AttributeError:
            pass


def _unpatch_adapter(adapter_cls: type) -> bool:
    record = _record_for(adapter_cls)
    if not record:
        return False
    originals = record.get("originals", {})
    wrappers = record.get("wrappers", {})
    restored = False
    for name, original in originals.items():
        current = getattr(adapter_cls, name, None)
        wrapper = wrappers.get(name)
        if current is wrapper:
            if _is_missing(original):
                try:
                    delattr(adapter_cls, name)
                except AttributeError:
                    pass
            else:
                setattr(adapter_cls, name, original)
            restored = True
    if getattr(adapter_cls, "__tg_hardened__", False):
        try:
            delattr(adapter_cls, "__tg_hardened__")
        except AttributeError:
            pass
    try:
        delattr(adapter_cls, "__tg_hardening_record__")
    except AttributeError:
        pass
    return restored


@register(
    PLUGIN_NAME,
    "tangshusheng",
    "以可卸载、幂等的运行时补丁增强 AstrBot Telegram 适配器，避免直接修改核心文件。",
    PLUGIN_VERSION,
    "https://github.com/tangshusheng/astrbot_plugin_tayhetgfix",
)
class TelegramHardening(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self._adapter_cls: type | None = None
        self._package: Any = None
        self._previous_package_registry: tuple[bool, Any] | None = None
        self._proxy_owner = getattr(self, "_proxy_owner", None) or object()
        self.status = "not_initialized"
        self.version = "unknown"

    async def initialize(self) -> None:
        enabled = _as_bool(_read_config(self.config, "enabled", True), True)
        self.version = _astrbot_version()
        if not enabled:
            self._deactivate()
            self.status = "disabled"
            _logger_call("info", "[tayhetgfix] 插件已禁用")
            return
        if _incompatible_version(self.config, self.version):
            self._deactivate()
            self.status = "skipped_version"
            _logger_call(
                "warning",
                "[tayhetgfix] AstrBot %s 在 incompatible_versions/skip_versions 中，跳过补丁",
                self.version,
            )
            return

        try:
            adapter_module = importlib.import_module(ADAPTER_MODULE)
            package = importlib.import_module(TELEGRAM_PACKAGE)
            adapter_cls = getattr(adapter_module, "TelegramPlatformAdapter", None)
            if not isinstance(adapter_cls, type):
                raise _PatchError("TelegramPlatformAdapter class not found")
            _anchor_check(adapter_cls)
        except Exception as exc:
            # If this instance was already active, release only its own patch
            # state. A failed replacement has no ownership and must not disturb
            # the currently active instance.
            self._deactivate()
            self.status = "skipped_anchor"
            _logger_call(
                "warning",
                "[tayhetgfix] 安全锚点检查失败，已跳过补丁: %s",
                exc,
                exc_info=True,
            )
            return

        previous_adapter_owner = getattr(adapter_cls, "__tayhetgfix_adapter_owner__", None)
        registry, had_attr, old_value = _install_package_registry(package, self._proxy_owner)
        self._package = package
        self._previous_package_registry = (had_attr, old_value)
        allowed = _allowed_updates(_read_config(self.config, "allowed_updates", None))

        # Telegram 出站代理必须显式注入：AstrBot 只导出环境变量，而 PTB 的
        # HTTPXRequest 在部分 httpx 版本下不会读取它们，表现为轮询 ConnectError。
        proxy = _resolve_proxy(self.config)
        proxy_enabled = _as_bool(_read_config(self.config, "proxy_enabled", True), True)
        proxy_applied = False
        if not proxy:
            _logger_call(
                "info",
                "[tayhetgfix] 未配置 proxy 且环境中没有 http_proxy/https_proxy，"
                "Telegram 将直连（墙内会 ConnectError）",
            )

        runtime = {
            "enabled": True,
            "allowed_updates": allowed,
            "proxy": proxy,
            "proxy_applied": proxy_applied,
            "max_restart": _max_restart(_read_config(self.config, "max_restart", DEFAULT_MAX_RESTART)),
            "restart_delay": _restart_delay(_read_config(self.config, "restart_delay", 1.0)),
            "media_download_fallback": _as_bool(
                _read_config(self.config, "media_download_fallback", True), True
            ),
            "rich_message_fallback": _as_bool(
                _read_config(self.config, "rich_message_fallback", True), True
            ),
            "adapter_module": adapter_module,
            "telegram_package": package,
            "handler_registry": registry,
        }

        existing = _record_for(adapter_cls)
        if existing is not None:
            # A duplicate initialize must not wrap a wrapper again.  When the
            # plugin itself was hot-reloaded after a feature addition, remove
            # the older record first so the new wrappers are installed too.
            old_wrappers = existing.get("wrappers", {})
            needs_refresh = callable(getattr(adapter_cls, "convert_message", None)) and (
                "convert_message" not in old_wrappers
            )
            if (
                (
                    _is_missing(getattr(adapter_cls, "send_rich_message", _MISSING))
                    or not callable(getattr(adapter_cls, "send_rich_message", None))
                )
                and "send_rich_message" not in old_wrappers
            ):
                needs_refresh = True
            if not needs_refresh:
                _claim_package_registry(package, self._proxy_owner)
                proxy_applied = _configure_httpx_proxy(
                    proxy,
                    proxy_enabled,
                    self._proxy_owner,
                    previous_adapter_owner,
                    _proxy_timeout(
                        _read_config(self.config, "proxy_connect_timeout", DEFAULT_PROXY_CONNECT_TIMEOUT),
                        DEFAULT_PROXY_CONNECT_TIMEOUT,
                    ),
                    _proxy_timeout(
                        _read_config(self.config, "proxy_read_timeout", DEFAULT_PROXY_READ_TIMEOUT),
                        DEFAULT_PROXY_READ_TIMEOUT,
                    ),
                )
                runtime["proxy_applied"] = proxy_applied
                existing["runtime"] = runtime
                setattr(adapter_cls, "__tg_hardened__", True)
                setattr(adapter_cls, "__tayhetgfix_adapter_owner__", self._proxy_owner)
                self._adapter_cls = adapter_cls
                self.status = "already_applied"
                _logger_call(
                    "info", "[tayhetgfix] 补丁已存在，刷新配置（version=%s）", self.version
                )
                return
            _unpatch_adapter(adapter_cls)

        originals: dict[str, Any] = {
            "_build_application": getattr(adapter_cls, "_build_application"),
            "run": getattr(adapter_cls, "run"),
            "message_handler": getattr(adapter_cls, "message_handler"),
        }
        if not all(inspect.iscoroutinefunction(originals[name]) for name in ("run", "message_handler")):
            self.status = "skipped_anchor"
            _logger_call("warning", "[tayhetgfix] run/message_handler 不是 async 方法，跳过补丁")
            _restore_package_registry(package, self._previous_package_registry, self._proxy_owner)
            self._previous_package_registry = None
            return

        wrappers = {
            "_build_application": _make_build_wrapper(originals["_build_application"]),
            "run": _make_run_wrapper(originals["run"]),
            "message_handler": _make_message_wrapper(originals["message_handler"]),
        }

        # These are optional in older AstrBot versions.  The three core
        # hardening wrappers remain usable even when a revision has no
        # ``convert_message`` or already ships ``send_rich_message``.
        convert_message = getattr(adapter_cls, "convert_message", None)
        if callable(convert_message) and inspect.iscoroutinefunction(convert_message):
            originals["convert_message"] = convert_message
            wrappers["convert_message"] = _make_convert_wrapper(convert_message)
        else:
            _logger_call(
                "warning",
                "[tayhetgfix] 未找到可包装的 async convert_message，媒体/rich_message 增强未启用",
            )

        existing_send_rich = getattr(adapter_cls, "send_rich_message", _MISSING)
        if _is_missing(existing_send_rich) or not callable(existing_send_rich):
            originals["send_rich_message"] = _MISSING
            wrappers["send_rich_message"] = _make_send_rich_message_wrapper()
        else:
            _logger_call(
                "debug",
                "[tayhetgfix] 核心已提供 send_rich_message，保留核心实现",
            )

        record = {"originals": originals, "wrappers": wrappers, "runtime": runtime}
        try:
            for name, wrapper in wrappers.items():
                setattr(adapter_cls, name, wrapper)
            setattr(adapter_cls, "__tg_hardening_record__", record)
            setattr(adapter_cls, "__tg_hardened__", True)
        except Exception:
            for name, original in originals.items():
                if _is_missing(original):
                    try:
                        delattr(adapter_cls, name)
                    except AttributeError:
                        pass
                else:
                    setattr(adapter_cls, name, original)
            _restore_package_registry(package, self._previous_package_registry, self._proxy_owner)
            self._previous_package_registry = None
            raise

        _claim_package_registry(package, self._proxy_owner)
        proxy_applied = _configure_httpx_proxy(
            proxy,
            proxy_enabled,
            self._proxy_owner,
            previous_adapter_owner,
            _proxy_timeout(
                _read_config(self.config, "proxy_connect_timeout", DEFAULT_PROXY_CONNECT_TIMEOUT),
                DEFAULT_PROXY_CONNECT_TIMEOUT,
            ),
            _proxy_timeout(
                _read_config(self.config, "proxy_read_timeout", DEFAULT_PROXY_READ_TIMEOUT),
                DEFAULT_PROXY_READ_TIMEOUT,
            ),
        )
        runtime["proxy_applied"] = proxy_applied
        setattr(adapter_cls, "__tayhetgfix_adapter_owner__", self._proxy_owner)
        self._adapter_cls = adapter_cls
        self.status = "applied"
        _logger_call(
            "info",
            "[tayhetgfix] 补丁已应用（version=%s, allowed_updates=%s, max_restart=%d, proxy=%s）",
            self.version,
            list(allowed) if allowed is not None else "PTB 默认",
            runtime["max_restart"],
            _proxy_log_target(proxy) if proxy else "直连",
        )

    def _deactivate(self) -> None:
        adapter_cls = self._adapter_cls
        if adapter_cls is not None:
            current_owner = getattr(adapter_cls, "__tayhetgfix_adapter_owner__", None)
            if current_owner is self._proxy_owner:
                _unpatch_adapter(adapter_cls)
                try:
                    delattr(adapter_cls, "__tayhetgfix_adapter_owner__")
                except AttributeError:
                    pass
            _restore_httpx_proxy_patch(self._proxy_owner)
            self._adapter_cls = None
        else:
            _restore_httpx_proxy_patch(self._proxy_owner)
        if self._package is not None:
            _restore_package_registry(self._package, self._previous_package_registry, self._proxy_owner)
            self._package = None
            self._previous_package_registry = None

    async def terminate(self) -> None:
        self._deactivate()
        self.status = "terminated"
        _logger_call("info", "[tayhetgfix] 已还原 Telegram 适配器补丁")


# Name aliases make the class easier to discover in older loader tooling.
TgHardeningPlugin = TelegramHardening

__all__ = [
    "TelegramHardening",
    "TgHardeningPlugin",
    "register_telegram_handler",
    "add_telegram_handler",
    "register_handler",
]
