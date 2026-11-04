<h1 align="center">Telegram 适配器自愈补丁</h1>

<p align="center">
  <strong>为 AstrBot Telegram 适配器提供轮询掉线检测与自动恢复、媒体容错及出站代理注入。</strong>
</p>

<p align="center"><strong>正式版 v1.3.0</strong> · <a href="CHANGELOG.md">更新日志</a></p>

<p align="center">
  <a href="https://github.com/Tayhe0611/astrbot_plugin_tayhetgfix/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/Tayhe0611/astrbot_plugin_tayhetgfix?style=for-the-badge&label=release" /></a>
  <a href="https://github.com/Tayhe0611/astrbot_plugin_tayhetgfix/blob/main/LICENSE"><img alt="License: AGPL-3.0" src="https://img.shields.io/badge/license-AGPL--3.0-blue?style=for-the-badge" /></a>
  <a href="https://github.com/Soulter/AstrBot"><img alt="AstrBot plugin" src="https://img.shields.io/badge/AstrBot-plugin-00A86B?style=for-the-badge" /></a>
  <a href="https://github.com/Tayhe0611/astrbot_plugin_tayhetgfix/stargazers"><img alt="GitHub stars" src="https://img.shields.io/github/stars/Tayhe0611/astrbot_plugin_tayhetgfix?style=for-the-badge&color=gold" /></a>
</p>

本插件面向 AstrBot Telegram 适配器，在不修改 AstrBot 安装文件的前提下，提供幂等、可卸载的运行时兼容补丁。适用于希望改善 Telegram 轮询稳定性、媒体转换容错及富文本消息处理的部署。

## 目录

- [功能一览](#功能一览)
- [安装与快速配置](#安装与快速配置)
- [常用配置](#常用配置)
- [掉线检测与自动恢复](#掉线检测与自动恢复)
- [代理配置](#代理配置)
- [handler 扩展](#handler-扩展)
- [限制与排障](#限制与排障)
- [更新日志](CHANGELOG.md)
- [作者与许可证](#作者与许可证)

## 功能一览

| 能力 | 说明 |
| --- | --- |
| **Telegram 运行时兼容** | 在平台实例化前对 Telegram 适配器方法进行锚点检查后再包装；不直接修改 AstrBot 核心文件。 |
| **掉线检测与自动恢复** | 观察真实 `getUpdates` 成功心跳；连续网络故障或轮询卡住时，通过 AstrBot 原生恢复接口请求重建 application/HTTP 客户端，带冷却且不另起轮询。 |
| **崩溃恢复** | 对未处理的 `run()` 异常提供有限次数的重建重试，并在 application 重建后重新接入看门狗。 |
| **消息异常隔离** | 为单条消息处理增加异常边界，避免个别转换/处理异常直接中断适配器。 |
| **媒体下载容错** | 语音、音频、图片、贴纸、文档、视频和视频笔记下载失败时，尽量保留消息并补空媒体占位。 |
| **入站富文本兼容** | 递归提取 `rich_message` 文本，兼容嵌套 blocks、表格 cells、`to_dict()` 与 `api_kwargs`。 |
| **PTB handler 注册** | 提供公共 helper，供其他插件向 Telegram bot 注册额外的 python-telegram-bot handler。 |
| **出站代理注入** | 为新建的 `HTTPXRequest` 显式补充代理；兼容 PTB 的 `proxy` 与旧版 `proxy_url` 参数，并避免覆盖调用方显式传参。 |

补丁支持幂等安装与 `terminate()` 清理；遇到不兼容版本或关键方法锚点变化时会跳过并记录日志。`allowed_updates` 默认保持 python-telegram-bot 自身行为，只有填写配置时才覆盖。

## 安装与快速配置

1. 在 AstrBot 插件管理中搜索 **Telegram 适配器自愈补丁** 安装；或从[最新 Release](https://github.com/Tayhe0611/astrbot_plugin_tayhetgfix/releases/latest)下载 ZIP 后导入。
2. 启用插件并检查配置；需要代理时确认 `proxy_enabled` 已开启，并设置可从 AstrBot 容器访问的代理地址，或留空使用 AstrBot 的 `http_proxy` / `https_proxy` 环境变量。
3. 保存后重载插件。首次启用或修改代理后，建议重启 AstrBot，使 Telegram 平台重新创建 HTTP 请求对象并应用代理。

## 常用配置

| 配置项 | 默认值 | 用途 |
| --- | --- | --- |
| `enabled` | `true` | 总开关；关闭时清理本插件持有的运行时补丁。 |
| `proxy_enabled` | `true` | 是否向新建的 Telegram `HTTPXRequest` 注入代理。 |
| `proxy` | 空 | 代理地址；留空时读取 AstrBot 的 `http_proxy` / `https_proxy` 环境变量。 |
| `proxy_connect_timeout` | `20` 秒 | 代理连接超时。 |
| `proxy_read_timeout` | `20` 秒 | 代理读取超时。 |
| `allowed_updates` | 空 | 留空保持 PTB 默认行为；填写逗号或空格分隔的更新类型后，仅接收列出的更新。 |
| `watchdog_enabled` | `true` | 开启轮询掉线看门狗；核心不具备安全恢复接口时自动跳过。 |
| `watchdog_interval` | `15` 秒 | 状态检查间隔，最小 1 秒；不产生额外探测请求。 |
| `watchdog_timeout` | `120` 秒 | 无成功轮询超时，最小 30 秒；单次请求预算至少为 long-poll timeout + 30 秒。 |
| `watchdog_failures` | `3` | 连续轮询网络失败阈值，最小 1；任意成功轮询（含空结果）清零。 |
| `watchdog_cooldown` | `120` 秒 | 两次看门狗恢复请求的最小间隔，最小 30 秒；跨 application 重建保留。 |
| `max_restart` | `3` | 插件包装层捕获到 `run()` 异常后的最大重启次数，与看门狗阈值独立。 |
| `restart_delay` | `1` 秒 | 重建 application 前的等待时间。 |
| `media_download_fallback` | `true` | 媒体下载失败时保留消息并添加空占位。 |
| `rich_message_fallback` | `true` | 从入站 `rich_message` 提取纯文本。 |
| `incompatible_versions` | 空 | 跳过补丁的 AstrBot 版本；支持 `4.28`、`4.28.*` 等写法。 |

## 掉线检测与自动恢复

默认开启，建议先使用 `15 / 120 / 3 / 120`（检查间隔 / 无成功轮询超时 / 连续失败次数 / 冷却秒数）。代理抖动明显、经常误触发时，可把超时和冷却调为 `180`、`300`；不要过度缩短，以免慢链路频繁重建。

- **检测依据**：观察核心正在执行的 `getUpdates`。成功返回空列表同样表示在线；长时间没有用户消息不会触发重启。`getMe` 成功也不会被当成轮询恢复。
- **触发条件**：连续网络失败达到阈值，**或**超过有效超时没有成功轮询。长轮询服务端等待时间 + 30 秒大于配置超时时，以更长预算为准；检查间隔和核心重试/关闭耗时会增加实际恢复延迟。
- **恢复方式**：设置 AstrBot 原生恢复 Event，由它现有的 `run()` 依次停止旧 application、重建 HTTP 客户端、恢复轮询。不另起 `run()`、不额外调用 `getUpdates`。关闭未完成或旧轮询任务仍存活时拒绝重建，优先避免双轮询。
- **防重启风暴**：同一恢复请求未消费时不重复请求，重建后仍保留冷却。Token 无效、权限、Webhook/重复实例冲突、BadRequest 等非网络错误暂停看门狗重启；Telegram `RetryAfter` 等待交给 PTB。请按日志修正配置，不能靠重建解决。
- **恢复确认**：日志 `请求核心重建` 只表示已发起请求；只有新的真实 `getUpdates` 正常返回后才记录 `轮询已恢复`。代理仍不可用时，重建本身无法修好代理。

插件重载时会尝试通过 `context.get_all_platforms()` 接入已运行的 Telegram 实例。首次接管尚未使用本插件错误回调的轮询，会请求核心进行一次受控重建，使新轮询采用看门狗策略；重复重载不会再次请求。核心不提供该接口时，需要重启平台或 AstrBot。卸载仅取消并等待插件自己的监控任务、恢复本插件持有的包装，不主动停止核心轮询；已经进入的请求仍由核心 polling 持有并按原取消/超时流程收尾。

**兼容边界**：看门狗要求核心存在 `_polling_recovery_requested`、`_recreate_application` 等原生恢复接口（以实际接口检测为准；本地样本为 AstrBot 4.28.0）。旧核心跳过看门狗，其他补丁仍可使用。只监测已进入 polling 的阶段，**不覆盖启动时 initialize/getMe、注册命令或 polling bootstrap 挂起**；这些情况请检查代理、重启平台并保留日志。看门狗不会重启 AstrBot 进程或代理服务。

## 代理配置

AstrBot 通常将 `http_proxy` / `https_proxy` 写入进程环境变量，但部分 `python-telegram-bot` 与 httpx 版本组合不一定会按预期读取这些变量。本插件会在 `HTTPXRequest.__init__` 上做幂等包装，为新创建的请求对象显式补代理和可选超时；不会覆盖调用方已传入的代理，也会处理位置参数与关键字参数冲突。

常见 Docker 部署地址示例：

| 部署方式 | 代理地址示例 |
| --- | --- |
| Docker 容器访问宿主机 mihomo | `http://172.18.0.1:7890`（以实际容器网关为准） |
| 默认 docker0 网桥 | `http://172.17.0.1:7890` |
| mihomo 与 AstrBot 在同一网络命名空间 | `http://127.0.0.1:7890` |

> Docker Compose 网络重建后网关地址可能变化。请确认 mihomo 监听地址、防火墙规则和容器网段；不要在公开网络中无保护地暴露代理端口。

日志仅显示代理 scheme、主机和端口，认证信息会隐藏。SOCKS5 代理需要在运行环境安装 `python-telegram-bot[socks]` 对应依赖。

## handler 扩展

其他插件可以使用公共 helper 注册 PTB handler：

```python
from astrbot_plugin_tayhetgfix.main import register_telegram_handler
from telegram.ext import CallbackQueryHandler

register_telegram_handler(CallbackQueryHandler(my_callback), group=120)
```

注册表也会在内存中暴露为 `astrbot.core.platform.sources.telegram._cs_extra_handlers`，用于兼容依赖该名称的插件；这不是 AstrBot 核心目录中的文件。

## 限制与排障

- 媒体容错在核心转换失败后进行回退处理；如果 AstrBot 版本改变 converter 调用约定，插件可能因锚点检查失败而跳过，需要重新验证兼容性。
- `max_restart` 只限制从核心 `run()` 冒泡到插件包装层的异常，不限制看门狗请求总次数。看门狗通过 `watchdog_cooldown` 限频；AstrBot 核心自行处理的其他崩溃重试仍由核心负责。
- 若日志出现 `Conflict`，先排查相同 Token 是否被两个 AstrBot/其他 bot 程序同时轮询；如启用了 Webhook，先确认并停用冲突的消费方式。不要通过降低冷却反复重建。
- 富文本 fallback 将内容提取为纯文本，不会把表格重新渲染成 AstrBot 组件。
- 代理补丁只影响补丁生效后新创建的请求对象；修改代理后建议重启 AstrBot。

确认代理是否生效，可查看 mihomo 日志是否出现发往 Telegram 的连接，或在容器内检查新建的 `HTTPXRequest` 的代理配置。请勿在公开 issue 或日志中粘贴包含代理用户名、密码或 token 的配置。

## 作者与许可证

Copyright (C) 2026 tangshusheng。项目基于 [GNU Affero General Public License v3.0（AGPL-3.0）](LICENSE)授权。完整许可证文本见 [LICENSE](LICENSE)。
