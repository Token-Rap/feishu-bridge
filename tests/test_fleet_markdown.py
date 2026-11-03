"""Behavioral regression coverage."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from feishu_bridge.fleet_markdown import md_to_feishu  # noqa: E402


def test_heading_to_bold():
    assert md_to_feishu("## Daily PnL") == "**Daily PnL**"
    assert md_to_feishu("# T") == "**T**"
    assert md_to_feishu("###### deep") == "**deep**"
    # 行内 # 不动
    assert md_to_feishu("issue #42 fixed") == "issue #42 fixed"


def test_table_to_bullets():
    md = "\n".join([
        "| Pair | Net | Sharpe |",
        "|---|---|---|",
        "| BTC | +12 | 1.3 |",
        "| ETH | -3 | 0.2 |",
    ])
    out = md_to_feishu(md)
    assert out == (
        "- **BTC** · Net: +12 · Sharpe: 1.3\n"
        "- **ETH** · Net: -3 · Sharpe: 0.2"
    )
    assert "|" not in out
    assert "---" not in out


def test_table_no_outer_pipes():
    md = "a | b\n--- | ---\n1 | 2"
    out = md_to_feishu(md)
    assert out == "- **1** · b: 2"


def test_table_single_column():
    md = "| Name |\n|---|\n| solo |"
    assert md_to_feishu(md) == "- **solo**"


def test_blockquote():
    assert md_to_feishu("> heads up") == "» heads up"


def test_keep_bold_list_code_link():
    src = "**bold** and `code`\n- item a\n- item b\n[x](https://y.com)"
    assert md_to_feishu(src) == src  # 全在 1.0 子集, 原样


def test_fenced_code_untouched():
    md = "```bash\ncat x | grep '## header'\n```"
    # 围栏内的 | 和 ## 不能被转换成 bullet/bold
    assert md_to_feishu(md) == md


def test_no_raw_heading_or_table_survives_complex():
    md = "\n".join([
        "# 📊 Realized Ledger",
        "",
        "**总览**",
        "",
        "| instId | net | t |",
        "|---|---:|---:|",
        "| BTC-USDT-SWAP | -21.5 | -1.36 |",
        "",
        "> 别用 trades.jsonl 算盈亏",
        "",
        "```",
        "| this stays | literal |",
        "```",
    ])
    out = md_to_feishu(md)
    # 围栏外不得有裸标题行或表格数据行
    for ln in out.split("\n"):
        assert not re.match(r"^\s{0,3}#{1,6}\s", ln), f"裸标题残留: {ln}"
    assert "» 别用 trades.jsonl 算盈亏" in out
    assert "- **BTC-USDT-SWAP**" in out
    # 围栏内原样保留
    assert "| this stays | literal |" in out


def test_empty_passthrough():
    assert md_to_feishu("") == ""


def test_latex_block_and_inline_degrade():
    """Behavioral regression coverage."""
    md = "$$t = \\frac{\\bar{x}}{s / \\sqrt{n}}$$\n\n其中 $s \\times \\sqrt{n} \\approx 9.5\\%$"
    out = md_to_feishu(md)
    assert "$$" not in out and "\\frac" not in out
    assert "x̄/(s / √(n))" in out and "×" in out and "≈" in out


def test_latex_spares_dollar_prices():
    md = "QQQ 挂 $704, 止损 $672; 单笔亏 $10 风险 $8 封顶"
    assert md_to_feishu(md) == md


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")
