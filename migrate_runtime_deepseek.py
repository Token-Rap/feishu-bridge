#!/usr/bin/env python3
"""runtime.json 模型覆盖重置 —— 让 bot 跟随新默认 deepseek-flash (backend/T-deepseek-model)。

默认 **dry-run**: 只列出每个 bot 会怎么改。`--apply` 才写; 写前把每个 runtime.json
备份成 runtime.json.bak-<YYYYmmdd-HHMMSS>, 写走 fleet_admin.write_runtime (同一把
runtime.lock, 不跟在跑的桥 /model 抢写)。

覆盖**两个租户、两条启动路径**:
  · 默认租户的飞书 fleet (由服务管理器管理): 名册 fleet.json (fleet_env.fleet_registry_path)
  · 其他租户 + 自助建的 App 模式 bot (网关代管进程): ~/.fleet-app/fleets/<owner>.json
  · 远程节点 (带 addr 且 home 不在本机、addr 也不是本机地址, 见 is_remote) 的 runtime.json
    在对面机器上 —— 这里只列出来, 不碰; 要在那台机器上跑同一个脚本 (或 fleet_admin
    set-model <bot> default)。在 Windows 节点上跑时, 它自己的 bot 会被认成本机的。

只清**属于被隐藏提供方** (默认 anthropic + openai) 的覆盖; deepseek-* 和认不出的自定义 id 不动。
清掉 = 删 model 键 = 跟随桥的 DEFAULT_MODEL (有 key 时 = deepseek-flash)。

⚠ 顺序: **先让桥跑上 T-deepseek-model 这版代码 (重启), 再 --apply**。老代码的桥读到
"没覆盖" 会回落老默认 claude-opus-5 —— 正是没额度的那个。
恢复: 每个 home 下的 runtime.json.bak-* 拷回去即可 (或 fleet_admin set-model)。

用法:
  venv/bin/python migrate_runtime_deepseek.py                 # dry-run
  venv/bin/python migrate_runtime_deepseek.py --apply
  venv/bin/python migrate_runtime_deepseek.py --owner example-owner      # 只看一个租户
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet_providers as P  # noqa: E402

HIDE = {"anthropic", "openai"}


def _home(raw: str) -> Path:
    return Path(str(raw).replace("~", str(Path.home())))


def _local_hosts() -> set[str]:
    """本机的名字和地址 (查不到的就算了, 不联网)。"""
    hosts = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
    with contextlib.suppress(Exception):
        name = socket.gethostname()
        hosts |= {name.lower(), name.split(".")[0].lower()}
        _n, aliases, addrs = socket.gethostbyname_ex(name)
        hosts |= {x.lower() for x in aliases} | set(addrs)
    with contextlib.suppress(Exception):
        for info in socket.getaddrinfo(socket.gethostname(), None):
            hosts.add(info[4][0])
    with contextlib.suppress(Exception):          # 默认出口网卡的地址; UDP connect 不发包
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("192.0.2.1", 9))
            hosts.add(sk.getsockname()[0])
    return hosts


def _addr_host(addr: str) -> str:
    a = str(addr or "").strip()
    if a.startswith("["):                          # [::1]:8790
        return a[1:a.find("]")].lower() if "]" in a else a.lower()
    return (a.rsplit(":", 1)[0] if a.count(":") == 1 else a).lower()


def is_remote(home: Path, addr: str, local_hosts: set[str] | None = None) -> bool:
    """runtime.json 不在本机才算远程。

    名册里带 addr 只说明桥用 TCP 连 (Windows 节点在自己那台机器上跑这个脚本时,
    名册里的 bot 也全带 addr), 不说明它在别的机器上。所以:
      · 没 addr → 本机;
      · home 是本机的绝对路径且存在 → 本机 (Mac 上 "D:/fleet/x" 不是绝对路径, 不会误判);
      · addr 的主机是本机地址 → 本机;
      · 其余 → 远程, 不动。
    """
    if not addr:
        return False
    if home.is_absolute() and home.is_dir():
        return False
    hosts = _local_hosts() if local_hosts is None else local_hosts
    return _addr_host(addr) not in hosts


def registries(fleet_json: Path | None = None, fleets_dir: Path | None = None) -> list:
    """[(owner, name, home, remote_addr)] —— gzt 飞书 fleet + 各租户 App fleet。"""
    out = []
    if fleet_json is None:
        from fleet_env import fleet_registry_path  # noqa: PLC0415
        fleet_json = fleet_registry_path()
    if fleets_dir is None:
        base = os.environ.get("FLEET_APP_DIR")
        fleets_dir = (Path(base) if base else Path.home() / ".fleet-app") / "fleets"
    try:
        for name, cfg in json.loads(Path(fleet_json).read_text(encoding="utf-8")).items():
            if isinstance(cfg, dict) and cfg.get("home"):
                out.append(("gzt", name, _home(cfg["home"]), cfg.get("addr") or ""))
    except FileNotFoundError:
        pass
    for f in sorted(Path(fleets_dir).glob("*.json")):
        try:
            reg = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            print(f"⚠ 名册读不了, 跳过: {f}", file=sys.stderr)
            continue
        for name, cfg in reg.items():
            if isinstance(cfg, dict) and cfg.get("home"):
                out.append((f.stem, name, _home(cfg["home"]), ""))
    return out


def plan(home: Path) -> tuple[str, str]:
    """→ (动作, 说明)。动作 ∈ clear / keep / none"""
    f = home / "runtime.json"
    try:
        rt = json.loads(f.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "none", "无 runtime.json (已跟随默认)"
    except Exception as exc:  # noqa: BLE001
        return "keep", f"runtime.json 读不了 ({exc}), 不动"
    m = str((rt or {}).get("model") or "").strip()
    if not m:
        return "none", "无模型覆盖 (已跟随默认)"
    prov = P.provider_for_model(m)
    if P.app_model(m).lower().startswith(("claude-", "gpt-")) or m.lower() in {"opus", "sonnet", "haiku", "fable"}:
        if prov in HIDE:
            return "clear", f"{m} ({prov}) → 删覆盖, 跟随默认"
    return "keep", f"{m} 不在隐藏范围, 不动"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--owner", default="")
    ap.add_argument("--fleet-json", type=Path, default=None)
    ap.add_argument("--fleets-dir", type=Path, default=None)
    a = ap.parse_args(argv)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    changed = 0
    local_hosts = _local_hosts()
    for owner, name, home, addr in registries(a.fleet_json, a.fleets_dir):
        if a.owner and owner != a.owner:
            continue
        if is_remote(home, addr, local_hosts):
            print(f"[{owner}] {name:18} 远程节点 {addr}: runtime.json 在对面机器, 本机不动 (去那台机器跑)")
            continue
        act, why = plan(home)
        print(f"[{owner}] {name:18} {act:5} {why}")
        if act != "clear":
            continue
        changed += 1
        if not a.apply:
            continue
        shutil.copy2(home / "runtime.json", home / f"runtime.json.bak-{stamp}")
        import fleet_admin  # noqa: PLC0415
        fleet_admin.write_runtime(home, model="default")
    print(f"\n{'已改' if a.apply else '[dry-run] 将改'} {changed} 个; "
          f"{'备份 runtime.json.bak-' + stamp if a.apply else '什么都没写, 确认后加 --apply'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
