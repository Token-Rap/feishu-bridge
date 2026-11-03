"""环境变量的新旧双轨读取 (去「飞书」命名迁移的阶段 2)。

■ 为什么先做这一件
`FEISHU_BRIDGE_*` → `FLEET_BRIDGE_*` 是整场改名的第一步, 也是**所有后续步骤的安全网**:
这一步落地之后, 各实例的 `.env` 改不改都能跑 —— 于是 launchd label / home 目录 /
Windows 路径那几步就都不是单向门了, 每一步都能独立回滚。

■ 为什么单独一个模块
`fleet_bridge.py` 和 `app_frontend.py` 都要用, 而后者被前者 import ——
放任一边都会造成循环 import 或重复定义。重复定义的坏处不是"多几行", 是**两份会分叉**:
分散定义的 effort 合法值可能不一致。

■ 读取顺序: 新名优先, 旧名回落
**不是** "旧名优先" —— 迁移期两个名字可能同时存在 (.env 已改新名, 而 launchd plist
里还留着旧名), 那时候该生效的是新的那个。

⚠ 这个模块是**过渡性的**: 迁移计划的阶段 7 会删掉回落分支。删的前提是确认所有
`.env` / plist / 脚本都不再写旧名 —— 在那之前删就是让还没迁的实例静默拿到空值。
"""
from __future__ import annotations

import os
from pathlib import Path

_NEW = "FLEET_BRIDGE_"
_OLD = "FEISHU_BRIDGE_"


def env(suffix: str, default: str = "") -> str:
    """读 `FLEET_BRIDGE_<suffix>`; 没有则回落 `FEISHU_BRIDGE_<suffix>`; 都没有给 default。

    传的是**后缀**不是全名 (`env("HOME")` 而不是 `env("FLEET_BRIDGE_HOME")`) ——
    这样调用点根本没有机会只写对一半, 前缀由这里统一拼。
    """
    for prefix in (_NEW, _OLD):
        val = os.getenv(prefix + suffix)
        if val is not None and val != "":
            return val
    return default


def env_any(*names: str, default: str = "") -> str:
    """按顺序试多个**完整变量名**, 返回第一个非空的; 都没有给 default。

    为什么不能都用上面那个 `env(suffix)`: 它假设「新旧名只差前缀、后缀相同」, 而运维层
    那几个不成立 —— `FEISHU_FLEET_JSON` 的新名是 `FLEET_REGISTRY_JSON`, **前后缀都变了**。
    (operator 定的, 理由值得留在这儿: **按它是什么命名, 别按它在哪命名** ——
     名字跟着位置走正是这次改名要修的那个错, 位置还会再变, 而"它是 bot 注册表"不会变。)

    ⚠ 有了它之后**别把 `env(suffix)` 删掉改成到处写全名**: 那个函数的价值正是调用点
    "没有机会只写对一半"。这个是给规律之外的少数几个用的, 不是通用替代。
    """
    for n in names:
        val = os.getenv(n)
        if val:
            return val
    return default


def env_set_with(suffix: str) -> str:
    """这个值实际是从哪个名字读到的 —— 只给日志/诊断用, 不参与判断。

    迁移期最难查的问题是"我明明改了 .env 怎么没生效"; 有这个就能一眼看出进程读的是
    哪一个。返回 "FLEET_BRIDGE_X" / "FEISHU_BRIDGE_X" / "" (都没设)。
    """
    for prefix in (_NEW, _OLD):
        if os.getenv(prefix + suffix):
            return prefix + suffix
    return ""


def fleet_registry_path() -> Path:
    """`fleet.json` (舰队名册) 的位置 —— **只此一份**。

    集中定义注册表路径，确保各调用方使用一致的读取规则。

    环境变量沿用双轨 (新名 `FLEET_REGISTRY_JSON` 优先, 回落 `FEISHU_FLEET_JSON`),
    跟本模块其余部分同一条规矩。
    """
    return Path(env_any("FLEET_REGISTRY_JSON", "FEISHU_FLEET_JSON")
                or str(Path.home() / ".fleet-ops" / "fleet.json"))
