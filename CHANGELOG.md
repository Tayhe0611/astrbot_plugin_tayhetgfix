# 更新日志

本文件记录面向用户的重要变化。版本采用语义化版本号。

## [1.3.0] - 2026-11-04

### 新增

- 新增 Telegram 轮询看门狗，基于核心实际的 `getUpdates` 成功心跳判断在线状态，连续网络失败或轮询无响应达到阈值时请求恢复。
- 新增看门狗配置：`watchdog_enabled`、`watchdog_interval`、`watchdog_timeout`、`watchdog_failures`、`watchdog_cooldown`。
- 新增已运行 Telegram 平台的热接管；在已有轮询使用旧错误回调时，通过 AstrBot 原生生命周期执行一次受控重建。
- 新增对空 `getUpdates` 结果、长轮询超时、Telegram `RetryAfter`、多实例隔离及插件热加载的处理。

### 安全与兼容

- 恢复交由 AstrBot 原生生命周期处理，不启动第二个轮询任务，也不发起额外探测请求。
- 旧 application 停止未完成或轮询任务仍运行时拒绝重建；Token、权限、冲突及协议错误不会触发网络故障重启。
- 监控仅覆盖 polling 阶段；核心缺少原生恢复接口时自动跳过看门狗。
- 插件卸载时仅清理本插件的监控和运行时包装，不主动停止核心轮询。

### 修复与验证

- 保留并扩展既有代理注入、媒体下载 fallback、富文本兼容和可卸载补丁。
- 增加生命周期、看门狗和真实 `python-telegram-bot 22.7` 离线模拟集成测试。
