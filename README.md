<h1 align="center">Telegram 适配器自愈补丁</h1>

<p align="center">
  <strong>为 AstrBot Telegram 适配器提供可卸载的运行时兼容补丁、媒体容错与出站代理注入。</strong>
</p>

<p align="center">
  <a href="https://github.com/tangshusheng/astrbot_plugin_tayhetgfix/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/tangshusheng/astrbot_plugin_tayhetgfix?style=for-the-badge&label=release" /></a>
  <a href="https://github.com/tangshusheng/astrbot_plugin_tayhetgfix/blob/main/LICENSE"><img alt="License: AGPL-3.0" src="https://img.shields.io/badge/license-AGPL--3.0-blue?style=for-the-badge" /></a>
  <a href="https://github.com/Soulter/AstrBot"><img alt="AstrBot plugin" src="https://img.shields.io/badge/AstrBot-plugin-00A86B?style=for-the-badge" /></a>
  <a href="https://github.com/tangshusheng/astrbot_plugin_tayhetgfix/stargazers"><img alt="GitHub stars" src="https://img.shields.io/github/stars/tangshusheng/astrbot_plugin_tayhetgfix?style=for-the-badge&color=gold" /></a>
</p>

本插件面向 AstrBot Telegram 适配器，在不修改 AstrBot 安装文件的前提下，提供幂等、可卸载的运行时兼容补丁。适用于希望改善 Telegram 轮询稳定性、媒体转换容错及富文本消息处理的部署。

## 目录

- [功能一览](#功能一览)
- [安装与快速配置](#安装与快速配置)
- [常用配置](#常用配置)
- [代理配置](#代理配置)
- [handler 扩展](#handler-扩展)
- [限制与排障](#限制与排障)
- [作者与许可证](#作者与许可证)

## 功能一览

| 能力 | 说明 |
| --- | --- |
| **Telegram 运行时兼容** | 在平台实例化前对 Telegram 适配器方法进行锚点检查后再包装；不直接修改 AstrBot 核心文件。 |
| **轮询稳定性** | 对未处理的 `run()` 异常提供有限次数的重建重试，并在 application 重建后恢复 polling 守卫。 |
| **消息异常隔离** | 为单条消息处理增加异常边界，避免个别转换/处理异常直接中断适配器。 |
| **媒体下载容错** | 语音、音频、图片、贴纸、文档、视频和视频笔记下载失败时，尽量保留消息并补空媒体占位。 |
| **入站富文本兼容** | 递归提取 `rich_message` 文本，兼容嵌套 blocks、表格 cells、`to_dict()` 与 `api_kwargs`。 |
| **PTB handler 注册** | 提供公共 helper，供其他插件向 Telegram bot 注册额外的 python-telegram-bot handler。 |
| **出站代理注入** | 为新建的 `HTTPXRequest` 显式补充代理；兼容 PTB 的 `proxy` 与旧版 `proxy_url` 参数，并避免覆盖调用方显式传参。 |

补丁支持幂等安装与 `terminate()` 清理；遇到不兼容版本或关键方法锚点变化时会跳过并记录日志。`allowed_updates` 默认保持 python-telegram-bot 自身行为，只有填写配置时才覆盖。

## 安装与快速配置

1. 在 AstrBot 插件管理中搜索 **Telegram 适配器自愈补丁** 安装；或从[最新 Release](https://github.com/tangshusheng/astrbot_plugin_tayhetgfix/releases/latest)下载 ZIP 后导入。
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
| `max_restart` | `3` | 插件包装层捕获到 `run()` 异常后的最大重启次数。 |
| `restart_delay` | `1` 秒 | 重建 application 前的等待时间。 |
| `media_download_fallback` | `true` | 媒体下载失败时保留消息并添加空占位。 |
| `rich_message_fallback` | `true` | 从入站 `rich_message` 提取纯文本。 |
| `incompatible_versions` | 空 | 跳过补丁的 AstrBot 版本；支持 `4.28`、`4.28.*` 等写法。 |

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
- `max_restart` 只限制从核心 `run()` 冒泡到插件包装层的异常；AstrBot 核心已自行处理的轮询重试仍由核心负责。
- 富文本 fallback 将内容提取为纯文本，不会把表格重新渲染成 AstrBot 组件。
- 代理补丁只影响补丁生效后新创建的请求对象；修改代理后建议重启 AstrBot。

确认代理是否生效，可查看 mihomo 日志是否出现发往 Telegram 的连接，或在容器内检查新建的 `HTTPXRequest` 的代理配置。请勿在公开 issue 或日志中粘贴包含代理用户名、密码或 token 的配置。

## 作者与许可证

Copyright (C) 2026 tangshusheng。项目基于 [GNU Affero General Public License v3.0（AGPL-3.0）](LICENSE)授权。完整许可证文本见 [LICENSE](LICENSE)。
