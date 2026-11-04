English | [中文](README.zh-CN.md)

![feishu-bridge](docs/cover.jpg)

# feishu-bridge

Website: https://token-rap.github.io/feishu-bridge/

A multi-instance gateway between Feishu and Claude Code or Codex. It receives messages through a WebSocket connection to a custom Feishu app, passes tasks to the corresponding local agent, and returns results as Feishu messages and streaming cards.

## Features

- Each bot has its own home, configuration, runtime state, and bound working directory; multiple instances can share the code.
- Persists session information and supports resuming and managing sessions; use `/help` in Feishu to see the commands.
- Supports streaming output, model and work parameter controls, and interruption of running tasks.
- `fleet_admin.py` provides management commands for instance lists, usage, context, models, and more; `fleet_ctl.sh` provides manual process control.

## Installation and configuration

You need Python, a custom Feishu app with event subscriptions configured, and a local installation and authentication for your chosen agent. Check the relevant service permissions and working directory authorization before use.

```bash
cd ~/path/to/repo
python3 -m venv .venv
.venv/bin/pip install -e .
mkdir -p ~/.fleet-example-bridge
```

In the instance home’s `.env`, configure `FEISHU_BRIDGE_APP_ID`, `FEISHU_BRIDGE_APP_SECRET`, `FEISHU_BRIDGE_OPEN_ID`, and `FEISHU_BRIDGE_REPO` with your own Feishu app, allowed sender, and target repository values. Keep credentials and runtime data outside the repository. Configure authentication for the agent you use, and never commit credentials.

```bash
FEISHU_BRIDGE_VENV="$PWD/.venv/bin/python" ./scripts/fleet_ctl.sh start ~/.fleet-example-bridge
./scripts/fleet_ctl.sh status ~/.fleet-example-bridge
.venv/bin/python -m feishu_bridge.admin.fleet_admin list
```

`example` in the commands is a placeholder; replace it with the instance name. `fleet_ctl.sh` is for manual management; do not use it alongside a process manager that automatically starts the same instance.

For a minimal multi-instance and Router/Worker collaboration example, see [fleet-manager-min](examples/fleet-manager-min/README.md).

## Code and tests

Application modules are in `src/feishu_bridge/` and are imported as `feishu_bridge.*`; `scripts/` contains operations scripts, `tests/` contains tests, and `probes/` contains standalone diagnostic probes. After installation, start the gateway with `.venv/bin/python -m feishu_bridge.bridge.fleet_bridge <home>`, or start the watchdog with `.venv/bin/python -m feishu_bridge.watchdog.fleet_watchdog`. Run the migration tool with `.venv/bin/python scripts/migrate_runtime_deepseek.py`.

```text
src/feishu_bridge/
├── __init__.py
├── bridge/       Bridge core, transport, shared utilities, and frontend cards
├── sessions/     Sessions, rolling, worktrees, and target control
├── admin/        Management commands and app activation
├── watchdog/     Watchdog monitoring and recovery
├── managed/      Managed subprocesses, runtime, persistence, and entry points
├── codex/        Codex approvals, archive recovery, and storage
└── questions/    Codex question flow and default questions
```

Each directory is a Python subpackage; for example, import the session module with `from feishu_bridge.sessions import fleet_sessions`. `fleet_managed_codex` belongs to managed because it serves the Codex runtime for managed subprocesses.

Tests correspond to the source subpackages above according to their primary responsibility:

```text
tests/
├── conftest.py                 Global fixtures and test environment isolation
├── codex_storage_fixture.py    Shared storage fixture across modules
├── bridge/                     Corresponds to bridge/
├── sessions/                   Corresponds to sessions/
├── admin/                      Corresponds to admin/
├── watchdog/                   Corresponds to watchdog/
├── managed/                    Corresponds to managed/
├── codex/                      Corresponds to codex/ and its adapters
├── questions/                  Corresponds to questions/
└── common/                     Cross-module lock consistency and test environment checks
```

Cross-module tests are classified by primary responsibility; `common/` holds checks that cannot be assigned to a single subpackage.

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

On 2026-11-03, the full test suite was run in an editable installation environment with Python 3.14 and claude-agent-sdk 0.2.157: 1962 passed, 5 skipped, 1 warning. Some tests depend on additional external source code or environment configuration; see the individual test files for details.

The watchdog’s gateway fallback alerts require `FLEET_WATCHDOG_ALERT_BOT`; gateway probing and automatic restarts require `FLEET_GATEWAY_LAUNCHD_LABEL`. The corresponding features are disabled when these are not set; the local sentinel still records watchdog failures. The local intent protocol in `question_defaults_intents.py` uses the author value `local-owner`; this is not authentication. Access control depends on local ownership and permissions of private directories and files.

### Optional notification hooks

Set `FLEET_NOTIFY_HOOK` to the path of a Python notification script to enable watchdog notifications. It receives `--title`, `--text`, and, for node notifications, `--home`. Without it, node notifications are skipped; watchdog-down alerts can still use the configured gateway and always leave a local sentinel. Monitor that sentinel independently.

Set `FLEET_SESSION_INJECT_HOOK` to a Python script accepting `--bot`, `--from`, and `--text` to notify a domain lead about an interrupted group turn. Without a valid script, the bridge logs the omission and continues; it does not automatically rerun the interrupted work.
