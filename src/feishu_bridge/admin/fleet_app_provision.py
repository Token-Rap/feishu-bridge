#!/usr/bin/env python3
"""Create a Feishu or Lark app using the device-code registration flow."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from feishu_bridge.bridge.fleet_env import env as _env   # 新名 FLEET_BRIDGE_* 优先, 回落旧名

# 飞书/Lark 官方账号域名 — 注册 device-code 流走这里
_ACCOUNTS = {
    "feishu": "https://accounts.feishu.cn",
    "lark": "https://accounts.larksuite.com",
}
_OPEN = {
    "feishu": "https://open.feishu.cn",
    "lark": "https://open.larksuite.com",
}
_REGISTRATION_PATH = "/oauth/v1/app/registration"
_TIMEOUT_S = 10

# bridge 配置目录 (多实例: FEISHU_BRIDGE_HOME 切目录; 默认 ~/.fleet-claude-bridge)
_BRIDGE_HOME = Path(_env("HOME") or str(Path.home() / ".fleet-claude-bridge"))
_BRIDGE_ENV = _BRIDGE_HOME / ".env"


def _post(base_url: str, body: dict[str, str]) -> dict:
    """POST form-urlencoded 到注册端点. 端点在 4xx 也回 JSON (poll 的
    authorization_pending 就是 400), 故无论状态码都解析 body."""
    req = Request(
        f"{base_url}{_REGISTRATION_PATH}",
        data=urlencode(body).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urlopen(req, timeout=_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        raw = exc.read()
        if raw:
            try:
                return json.loads(raw.decode("utf-8"))
            except ValueError:
                raise exc from None
        raise


def _init(base_url: str) -> None:
    res = _post(base_url, {"action": "init"})
    if "client_secret" not in (res.get("supported_auth_methods") or []):
        raise RuntimeError(f"注册环境不支持 client_secret 认证: {res}")


def _begin(base_url: str) -> dict:
    res = _post(base_url, {
        "action": "begin",
        "archetype": "PersonalAgent",
        "auth_method": "client_secret",
        "request_user_info": "open_id",
    })
    device_code = res.get("device_code")
    if not device_code:
        raise RuntimeError(f"注册未返回 device_code: {res}")
    return {
        "device_code": device_code,
        "qr_url": res.get("verification_uri_complete", ""),
        "interval": res.get("interval") or 5,
        "expire_in": res.get("expire_in") or 600,
    }


def _poll(base_url: str, device_code: str, interval: int, expire_in: int) -> dict | None:
    """轮询直到用户扫码授权 / 超时 / 拒绝. 成功返回 app_id/app_secret/open_id."""
    deadline = time.monotonic() + expire_in
    dots = 0
    while time.monotonic() < deadline:
        try:
            res = _post(base_url, {
                "action": "poll", "device_code": device_code, "tp": "ob_app",
            })
        except (URLError, OSError, ValueError):
            time.sleep(interval)
            continue
        dots += 1
        if dots == 1:
            print("  等待扫码授权", end="", flush=True)
        elif dots % 3 == 0:
            print(".", end="", flush=True)

        if res.get("client_id") and res.get("client_secret"):
            print()
            return {
                "app_id": res["client_id"],
                "app_secret": res["client_secret"],
                "open_id": (res.get("user_info") or {}).get("open_id"),
            }
        if res.get("error") in {"access_denied", "expired_token"}:
            print()
            print(f"  ❌ 注册终止: {res.get('error')}")
            return None
        time.sleep(interval)
    print()
    print("  ❌ 超时, 未完成扫码")
    return None


def _render_qr(url: str) -> bool:
    try:
        import qrcode
    except ImportError:
        return False
    try:
        qr = qrcode.QRCode()
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def _probe_bot_name(app_id: str, app_secret: str, domain: str) -> str | None:
    """探测机器人名字 (方便用户在飞书里搜索打开对话). best-effort."""
    base = _OPEN.get(domain, _OPEN["feishu"])
    try:
        tok_req = Request(
            f"{base}/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(tok_req, timeout=_TIMEOUT_S) as resp:
            token = json.loads(resp.read().decode()).get("tenant_access_token")
        if not token:
            return None
        info_req = Request(
            f"{base}/open-apis/bot/v3/info",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urlopen(info_req, timeout=_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode())
        bot = data.get("bot") or {}
        return bot.get("app_name") or bot.get("bot_name")
    except Exception:  # noqa: BLE001
        return None


def _save_env(creds: dict, domain: str) -> None:
    """把凭证写入 ~/.fleet-claude-bridge/.env (幂等更新已有键)."""
    _BRIDGE_HOME.mkdir(parents=True, exist_ok=True)
    updates = {
        "FLEET_BRIDGE_APP_ID": creds["app_id"],
        "FLEET_BRIDGE_APP_SECRET": creds["app_secret"],
        "FLEET_BRIDGE_DOMAIN": domain,
    }
    if creds.get("open_id"):
        updates["FLEET_BRIDGE_OPEN_ID"] = creds["open_id"]

    lines = _BRIDGE_ENV.read_text().splitlines() if _BRIDGE_ENV.exists() else []
    seen = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else None
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}={val}")
    _BRIDGE_ENV.write_text("\n".join(out) + "\n")
    # .env 含密钥 — 收紧权限
    _BRIDGE_ENV.chmod(0o600)


def main() -> int:
    ap = argparse.ArgumentParser(description="飞书自建应用扫码建号 (scan-to-create)")
    ap.add_argument("--lark", action="store_true", help="用 Lark 国际版 (默认飞书国内)")
    args = ap.parse_args()
    domain = "lark" if args.lark else "feishu"
    base_url = _ACCOUNTS[domain]

    print(f"\n  ─── 飞书自建应用 · 扫码建号 ({domain}) ───\n")
    print("  连接飞书注册服务...", end="", flush=True)
    _init(base_url)
    begin = _begin(base_url)
    print(" ok\n")

    qr_url = begin["qr_url"]
    if _render_qr(qr_url):
        print("  ↑ 用飞书手机 App 扫描上面二维码授权")
        print(f"  或直接在手机飞书里打开:\n  {qr_url}\n")
    else:
        print(f"  在飞书手机 App 里打开此链接授权:\n\n  {qr_url}\n")

    creds = _poll(base_url, begin["device_code"], begin["interval"], begin["expire_in"])
    if not creds:
        return 1

    _save_env(creds, domain)
    bot_name = _probe_bot_name(creds["app_id"], creds["app_secret"], domain)
    print("\n  ✅ 应用已创建, 凭证已写入:", _BRIDGE_ENV)
    print(f"     FLEET_BRIDGE_APP_ID    = {creds['app_id']}")
    print(f"     FLEET_BRIDGE_OPEN_ID   = {creds.get('open_id') or '(未返回)'}")
    if bot_name:
        print(f"     机器人名字              = {bot_name}  ← 在飞书里搜这个名字打开对话")
    print("\n  下一步:")
    print("  1) 在飞书里搜上面机器人名字, 给它发一条消息建立私聊")
    print("  2) 启动 bridge (Claude 认证默认复用 claude CLI 订阅登录, 无需额外 token)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
