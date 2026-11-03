"""Behavioral regression coverage."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-tls-")]
try:
    from feishu_bridge import fleet_bridge as b
finally:
    sys.argv = _argv

_CERT = "API Error: UNKNOWN_CERTIFICATE_VERIFICATION_ERROR"


def test_cert_error_is_recognised():
    assert any(k in _CERT.lower() for k in b._TLS_ERR_KEYS), "认不出证书错误就不会重试"


def test_cert_error_is_not_lumped_with_network():
    """**不许混进网络那一类** —— 混了就会被报成"网络中断", 而那是两件事。"""
    assert not any(k in _CERT.lower() for k in b._NET_ERR_KEYS)


def test_cert_error_is_not_treated_as_auth():
    """也不是认证问题 —— 认证那类的结论是"重试没用, 去重登", 证书这类重试是有用的。"""
    assert not any(k in _CERT.lower() for k in b._AUTH_ERR_KEYS)


def test_tip_says_something_different_from_a_network_blip():
    """收尾话术必须和网络抖动分开, **而且不许劝他一直重试**。"""
    tip = b._failure_tip(saw_unsafe_tool=False, retried=True, err=_CERT)
    assert "证书" in tip, tip
    assert "别一直重试" in tip, "反复校验失败时劝他继续重试是最没用的动作"
    assert "发一句「重试」" not in tip, "默认那句会把他引向做无用功"

    plain = b._failure_tip(saw_unsafe_tool=False, retried=True, err="API Error: 529 overloaded")
    assert plain != tip, "网络抖动和证书失败不能给同一句话"


def test_a_normal_reply_mentioning_certificates_is_not_matched():
    """⚠ 判据要窄: 聊天里说到"证书"不该被当成故障。

    这条是照本仓 `_is_api_error` 的老纪律来的 —— 用关键词去匹配正文会误伤真回复,
    而误判的代价是**把一条好回复当故障重跑一遍**。
    """
    for text in ("我帮你看了下 SSL 证书链的配置",
                 "certificate 这个词在 RFC 里的定义是",
                 "你需要在 nginx 里配 ssl_certificate"):
        assert not any(k in text.lower() for k in b._TLS_ERR_KEYS), text
