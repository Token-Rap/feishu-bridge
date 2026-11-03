"""使用真实 socket 验证上传中途连接重置后的重试。

服务端接收部分请求后发送 RST，以覆盖连接建立后中断的情况。
HTTP 502、503、504 可重试；4xx 和上游应用的 500 不自动重试。
该测试可随 pytest 运行，也可作为独立脚本运行。
"""
from __future__ import annotations

import os
import socket
import struct
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path

# 默认取本仓 (tests/ 的上级); 远程节点用 FLEET_BRIDGE_DIR 指到它自己的 bridge 目录
BRIDGE_DIR = os.environ.get("FLEET_BRIDGE_DIR") or str(Path(__file__).resolve().parents[1])
sys.path.insert(0, BRIDGE_DIR)
from feishu_bridge.bridge.app_frontend import AppFrontend  # noqa: E402
















try:
    sys.stdout.reconfigure(errors="replace")      # py3.7+; 保留终端原编码, 只是不再抛
except Exception:  # noqa: BLE001
    pass


def _mark(ok: bool) -> str:
    """能显示 emoji 就用, 显示不了退回 ASCII —— 两边控制台读到的都是人话。"""
    sym = "✅" if ok else "❌"
    try:
        sym.encode(sys.stdout.encoding or "utf-8")
        return sym
    except (UnicodeEncodeError, LookupError, TypeError):
        return "[PASS]" if ok else "[FAIL]"


OK_BODY = b'{"path": "/uploads/fake.png"}'
PAYLOAD_KB = 512          # 要够大, 保证 body 分多个 TCP 段发, 才有「传到一半」可言


def _png_bytes(kb: int) -> bytes:
    """手搓一个合法 PNG, 不依赖 Pillow, 也不依赖任何 bot 的私有文件。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (len(data).to_bytes(4, "big") + tag + data
                + zlib.crc32(tag + data).to_bytes(4, "big"))

    width, height = 64, 64
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(width * 3) for _ in range(height))
    padding = os.urandom(kb * 1024)          # 随机数据不可压缩, 撑出真实体积
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"teXt", b"pad\x00" + padding)
            + chunk(b"IEND", b""))


def _drain_request(conn: socket.socket) -> None:
    """Behavioral regression coverage."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        part = conn.recv(65536)
        if not part:
            return
        buf += part
    head, _, rest = buf.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
            break
    remaining = length - len(rest)
    while remaining > 0:
        part = conn.recv(min(65536, remaining))
        if not part:
            return
        remaining -= len(part)


class FakeGateway:
    """假网关: 前 reset_times 个连接传到一半 RST, 之后按 final_status 应答。"""

    def __init__(self, reset_times: int, final_status: bytes = b"200 OK",
                 final_body: bytes = OK_BODY) -> None:
        self.reset_times = reset_times
        self.final_status = final_status
        self.final_body = final_body
        self.connections = 0
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            self.connections += 1
            try:
                if self.connections <= self.reset_times:
                    conn.recv(8192)              # 只收一部分 → 「传到一半」
                    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                    struct.pack("ii", 1, 0))   # close 发 RST 而非 FIN
                    conn.close()
                    continue
                _drain_request(conn)             # 把 body 收干净再应答, 免得对端先拿到 RST
                conn.sendall(b"HTTP/1.1 " + self.final_status + b"\r\n"
                             b"Content-Type: application/json\r\n"
                             b"Content-Length: " + str(len(self.final_body)).encode()
                             + b"\r\nConnection: close\r\n\r\n" + self.final_body)
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop = True
        self._srv.close()


def _run(image: Path, reset_times: int, status: bytes):
    """跑一次真实中转, 返回 (连接次数, 结果, 日志)。pytest 与脚本共用。"""
    gw = FakeGateway(reset_times, status, OK_BODY if status == b"200 OK" else b"{}")







    for _p in ("FLEET_BRIDGE_", "FEISHU_BRIDGE_"):
        for _k in ("GATEWAY", "GATEWAY_TOKEN", "BOT_NAME"):
            os.environ.pop(_p + _k, None)
    os.environ["FLEET_BRIDGE_GATEWAY"] = f"http://127.0.0.1:{gw.port}"
    os.environ["FLEET_BRIDGE_GATEWAY_TOKEN"] = "probe-token"
    os.environ["FLEET_BRIDGE_BOT_NAME"] = "test"

    logs: list[str] = []
    front = AppFrontend(Path(tempfile.gettempdir()), logs.append)
    result = front.relay_media(str(image))
    conns = gw.connections
    gw.close()
    # 事后双保险: 一次都没连上假网关 = 它没接管, 而 relay 已经跑完了 —— 那多半打在了
    # 别的地方。**这时候必须吵**, 不能让它退化成一条普通的 assert 失败:
    # 后者读起来像"功能坏了", 前者才是"你刚往生产打了东西"。
    if conns == 0:
        raise RuntimeError(
            "假网关一次都没被连上 —— relay 可能打到了真网关。"
            f"检查环境里是否还有 FLEET_BRIDGE_/FEISHU_BRIDGE_ 的 GATEWAY 配置; "
            f"本次 relay 返回: {result!r}")
    return conns, result, logs


def run_case(name: str, image: Path, reset_times: int, status: bytes,
             expect_path: bool, expect_conns: int) -> bool:
    start = time.monotonic()
    conns, result, logs = _run(image, reset_times, status)
    elapsed = time.monotonic() - start

    class _G:      # 保持下面打印逻辑原样
        connections = conns
    gw = _G()
    path_ok = (result is not None) == expect_path
    conns_ok = gw.connections == expect_conns
    print("%-32s %s  连接=%d/%d 耗时=%.2fs"
          % (name, _mark(path_ok and conns_ok), gw.connections, expect_conns, elapsed))
    if not path_ok:                      # 只在真不符时才打, 免得看的人以为是返回值出的问题
        print("     返回=%s, 期望%s" % (result, "一个路径" if expect_path else " None"))
    if not conns_ok:
        print("     连接次数不符 —— 重试判据与期望的契约不一致")
    ok = path_ok and conns_ok
    for line in logs:
        print("     日志: %s" % line)
    return ok


# 用例表: pytest 与独立脚本**共用同一份**, 免得两条路径断言不同的契约
# (name, reset_times, status, expect_path, expect_conns)
CASES = [
    # 最关键: 前两次被 RST, 第三次成功 —— 证明重试**真能救回来**
    ("① 两次 RST 后成功 → 救回", 2, b"200 OK", True, 3),
    ("② 一直 RST → 试满 3 次", 99, b"200 OK", False, 3),
    # 4xx: 请求本身的问题, 重试无用
    ("③ 415 类型不认 → 不重试", 0, b"415 Unsupported Media Type", False, 1),
    ("④ 413 超限 → 不重试", 0, b"413 Payload Too Large", False, 1),
    # 5xx 要分开看: 中间层够不到上游是瞬时的, 上游自己炸了不是
    ("⑤ 502 中间层 → 重试", 0, b"502 Bad Gateway", False, 3),
    ("⑥ 503 不可用 → 重试", 0, b"503 Service Unavailable", False, 3),
    ("⑦ 504 超时 → 重试", 0, b"504 Gateway Timeout", False, 3),
    ("⑧ 500 上游炸了 → 不重试", 0, b"500 Internal Server Error", False, 1),
]


# ── pytest 入口 (随主套件默认跑) ────────────────────────────────────
def _fixture_png(tmp_path_factory):
    """512KB 夹具生成一次给全部用例用 —— 每个用例各生成一次纯属浪费。"""
    p = tmp_path_factory.mktemp("relay") / "relay_fixture.png"
    p.write_bytes(_png_bytes(PAYLOAD_KB))
    return p


try:
    import pytest

    @pytest.fixture(scope="module")
    def fixture_png(tmp_path_factory):
        return _fixture_png(tmp_path_factory)

    @pytest.mark.parametrize("name,resets,status,expect_path,expect_conns", CASES,
                             ids=[c[0].split()[0] for c in CASES])
    def test_retry_contract(fixture_png, name, resets, status, expect_path, expect_conns):
        gw, result, _ = _run(fixture_png, resets, status)
        assert (result is not None) == expect_path, f"{name}: 返回值不符"
        assert gw == expect_conns, f"{name}: 连接 {gw} 次, 期望 {expect_conns} —— 重试判据与契约不一致"
except ImportError:      # 远程节点没装 pytest 也要能独立跑
    pass


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        image = Path(tmp) / "relay_fixture.png"
        image.write_bytes(_png_bytes(PAYLOAD_KB))
        print("夹具 %s (%.0f KB)\n" % (image.name, image.stat().st_size / 1024))

        results = [
            # 最关键: 前两次被 RST, 第三次成功 —— 证明重试**真能救回来**
            run_case(n, image, r, s, p, c) for n, r, s, p, c in CASES
        ]

    passed = sum(results)
    print("\n%d/%d 通过" % (passed, len(results)))
    if passed != len(results):
        print("⚠ 若失败的恰是 ⑤⑥⑦, 说明这台还是「HTTPError 一律不重试」的旧版, 灰度没铺到。")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
