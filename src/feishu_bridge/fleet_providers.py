"""模型提供方 —— 让 Claude Code 跑在 DeepSeek 上 (backend/T-deepseek-model)。

纯逻辑, 无 lark/sdk 依赖, 便于单测; fleet_bridge.py 与 app_frontend.py 共用这一份。

■ engine ≠ provider
engine = 桥用哪个 CLI (claude / codex); provider = 请求打到谁家。DeepSeek 有 Anthropic
兼容接口, 所以 deepseek-* 的 engine 仍是 "claude": 桥照样起 Claude Code, 只在建 client
时注入下面这组环境变量。会话存储 / 审批 / 提问卡 / compact 全部沿用 Claude 那条路。

■ 事实来源 (核过, 别凭记忆改):
· https://api-docs.deepseek.com/ —— 模型表: `deepseek-flash` (现由 DeepSeek-V4.1-Flash-1017
  提供) 与 `deepseek-v4-pro`; Anthropic base_url = https://api.deepseek.com/anthropic
· https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code —— Claude Code
  要设的变量: ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_MODEL=deepseek-flash[1m]
  / ANTHROPIC_DEFAULT_{OPUS,SONNET}_MODEL / ANTHROPIC_DEFAULT_HAIKU_MODEL=deepseek-flash /
  CLAUDE_CODE_SUBAGENT_MODEL=deepseek-flash / CLAUDE_CODE_AUTO_COMPACT_WINDOW=786432
· https://api-docs.deepseek.com/guides/thinking_mode —— effort 只有 low/high/max 真生效
  (medium/xhigh→high), 用户用真 key 查 GET /models 也是 supported_levels [low, high, max]。

■ key 从哪来 (先到先得), **只在要用时读, 从不打印**:
  1. 环境变量 DEEPSEEK_API_KEY (含各 bot home 的 .env, 桥启动时 load_dotenv 过)
  2. 文件 $FLEET_DEEPSEEK_KEY_FILE, 缺省 ~/.fleet-app/secrets/deepseek_api_key (不在任何 git 仓里)
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"
DEEPSEEK_FLASH = "deepseek-flash"
DEEPSEEK_PRO = "deepseek-v4-pro"
DEEPSEEK_MODELS = (DEEPSEEK_FLASH, DEEPSEEK_PRO)
DEEPSEEK_EFFORTS = ("low", "high", "max")
FLEET_FALLBACK_DEFAULT = "gpt-6.1-sol"
_AUTO_COMPACT_WINDOW = "786432"     # DeepSeek Claude Code 接入页给的值 (1M 窗口的 75%)
_1M_SUFFIX = "[1m]"                 # Claude Code 的「按 1M 上下文算窗口」后缀, 接入页推荐带


def provider_for_model(model: str) -> str:
    """anthropic | openai | deepseek。不认识的 id 当 anthropic (老行为: 一切非 gpt- 都走 Claude)。"""
    m = app_model(model).lower()
    if m.startswith("deepseek-"):
        return "deepseek"
    if m.startswith("gpt-"):
        return "openai"
    return "anthropic"


def app_model(model: str) -> str:
    """CLI/SDK 报回来的 `deepseek-flash[1m]` → App 侧 id `deepseek-flash`。别的原样。"""
    m = str(model or "").strip()
    return m[: -len(_1M_SUFFIX)] if m.lower().endswith(_1M_SUFFIX) else m


def cli_model(model: str) -> str:
    """App 侧 id → 传给 Claude Code `--model` / set_model 的串。

    只给 DeepSeek 补 `[1m]`: App/网关的模型名校验是 [A-Za-z0-9._-], 方括号进不去,
    所以这一步只能在桥里做。Anthropic/自定义 id 原样。
    """
    m = str(model or "").strip()
    if provider_for_model(m) == "deepseek" and not m.lower().endswith(_1M_SUFFIX):
        return m + _1M_SUFFIX
    return m


def key_file() -> Path:
    raw = os.environ.get("FLEET_DEEPSEEK_KEY_FILE", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".fleet-app" / "secrets" / "deepseek_api_key"


def deepseek_configured() -> bool:
    """有没有 key —— **不读内容**, 只看 env 非空或文件非空。给 caps / 默认值判断用。"""
    if os.environ.get("DEEPSEEK_API_KEY", "").strip():
        return True
    try:
        f = key_file()
        return f.is_file() and f.stat().st_size > 0
    except OSError:
        return False


def deepseek_key() -> str:
    """真正要建 client 时才读。读不到返回 ''。⚠ 返回值绝不许进日志。"""
    got = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if got:
        return got
    try:
        return key_file().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class ProviderNotConfigured(RuntimeError):
    pass


def provider_env(model: str) -> dict:
    """建 Claude Code 子进程时要**叠加**的环境变量 (SDK 是 {**os.environ, **options.env})。

    anthropic → {} (行为与改动前完全一致)。
    deepseek  → 指向 DeepSeek 的一整组; 同时把 CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY
                覆盖成空串 —— 一是确保认证走 DeepSeek key, 二是**别把 Anthropic 订阅凭证
                发给第三方端点**。没 key → 抛 ProviderNotConfigured (这一轮失败, 进程不倒)。
    """
    if provider_for_model(model) != "deepseek":
        return {}
    key = deepseek_key()
    if not key:
        raise ProviderNotConfigured(
            "DeepSeek 未配置: 没有 DEEPSEEK_API_KEY, 也没有 " + str(key_file()))
    main = cli_model(model)
    return {
        "ANTHROPIC_BASE_URL": DEEPSEEK_BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": key,
        "ANTHROPIC_API_KEY": "",
        "CLAUDE_CODE_OAUTH_TOKEN": "",
        "ANTHROPIC_MODEL": main,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": main,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": main,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": DEEPSEEK_FLASH,
        "CLAUDE_CODE_SUBAGENT_MODEL": DEEPSEEK_FLASH,
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": _AUTO_COMPACT_WINDOW,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }


def resolve_default_model(configured: bool | None = None) -> str:
    """舰队默认模型 —— 桥启动时算一次, 发布在 status.json defaults / runtime 事件里。

    FLEET_DEFAULT_MODEL 显式指定 > gpt-6.1-sol。
    保留 configured 参数兼容既有调用；DeepSeek key 不再决定舰队默认值。
    """
    explicit = os.environ.get("FLEET_DEFAULT_MODEL", "").strip()
    if explicit:
        return explicit
    return FLEET_FALLBACK_DEFAULT


# ── 会话归属哪个 provider (跨提供方续聊的护栏) ─────────────────────────────────
# Claude Code 的 transcript 里存着上一家返回的 thinking 块 (Anthropic 的带签名,
# DeepSeek 的不带)。把 A 家的历史整段 resume 给 B 家:
#   · DeepSeek → Anthropic: 不带合法签名的 thinking 块会被 Anthropic 拒 (400)。
#   · Anthropic → DeepSeek: DeepSeek 不支持 redacted_thinking / document 块; 带 tools
#     的请求还要求历史每轮都回传 reasoning —— 都可能 400, 而且是整个会话卡死, 不是一轮。
# 所以缺省**不跨 provider resume**: 切 provider 时开新会话, 旧会话留在 /sessions 里可切回
# (切回原 provider 再 /resume 就接上)。FLEET_CROSS_PROVIDER_RESUME=1 可放开 (自担风险)。
# 没登记过的老会话一律视为 anthropic —— 改动前所有 Claude 会话都是打到 Anthropic 的。

def cross_provider_resume_allowed() -> bool:
    return os.environ.get("FLEET_CROSS_PROVIDER_RESUME", "").strip() == "1"


def _read_map(path: Path) -> dict:
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def session_provider(path: Path, sid: str) -> str:
    return str(_read_map(path).get(sid) or "anthropic")


def tag_session(path: Path, sid: str, provider: str) -> None:
    """记一笔 sid → provider。失败静默 (最坏是下次把它当 anthropic)。"""
    if not sid:
        return
    try:
        data = _read_map(path)
        if data.get(sid) == provider:
            return
        data[sid] = provider
        if len(data) > 2000:                       # 只留最近的, 别无限长
            data = dict(list(data.items())[-2000:])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        pass


def can_resume(path: Path, sid: str, model: str) -> bool:
    if not sid or cross_provider_resume_allowed():
        return True
    return session_provider(path, sid) == provider_for_model(model)


def codex_bin() -> str | None:
    """Codex CLI 路径。显式 CODEX_BIN 也必须真的可执行, 不拿字符串冒充 capability。"""
    configured = os.getenv("CODEX_BIN", "").strip()
    return shutil.which(configured) if configured else shutil.which("codex")
