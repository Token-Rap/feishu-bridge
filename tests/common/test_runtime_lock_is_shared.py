"""bridge 与 fleet_admin 必须锁**同一个文件** (审计 T022)。

两个**独立进程**会写同一份 `<home>/runtime.json`:
· bridge 的 `/model` `/effort` 与 App 静默通道
· `fleet_admin.py set-model / set-effort` (运维 CLI)
串行化全靠 `flock(<home>/runtime.lock)`。

⚠ **锁只在两边用同一个文件名时才成立**, 而现在这份一致是"巧合般"的:
    bridge:  _RUNTIME_FILE.with_suffix(".lock")   → runtime.lock
    admin:   home / "runtime.lock"                 → runtime.lock
一边是从 `runtime.json` 推导, 一边是写死的字面量。**哪天 bridge 改成
`runtime.json.lock`(比如为了和 tmp 命名统一), 两边就各锁各的** ——
而 `flock` 不会报错, 两个进程都拿得到自己那把锁, 然后**并发读-改-写同一个文件**。
表现: 运维刚 set-model, bot 那边一轮下来又被覆盖回去, 偶发且无痕。

⇒ 这一类**不该合并、但必须钉住**: 两个模块各有各的加载方式 (admin 是独立 CLI,
  让它 import bridge 太重), 强行共享会引入不必要的依赖。
  **合并不是唯一的收口手段, "让分叉当场变红"也是。**

⚠ 顺带记一处**不一致但无害**的, 免得以后有人当 bug 去"修":
    bridge 的临时文件 runtime.tmp   (`_RUNTIME_FILE.with_suffix(".tmp")`)
    admin  的临时文件 runtime.json.tmp (`path.with_suffix(path.suffix + ".tmp")`)
名字不同没关系 —— 各写各的 tmp 再 rename, 且整段都在锁内。**不要为了"整齐"去统一它**,
那会动到两处原子写, 收益为零。
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_HOME = Path("/tmp/fleet-fake-home")


def _bridge_lock_name() -> str:
    """按 bridge 源码里的写法算出锁文件名。"""
    src = (_ROOT / "src/feishu_bridge/bridge/fleet_bridge.py").read_text(encoding="utf-8")
    m = re.search(r'_RUNTIME_FILE\s*=\s*_BRIDGE_HOME\s*/\s*"([^"]+)"', src)
    assert m, "找不到 _RUNTIME_FILE 的定义 —— 先修扫描, 别改断言"
    runtime_name = m.group(1)
    m2 = re.search(r'_RUNTIME_FILE\.with_suffix\("(\.[a-z]+)"\)\s*,\s*"w"', src)
    assert m2, "找不到 bridge 开锁那一行 —— 先修扫描"
    return (_HOME / runtime_name).with_suffix(m2.group(1)).name


def _admin_lock_name() -> str:
    src = (_ROOT / "src/feishu_bridge/admin/fleet_admin.py").read_text(encoding="utf-8")
    m = re.search(r'open\(home\s*/\s*"([^"]+)"\s*,\s*"w"\)', src)
    assert m, "找不到 admin 开锁那一行 —— 先修扫描, 别改断言"
    return m.group(1)


def test_both_sides_lock_the_same_filename():
    b, a = _bridge_lock_name(), _admin_lock_name()
    assert b == a, (
        f"bridge 锁 {b}, fleet_admin 锁 {a} —— **两边各锁各的等于没锁**。\n"
        "flock 不会因此报错: 两个进程都拿得到自己那把, 然后并发读-改-写 runtime.json。\n"
        "表现是'运维刚 set-model, bot 一轮下来又覆盖回去', 偶发且无痕。")


def test_both_sides_write_the_same_runtime_file():
    """锁对了但写错文件同样白搭 —— 一并钉住。"""
    bs = (_ROOT / "src/feishu_bridge/bridge/fleet_bridge.py").read_text(encoding="utf-8")
    as_ = (_ROOT / "src/feishu_bridge/admin/fleet_admin.py").read_text(encoding="utf-8")
    b = re.search(r'_RUNTIME_FILE\s*=\s*_BRIDGE_HOME\s*/\s*"([^"]+)"', bs).group(1)
    hits = set(re.findall(r'home\s*/\s*"(runtime\.[a-z]+)"', as_))
    assert b in hits, f"bridge 写 {b}, 而 admin 那侧只见到 {sorted(hits)}"
