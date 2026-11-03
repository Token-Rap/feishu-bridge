#!/usr/bin/env python3
"""fleet_bridge 的高危操作分类 (纯逻辑, 无重依赖, 独立可测).

策略 = 用户选的「写也放行, 仅高危审批」: 默认放行, 仅以下弹飞书审批
  - Bash 命中高危命令模式 (push/删除/launchctl/包安装/数据上传/提权 等)
  - 写入敏感路径 (.env / state / launchd / 凭证) 或仓库外
  - 读取敏感文件 (可能外泄凭证)
其余 (普通源码读写 / Grep / Glob / WebFetch / MCP …) 一律放行.
"""
from __future__ import annotations

import re
from pathlib import Path

# Bash 高危命令模式
BASH_HIGH_RISK = [
    r"\brm\s+-", r"\brm\s+/", r"\bgit\s+push\b", r"\bgit\s+reset\b",
    r"\bgit\s+clean\b", r"\bgit\s+checkout\s+\.", r"\bgit\s+restore\b",
    r"\bgh\s+(pr\s+merge|release|repo\s+delete)", r"\blaunchctl\b",
    r"\b(sudo|chmod|chown|dd|mkfs|shutdown|reboot)\b", r"\b(kill|pkill|killall)\b",
    r"\b(pip|pip3|npm|brew|gem|cargo)\s+(install|uninstall|publish)",
    r"\bcurl\b.*\s(-d|-T|--data|--upload)", r"\bwget\b.*--post",
    r"\b(scp|rsync|nc|ncat)\b", r">\s*/(etc|usr|bin|System)", r":\(\)\s*\{",
]
# 敏感路径片段 (写或读命中 → 高危)
SENSITIVE_PATH = (
    ".env", ".credentials", "credentials.json", "/state/", "scripts/launchd",
    "launchagents", "/.claude", "id_rsa", ".ssh", ".aws", "secret",
)


def _resolve(path: str, repo: str) -> str:
    try:
        p = Path(path)
        if not p.is_absolute():
            p = Path(repo) / p
        return str(p.resolve())
    except Exception:  # noqa: BLE001
        return path


def classify_risk(tool: str, inp: dict, repo: str) -> tuple[bool, str, str]:
    """返回 (是否高危, 一句话原因, 给用户看的操作摘要)."""
    if tool == "Bash":
        cmd = str(inp.get("command", ""))
        summary = cmd if len(cmd) <= 300 else cmd[:300] + " …"
        for pat in BASH_HIGH_RISK:
            if re.search(pat, cmd):
                return True, f"命令命中高危模式 /{pat}/", summary
        return False, "", summary
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = inp.get("file_path") or inp.get("path") or inp.get("notebook_path") or ""
        rp = _resolve(str(path), repo).lower()
        summary = f"{tool} → {path}"
        if any(s in rp for s in SENSITIVE_PATH):
            return True, "写入敏感路径 (凭证/state/launchd 等)", summary
        if not rp.startswith(str(Path(repo).resolve()).lower()):
            return True, "写入仓库外路径", summary
        return False, "", summary          # 普通源码改动 → 放行
    if tool == "Read":
        path = str(inp.get("file_path") or inp.get("path") or "")
        if any(s in _resolve(path, repo).lower() for s in SENSITIVE_PATH):
            return True, "读取敏感文件 (可能外泄凭证)", f"Read → {path}"
        return False, "", f"Read → {path}"
    return False, "", f"{tool}"            # 其它工具放行
