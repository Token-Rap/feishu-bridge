# 最小舰队：fleet-manager + 两个 worker

三个飞书 bot 共用本仓代码，但各有 home、飞书应用和工作目录。这里的 Router/Worker 是**工作约定**：当前公开包没有开箱即用的跨 bot 自动分派和结果回传工具。下面用人转交任务与结果，验证多实例；fleet.json 和角色提示不会自动完成编排。

## 配置和启动

在仓库根目录执行，先准备 Python、已登录的 Claude Code 或其他已配置的 agent，以及三个飞书自建应用；配置消息事件订阅、长连接和必要权限。各应用的凭证、允许操作的用户 open_id、工作目录和 agent 工具权限需分别配置。工作目录应存在，且仅授予相应 bot 所需权限。

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
mkdir -p ~/.feishu-example-manager ~/.feishu-example-jobhunt ~/.feishu-example-backend
cp examples/fleet-manager-min/homes/fleet-manager.env.example ~/.feishu-example-manager/.env
cp examples/fleet-manager-min/homes/jobhunt.env.example ~/.feishu-example-jobhunt/.env
cp examples/fleet-manager-min/homes/backend.env.example ~/.feishu-example-backend/.env
cp examples/fleet-manager-min/fleet.json.example ~/.feishu-example-manager/fleet.json
chmod 600 ~/.feishu-example-manager/.env ~/.feishu-example-jobhunt/.env ~/.feishu-example-backend/.env
```

编辑三个 `.env`，分别替换 `FEISHU_BRIDGE_APP_ID`、`FEISHU_BRIDGE_APP_SECRET`、`FEISHU_BRIDGE_OPEN_ID`、`FEISHU_BRIDGE_REPO`，仓库路径用实际绝对路径；不要提交凭证。编辑复制后的 fleet.json，使 `home` 对应各实例，`repo` 对应各自工作目录。名册用于管理，不会创建 bot 或配置飞书应用。确保启动 shell 没有设置覆盖这些字段的 `FLEET_BRIDGE_*` / `FEISHU_BRIDGE_*` 环境变量。

```bash
export FEISHU_BRIDGE_VENV="$PWD/.venv/bin/python"
./scripts/fleet_ctl.sh start ~/.feishu-example-manager
./scripts/fleet_ctl.sh start ~/.feishu-example-jobhunt
./scripts/fleet_ctl.sh start ~/.feishu-example-backend
./scripts/fleet_ctl.sh status ~/.feishu-example-manager
```

也可前台启动单个实例：`.venv/bin/python -m feishu_bridge.bridge.fleet_bridge ~/.feishu-example-manager`。同一实例不要同时用两种方式启动，也不要与自动拉起它的进程管理器并用。检查各 home 的 `bridge.log`，再通过飞书向各 bot 发消息验证连接；进程存在不代表消息已送达。

## 管理与一次任务

`--fleet` 必须放在子命令前，指向上面复制并编辑的名册：

```bash
.venv/bin/python -m feishu_bridge.admin.fleet_admin --fleet ~/.feishu-example-manager/fleet.json list
.venv/bin/python -m feishu_bridge.admin.fleet_admin --fleet ~/.feishu-example-manager/fleet.json config
.venv/bin/python -m feishu_bridge.admin.fleet_admin --fleet ~/.feishu-example-manager/fleet.json set-model fleet-manager default
.venv/bin/python -m feishu_bridge.admin.fleet_admin --fleet ~/.feishu-example-manager/fleet.json set-effort backend high
```

`set-model` 可将 `default` 换成所用 agent 支持的模型 ID；`set-effort` 可用 `low`、`medium`、`high`、`xhigh`、`max` 或 `default`。修改写入对应 home 的运行时配置，在下一轮读取；具体模型和参数能否使用取决于所选 agent 和账户。

例如，人在飞书告诉 fleet-manager：“你负责协调，将职位调研交给 jobhunt、技术核查交给 backend，收到结果后汇总回复。”Router 输出两份带任务范围和验收条件的委派；人分别转发给 worker，worker 在各自会话和工作目录执行并返回结果，人再把结果转交 Router，Router 汇总并回复人。可在各自工作目录的 `CLAUDE.md` 写入角色约定（Claude 路径会读取）；执行工具和访问权限须由实际 agent 环境配置。若以后接入自己的传递机制，仍需验证目标、送达、结果与回报，不能把本示例当作自动编排。

## 与 Super-agent 的对照

参考 [DeepSeek Harness 的 dsh-superagent-prototype](https://github.com/deepseek-ai/dsh-superagent-prototype)：这是架构探索的研究原型，不是本仓依赖。本例借鉴角色和控制回路，能力边界如下：

- **Router / Worker**：fleet-manager 负责交互、全局协调和分派约定，可选较轻模型；jobhunt、backend 分别执行领域任务，隔离 home 和会话，配置各自所需工具。实际分派和回报在本例由人转交。
- **Trigger**：飞书消息及群事件可触发交互，属于事件驱动入口；是否主动处理某类事件取决于实际配置和代码，不等于任意事件都会生成任务。
- **Watchdog**：`feishu_bridge.watchdog` 是外部控制回路，检查心跳及卡住的回合；其恢复依赖 macOS launchd 服务。
- **连续 / 高效 / 主动 / 持久**：多实例 home 和 sessions 保存各自运行与会话状态，managed 提供受管理的运行时，watchdog 提供健康检查；分工和模型配置有助于效率，事件入口可支持主动工作，但本例没有自动规划、自动分派或持续运行保证。

可选：在 macOS 上执行一次 watchdog 检查（会尝试恢复符合条件的已配置 launchd 服务；定期运行需自行配置调度）：

```bash
FLEET_REGISTRY_JSON="$HOME/.feishu-example-manager/fleet.json" .venv/bin/python -m feishu_bridge.watchdog.fleet_watchdog
```

仅用 `fleet_ctl.sh` 手动启动时，不要假设 watchdog 能重启这些进程；网关告警和重启还需要根 README 所述的额外配置。
