"""Turn failure / retry wording helpers split out of fleet_bridge (T022).

No lark / SDK imports — only getattr on ResultMessage-shaped objects.
Added first (「先加后减」); fleet_bridge still has its own copies until the
next commit switches the imports over and drops the duplicates.
"""
from __future__ import annotations

_NET_ERR_KEYS = ("connection lost", "connection closed", "connection error",
                 "connection reset", "socket", "overloaded", "peer closed",
                 "timeout", "502", "503", "529", "econnreset")


def _retry_summary(elapsed_s: float, chars: int, tools: int, ok: bool,
                   exc: str | None = None, errored: bool = False) -> str:
    '生成重试结束日志，区分成功、错误和无内容，避免将部分错误输出报告为成功。'
    head = f"重试结束 · {elapsed_s:.0f}s"
    if tools:
        head += f" · {tools} 次工具"
    if exc:
        return f"{head} · 异常告终 ({exc[:80]})"
    if errored:
        # 以重试结果状态判断失败，不从正文推测错误。
        return f"{head} · 拿回 {chars} 字但这一轮是错着结束的 (本轮按未跑完处理)"
    if ok:
        return f"{head} · 成功, 拿回 {chars} 字"
    return f"{head} · 没拿回内容 (本轮按未跑完处理)"


def _retry_user_note(ok: bool, exc: str | None = None, errored: bool = False) -> str:
    """给 App 看的收尾旁白, 配对开头那句「正在自动重试…」。

    走 ev=notice 而不是 ev=error: notice **不清**客户端的「生成中」, 而这一轮此刻
    确实还没结束 —— 清了就是个假收尾。真正的终结事件由后面的正常收尾路径发。
    """
    if exc:
        return "（自动重试异常中断，这一轮没跑完）"
    if errored:
        return "（自动重试也撞上同一个错，这一轮没跑完；发一句「重试」可再来一遍）"
    return "（已自动重试并接上）" if ok else "（自动重试没拿回内容，这一轮没跑完）"


def looks_like_api_error(text: str | None) -> bool:
    """判断 result 是否为 SDK 的 API 错误。只匹配固定前缀，避免把正常正文中的网络术语误判为故障。"""
    return str(text or "").lstrip().startswith("API Error")


_RESULT_FIELDS_MAX = 600        # 单个字段的 repr 上限 (errors 可能很长)


def _error_suffix(result_msg, answer_buf: str, saw_unsafe_tool: bool, retried: bool) -> str:
    """生成失败提示。不要以 subtype 作为错误回落值；正文已有错误原文时避免重复，但保留警告和操作提示。"""
    err = str(getattr(result_msg, "result", None)
              or getattr(result_msg, "terminal_reason", None) or "未知错误")
    tip = _failure_tip(saw_unsafe_tool, retried, err)
    if err and err in (answer_buf or ""):
        return f"\n\n⚠ 这一轮没跑完。{tip}"
    return f"\n\n⚠ 出错: {err}{tip}"


def _failure_meta(result_msg, saw_unsafe_tool: bool, retried: bool) -> dict:
    """这一轮失败时给客户端的**结构化**重试信息; 正常回合返回 {} (字段一个都不出现)。

    客户端需要「重试」按钮。规则**只在服务端存一份** ——
    让客户端去 match 错误文案的话, 同一套判据会在 Swift 和 Python 各存一份, 加一种
    错误类型要改两处, 迟早漂移。而漂移的后果恰好是这次要避免的那个:
    **在不该重试的错误上给出重试入口, 把无用功从打字变成点击, 还更顺手。**

    · `failed` 存在与否 = 这一轮失败与否 (正常回复不带这三个字段);
    · `retryable=False` 只给认证类 —— 重试用的是同一份凭据, 通常还是失败;
    · `retry_hint` 是 `_failure_tip` **去掉外层括号**的版本: 正文里那句嵌在句子里
      需要括号, 客户端要单独渲染成副文本, 括号是多余的。
      ⚠ 正文里那句的形状 (全角括号包住、位于 text 末尾) **是与客户端的约定**:
      它靠精确后缀剥离来避免同一句显示两遍。改这个形状前先说一声 —— 改了不会崩,
      只会**静默降级**成"不单独渲染", 而那是没人会注意到的那种降级。
    """
    err = str(getattr(result_msg, "result", None)
              or getattr(result_msg, "terminal_reason", None) or "未知错误")
    tip = _failure_tip(saw_unsafe_tool, retried, err)
    return {"failed": True,
            "retryable": not any(k in err.lower() for k in _AUTH_ERR_KEYS),
            "retry_hint": tip.strip("（）()")}


def _result_fields_line(result_msg) -> str:
    """记录结果字段的原始值，包括缺失字段的 None；截断长值时标明原长。"""
    if result_msg is None:
        return "result_msg=None (这一轮没拿到 ResultMessage)"
    def _r(name: str) -> str:
        val = repr(getattr(result_msg, name, None))
        if len(val) > _RESULT_FIELDS_MAX:
            val = val[:_RESULT_FIELDS_MAX] + f"…(截断, 原长 {len(val)})"
        return f"{name}={val}"
    # Include result because it contributes to the user-visible error.
    return " ".join(_r(n) for n in
                    ("is_error", "subtype", "api_error_status", "terminal_reason",
                     "errors", "result"))


# 认证失效的固定文案 (这句不是 bridge 也不是
# SDK python 层生成的, 是 SDK 捆绑的那个 256MB CLI 二进制吐的):
#   "Failed to authenticate: OAuth session expired and could not be refreshed"
#   "API Error: 401 OAuth access token has expired. Re-authenticate..."
# 它们**不是网络抖动**, 所以故意不在 _NET_ERR_KEYS 里 (不该自动重试, 那是对的);
# 但也因此掉进 _failure_tip 的默认分支, 于是用户被告知「发一句重试」——
# **而重试用的是同一份凭据, 通常还是失败**。教用户做无用功比不说更坏: 他会反复重试,
# 而真正该做的事 (去 Mac 重登) 一直没人告诉他。
_AUTH_ERR_KEYS = ("failed to authenticate", "oauth session expired",
                  "oauth access token has expired", "re-authenticate")

# TLS 证书校验失败可能是暂时性故障，也可能表明持续的信任问题。
#
# **判断: 该自动重试, 而重试不削弱安全。** 核心是 **重试 ≠ 绕过** ——
# 每一次重试都**独立地做完整证书校验**。真有中间人, 每次重试照样失败, 错误照样浮到用户
# 面前; 真是路径抖动 (代理 / 公共 WiFi / 边缘节点), 一次重试就过去了。
# ⚠ **但不能塞进 _NET_ERR_KEYS**: 那样一条**持续**的证书失败会被报成"网络中断, 已自动重试",
# 而这两种情况的下一步完全不同 —— 抖动是"等等再来", 反复校验失败可能是中间设备在解密
# 流量、或本机信任库坏了。**把可能的中间人说成网络抖动**, 正是这仓通篇在防的那个形状。
# ⇒ 单独一类: **重试同网络, 但日志和收尾话术分开** (见 _failure_tip)。
_TLS_ERR_KEYS = ("certificate_verification", "certificate verify failed",
                 "unknown_certificate", "ssl: certificate")


def _failure_tip(saw_unsafe_tool: bool, retried: bool, err: str = "") -> str:
    """这一轮没跑完时, 补给用户的那句「你要做什么」。

    即使已有部分正文，也需要说明回合未完成及可采取的下一步。
    """
    if any(k in (err or "").lower() for k in _AUTH_ERR_KEYS):
        
        # CLI 可在重新登录后读取更新的凭据，无需重启 bridge。
        return ("（**认证失效了, 重试通常没用** —— 请在 Mac 上跑一次 `claude` 重新登录。"
                "登录完**不用重启我**, 下一条消息就能用。）")
    # ⚠ **retried 必须排在 saw_unsafe_tool 前面**: 自动重试改成
    # continuation 语义之后, 有写操作的轮次**也会**自动重试, 两个条件可以同时为真 ——
    # 顺序反了就会对着一次真的重试说"未自动重试", 而那句话是用户判断下一步的依据。
    if any(k in (err or "").lower() for k in _TLS_ERR_KEYS):
        # ⚠ **证书错误不许说成"网络抖动"** —— 两者的下一步完全不同。
        # 一次失败多半是路径抖动 (已自动重试过); **反复失败才是信号**, 所以这句强调那一点,
        # 并且**不给"再发一句重试"** —— 一直重试正是这种情况下最没用的动作。
        return ("（**TLS 证书校验失败**" + ("且自动重试后仍失败" if retried else "") +
                " —— 多半是网络中间设备 (公司代理 / 公共 WiFi 劫持) 或本机证书信任库, "
                "不是普通抖动。换个网络再试; 反复出现要查证书链, 别一直重试）")
    if retried:
        return "（已自动重试一次仍未跑完; 发一句「重试」可再来一遍）"
    if saw_unsafe_tool:
        return "（本轮有写操作, 未自动重试以防重复; 发一句「重试」我再跑）"
    return "（发一句「重试」可再跑一遍）"


def turn_failed(result_msg) -> bool:
    """这一轮是不是出错了 —— **有类型字段就读类型, 没有才猜文本**。

    优先读取结构化错误字段；字段缺失或为空时回落到文本判断。
    """
    if result_msg is None:
        return False
    if getattr(result_msg, "api_error_status", None) is not None:
        return True                       # 上游明说了失败状态码, 没有比这更硬的
    if getattr(result_msg, "terminal_reason", None) == "api_error":
        return True
    return bool(result_msg.is_error) or looks_like_api_error(result_msg.result)
