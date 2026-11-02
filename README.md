# astrbot_plugin_tayhetgfix

这是一个**不修改 AstrBot 安装文件**的 Telegram 适配器运行时补丁插件。

## 功能

插件在 AstrBot 插件初始化阶段导入 `TelegramPlatformAdapter`，在平台实例化前对核心方法做幂等包装：

1. `_build_application`：保留核心注册逻辑后，补注册运行时 handler，并在应用重建后重新安装 polling 守卫。
2. `run`：外层捕获未处理异常，按 `max_restart` 重建 application 后重试。
3. `message_handler`：最后一道异常隔离，单条消息转换/处理异常不会直接冒泡到 Telegram 适配器。
4. `convert_message`（核心存在且为 async 时）：
   - 语音、音频、图片、贴纸、文档、视频、视频笔记下载失败时保留消息并添加空媒体占位；
   - 核心在 `file_path is None` 时直接返回的文档/视频也会补空占位；
   - 解析入站 `rich_message`，兼容字符串 `text`、嵌套 `blocks`、表格 `cells`、`api_kwargs`，并转换为可处理的纯文本。
5. 如果核心没有 `send_rich_message`，插件会注入该方法，复用适配器已有 bot client 调用 `sendRichMessage`；如果核心已经提供，则保留核心实现。
6. **出站代理注入**（v1.2.0 新增）：给 `telegram.request.HTTPXRequest` 显式传入代理，解决墙内轮询 `httpx.ConnectError`；支持 PTB 的 `proxy` 与旧版 `proxy_url` 参数形式。

polling 默认保留 python-telegram-bot 自身的 `allowed_updates` 行为；只有显式配置更新类型时才覆盖。所有补丁都带有锚点检查、版本跳过、幂等标记，并在 `terminate()` 中恢复原始方法或删除插件注入的方法。

## 为什么需要出站代理注入

AstrBot 的 `core_lifecycle.py` 只会把 `http_proxy` 配置写成进程环境变量：

```python
os.environ["https_proxy"] = proxy_config
os.environ["http_proxy"] = proxy_config
```

但 `python-telegram-bot` 的 `HTTPXRequest` 是否读取这些变量，取决于底层 httpx 的 `trust_env` 行为。在部分版本组合下它并不生效，结果就是：

```text
telegram.error.NetworkError: httpx.ConnectError:
  Telegram polling request failed
  Telegram polling crashed with exception ... Retrying in 5.0s
```

而 mihomo 侧**完全看不到**任何 telegram 流量——请求在容器内就失败了，根本没到代理。

本插件在 `HTTPXRequest.__init__` 上做一次幂等包装，为每个新建的请求对象显式补上 `proxy`（以及可选超时）。只注入调用方**没有**提供的参数，且同时考虑位置参数与关键字参数，因此不会覆盖显式配置、也不会触发 `got multiple values`。

## 安装

将本目录复制为：

```text
data/plugins/astrbot_plugin_tayhetgfix/
```

然后在 AstrBot WebUI 中启用插件并重载。插件应在 Telegram 平台实例化之前加载；日志中会打印：

```text
[tayhetgfix] 补丁已应用 (version=..., allowed_updates=..., max_restart=..., proxy=http://host:port)
[tayhetgfix] 已注入 Telegram 出站代理: http://host:port (认证信息已隐藏)
```

如果版本不兼容或目标方法锚点发生变化，插件会打印 warning 并跳过，不会直接修改文件。

### 重要：必须重启 AstrBot，而不只是重载插件

代理补丁作用在 `HTTPXRequest.__init__` 上，只对**之后新建**的请求对象生效。如果 Telegram 平台已经在运行，它手里的 bot 请求对象是旧的，不会自动获得代理。

所以首次启用后请**重启容器/进程**：

```bash
docker restart astrbot
```

## 配置

- `enabled`：总开关。
- `proxy_enabled`：是否注入出站代理，默认开启。
- `proxy`：代理地址。**留空则自动读取** AstrBot 的 `http_proxy`/`https_proxy` 环境变量。
- `proxy_connect_timeout` / `proxy_read_timeout`：经代理访问 Telegram 的连接/读取超时，默认各 20 秒。
- `allowed_updates`：可选的 polling 更新类型覆盖；留空保持 python-telegram-bot 默认行为（默认会过滤 `chat_member`、`message_reaction` 和 `message_reaction_count`）。填写逗号/空格分隔的类型后只接收所列更新。
- `max_restart`：外层 `run()` 异常的最大重试次数，默认 3。
- `restart_delay`：重建前等待秒数。
- `media_download_fallback`：媒体下载失败时是否保留消息并添加空占位，默认开启。
- `rich_message_fallback`：是否从入站 `rich_message` 提取纯文本，默认开启。
- `incompatible_versions`：需要跳过的版本列表，支持 `4.28`、`4.28.*` 等写法。

### 代理地址怎么填

| 部署方式 | 建议值 |
|---|---|
| Docker 容器访问宿主机 mihomo | `http://172.18.0.1:7890`（即容器网关） |
| Docker 且用默认 docker0 网桥 | `http://172.17.0.1:7890` |
| mihomo 与 AstrBot 同容器/同网络命名空间 | `http://127.0.0.1:7890` |

> 注意：`172.18.0.1` 是 Docker compose 动态分配的网关。如果重建过网络（`docker compose down/up`），网段可能变成 `172.19.x`，此时代理会失效。建议在 mihomo 侧把 `bind-address` 改为 `0.0.0.0` 并配合防火墙，或在 compose 里固定 `subnet`。

## 给其他插件注册 PTB handler

其他插件可以直接导入本插件的公共 helper：

```python
from astrbot_plugin_tayhetgfix.main import register_telegram_handler
from telegram.ext import CallbackQueryHandler

register_telegram_handler(CallbackQueryHandler(my_callback), group=120)
```

注册表也会在内存中暴露为 `astrbot.core.platform.sources.telegram._cs_extra_handlers`，用于兼容原来依赖该名称的插件；它不是核心目录中的文件。

## 富文本说明

历史补丁里的富文本展平逻辑把 `heading.text` 假定为列表，并且没有处理表格 `cells`。本插件使用递归解析：

- `text: "明日课表"`；
- `blockquote.blocks` / `details.blocks`；
- `table.cells`，输出为用 ` | ` 分隔的行；
- 单元格中的 `{"text": {"type": "bold", "text": "..."}}`；
- PTB 的 `to_dict()` 和 `api_kwargs` 两种字段位置。

## 限制

- 媒体容错采用“先调用原始 `convert_message`，失败后隐藏失败媒体字段重试，再补空组件”的包装方式，因此不替换核心整段逻辑；如果核心版本改变了 converter 的调用约定，锚点/冒烟测试需要重新确认。
- `max_restart` 只限制从核心 `run()` 冒泡到插件包装层的异常；AstrBot 自身已经在核心内部处理的轮询重试仍由核心逻辑负责。
- 富文本只提取为纯文本，不会把表格重新渲染为 AstrBot 组件。
- 代理注入不处理 SOCKS5 的额外依赖；如需 `socks5://`，须在镜像内安装 `python-telegram-bot[socks]`。

## 排障

确认代理是否真的生效，在容器内执行：

```bash
docker exec astrbot python3 -c "
from telegram.request import HTTPXRequest
r = HTTPXRequest()
print('proxy =', r._proxy_url if hasattr(r,'_proxy_url') else getattr(r,'proxy',None))
"
```

更直接的验证是看 mihomo 日志里是否终于出现 telegram 流量：

```bash
journalctl -u mihomo -f | grep -i telegram
```

只要出现 `--> api.telegram.org:443 match Match using PROXY[...]`，就说明代理链路已打通。
