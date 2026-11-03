# 测试指南

`tests/` 按 `src/feishu_bridge/` 的功能模块组织：`bridge/`、`sessions/`、`admin/`、`watchdog/`、`managed/`、`codex/` 和 `questions/`。跨模块检查放在 `tests/common/`，共享 fixture 和 `conftest.py` 留在 `tests/` 根目录。

日常改动优先运行受影响的模块，不必默认每次运行全量。例如：

```bash
pytest tests/bridge/
pytest tests/sessions/
pytest tests/admin/
pytest tests/watchdog/
pytest tests/managed/
pytest tests/codex/
pytest tests/questions/
pytest tests/common/
```

跨模块重构、发布前、修改共享 fixture 或 `conftest.py`，以及不确定影响范围时，运行全量：

```bash
pytest
```
