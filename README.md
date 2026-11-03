# feishu-bridge

飞书与 Claude Code、Codex 之间的多实例网关。通过飞书自建应用的 WebSocket 长连接接收消息，将任务交给对应的本地 agent，并以飞书消息和流式卡片返回结果。

## 功能

- 每个 bot 使用独立的 home、配置、运行状态和绑定工作目录；多个实例可共享代码。
- 持久化会话信息，支持续接及会话管理；可在飞书中使用 `/help` 查看命令。
- 支持流式输出、模型和工作参数控制，以及中断正在运行的任务。
- `fleet_admin.py` 提供实例列表、用量、上下文和模型等管理命令；`fleet_ctl.sh` 提供手动进程控制。

## 安装与配置

需要 Python、飞书自建应用及其事件订阅配置，以及所选 agent 的本地安装和认证。使用前请检查相关服务权限与工作目录授权。

```bash
cd ~/path/to/repo
python3 -m venv .venv
.venv/bin/pip install -e .
mkdir -p ~/.fleet-example-bridge
```

在实例 home 的 `.env` 中配置 `FEISHU_BRIDGE_APP_ID`、`FEISHU_BRIDGE_APP_SECRET`、`FEISHU_BRIDGE_OPEN_ID` 和 `FEISHU_BRIDGE_REPO`；值使用自己的飞书应用、允许的发送人和目标仓库。凭证与运行数据应保存在仓库之外。根据所用 agent 配置相应认证，切勿提交凭证。

```bash
FEISHU_BRIDGE_VENV="$PWD/.venv/bin/python" ./scripts/fleet_ctl.sh start ~/.fleet-example-bridge
./scripts/fleet_ctl.sh status ~/.fleet-example-bridge
.venv/bin/python -m feishu_bridge.admin.fleet_admin list
```

命令中的 `example` 是占位符，请替换为实例名称。`fleet_ctl.sh` 是手动管理方式；不要与自动拉起同一实例的进程管理器同时使用。

最小多实例与 Router/Worker 协作示例见 [fleet-manager-min](examples/fleet-manager-min/README.md)。

## 代码与测试

业务模块位于 `src/feishu_bridge/`，以 `feishu_bridge.*` 导入；`scripts/` 存放运维脚本，`tests/` 存放测试，`probes/` 存放独立诊断探针。安装后可用 `.venv/bin/python -m feishu_bridge.bridge.fleet_bridge <home>` 启动网关，或用 `.venv/bin/python -m feishu_bridge.watchdog.fleet_watchdog` 启动 watchdog。迁移工具使用 `.venv/bin/python scripts/migrate_runtime_deepseek.py`。

```text
src/feishu_bridge/
├── __init__.py
├── bridge/       桥接核心、传输、通用工具与前端卡片
├── sessions/     会话、滚动、worktree 与目标控制
├── admin/        管理命令与应用开通
├── watchdog/     看门狗监测与恢复
├── managed/      managed 子进程、运行时、持久化与入口
├── codex/        Codex 审批、归档恢复与存储
└── questions/    Codex 问题流与默认问题
```

每个目录都是 Python 子包；例如使用 `from feishu_bridge.sessions import fleet_sessions` 导入会话模块。`fleet_managed_codex` 归入 managed，因为它服务于 managed 子进程的 Codex 运行时。

测试按主责模块与上述源码子包对应：

```text
tests/
├── conftest.py                 全局 fixture 与测试环境隔离
├── codex_storage_fixture.py    跨模块共享存储 fixture
├── bridge/                     对应 bridge/
├── sessions/                   对应 sessions/
├── admin/                      对应 admin/
├── watchdog/                   对应 watchdog/
├── managed/                    对应 managed/
├── codex/                      对应 codex/ 及其适配器
├── questions/                  对应 questions/
└── common/                     跨模块的锁一致性与测试环境检查
```

跨模块测试按主责归类；`common/` 保存无法归属于单个子包的检查。

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

2026-11-03 在 Python 3.14、claude-agent-sdk 0.2.157 的可编辑安装环境中执行全量测试：1962 passed、5 skipped、1 warning。部分测试依赖额外的外部源码或环境配置，具体以测试文件为准。

watchdog 的网关备用告警需要设置 `FLEET_WATCHDOG_ALERT_BOT`；网关探测与自动重启需要设置 `FLEET_GATEWAY_LAUNCHD_LABEL`。未设置时对应功能关闭；本地哨兵仍会记录 watchdog 故障。`question_defaults_intents.py` 的本地 intent 协议使用 author 值 `local-owner`，它不是身份认证；访问控制依赖私有目录与文件的本地所有权和权限。

### Optional notification hooks

Set `FLEET_NOTIFY_HOOK` to the path of a Python notification script to enable watchdog notifications. It receives `--title`, `--text`, and, for node notifications, `--home`. Without it, node notifications are skipped; watchdog-down alerts can still use the configured gateway and always leave a local sentinel. Monitor that sentinel independently.

Set `FLEET_SESSION_INJECT_HOOK` to a Python script accepting `--bot`, `--from`, and `--text` to notify a domain lead about an interrupted group turn. Without a valid script, the bridge logs the omission and continues; it does not automatically rerun the interrupted work.
