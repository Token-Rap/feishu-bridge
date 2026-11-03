"""markdown → 飞书卡片 (Card JSON 1.0) 安全文本.

飞书 1.0 `markdown` 元素只渲染子集 (加粗/斜体/删除线/链接/行内代码/代码块/
有序无序列表/分隔线), **不渲染** ATX 标题 (`##`) 和 GFM 表格 (`| a | b |`);
schema 2.0 虽支持但需客户端 ≥7.20 且旧端静默退化成裸文本. 故按最低公共子集转换,
保证任意客户端都渲染正确:

  - 标题 `## X`      → `**X**`
  - 表格            → 每行 `- **首列** · 表头2: 值2 · 表头3: 值3`
  - 引用 `> x`      → `» x`
  - 代码块 ``` 内    → 原样 (不转换其中的 # / |)
  - LaTeX `$$..$$` / `$..$` → 纯文本符号 (\\frac→/, \\sqrt→√, \\times→× 等;
    飞书不渲染 LaTeX，需降级; 美元价格 `$704` 不受影响 — 仅含 \\ ^ _ {} = 的才算公式)
  - 加粗/斜体/链接/行内代码/列表/分隔线 → 原样保留

输出保证不含裸 `## ` 标题行或 `| a | b |` 表格行.
依据: 研究 workflow feishu-card-markdown-research (高置信).
"""
from __future__ import annotations

import re

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_SEP_CELL = re.compile(r":?-{1,}:?")

# ── LaTeX → 纯文本降级 (飞书卡片不渲染 LaTeX) ──
_FENCE_BLOCK = re.compile(r"(```.*?```|~~~.*?~~~)", re.S)
_LATEX_SYMS = [  # 顺序敏感: 长命令在前 (\\neq 先于 \\ne)
    (r"\leq", "≤"), (r"\le", "≤"), (r"\geq", "≥"), (r"\ge", "≥"),
    (r"\neq", "≠"), (r"\ne", "≠"), (r"\times", "×"), (r"\div", "÷"),
    (r"\pm", "±"), (r"\approx", "≈"), (r"\cdot", "·"), (r"\sum", "Σ"),
    (r"\infty", "∞"), (r"\alpha", "α"), (r"\beta", "β"), (r"\sigma", "σ"),
    (r"\mu", "μ"), (r"\rho", "ρ"), (r"\Delta", "Δ"), (r"\lambda", "λ"),
]


def _wrap(x: str) -> str:
    """分子/分母含运算符或空格时加括号, 否则裸放."""
    x = x.strip()
    return f"({x})" if re.search(r"[+\-/ ]", x) else x


def _latex_body(s: str) -> str:
    """一段 LaTeX 公式体 → 可读纯文本."""
    s = re.sub(r"\\(?:text|mathrm|mathbf|operatorname)\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\bar\{([^{}]*)\}", "\\1̄", s)          # \bar{x} → x̄
    s = re.sub(r"\\sqrt\{([^{}]*)\}", r"√(\1)", s)
    for _ in range(3):                                         # 嵌套分数最多展开 3 层
        s2 = re.sub(r"\\[dt]?frac\{([^{}]*)\}\{([^{}]*)\}",
                    lambda m: f"{_wrap(m.group(1))}/{_wrap(m.group(2))}", s)
        if s2 == s:
            break
        s = s2
    s = re.sub(r"\\left|\\right", "", s)
    for k, v in _LATEX_SYMS:
        s = s.replace(k, v)
    s = s.replace("^2", "²").replace("^3", "³")
    s = re.sub(r"\^\{([^{}]*)\}", r"^(\1)", s)
    s = re.sub(r"_\{([^{}]*)\}", r"_(\1)", s)
    s = re.sub(r"\\[a-zA-Z]+", "", s)                          # 未知命令丢弃
    s = s.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", s).strip()


def _convert_latex(md: str) -> str:
    """全文 LaTeX 降级, 跳过代码围栏; 行内 $..$ 仅当内容像公式 (含 \\ ^ _ {} =)
    才转换 — 保护美元价格 `$704` / `亏 $10 风险 $8`。"""
    parts = _FENCE_BLOCK.split(md)
    for i in range(0, len(parts), 2):                          # 偶数段 = 围栏外
        seg = re.sub(r"\$\$(.+?)\$\$", lambda m: _latex_body(m.group(1)), parts[i], flags=re.S)
        seg = re.sub(r"\$([^$\n]+)\$",
                     lambda m: _latex_body(m.group(1)) if re.search(r"[\\^_{}=]", m.group(1))
                     else m.group(0), seg)
        parts[i] = seg
    return "".join(parts)


def _split_row(line: str) -> list[str]:
    """拆 GFM 表格行为单元格 (去首尾管道)."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_table_row(line: str) -> bool:
    s = line.strip()
    return "|" in s and s not in ("", "|")


def _is_sep_row(line: str) -> bool:
    """表格分隔行: 每个非空单元格都形如 ---, :--, --:, :-:."""
    cells = _split_row(line)
    nonempty = [c for c in cells if c]
    return bool(nonempty) and all(_SEP_CELL.fullmatch(c) for c in nonempty)


def _row_to_bullet(headers: list[str], cells: list[str]) -> str:
    """一行表格数据 → 一个 bullet (首列加粗作 key, 其余 表头:值 · 连接)."""
    if not cells:
        return ""
    bullet = f"- **{cells[0]}**"
    segs = []
    for idx in range(1, len(cells)):
        val = cells[idx].strip()
        if not val:
            continue
        hdr = headers[idx].strip() if idx < len(headers) else ""
        segs.append(f"{hdr}: {val}" if hdr else val)
    return bullet + (" · " + " · ".join(segs) if segs else "")


def md_to_feishu(md: str) -> str:
    """转换 markdown 为飞书 1.0 卡片 markdown 元素安全文本."""
    if not md:
        return md
    md = _convert_latex(md)
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    in_fence = False
    while i < n:
        line = lines[i]

        # 代码块围栏: 内部原样直通
        if _FENCE.match(line):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if in_fence:
            out.append(line)
            i += 1
            continue

        # 表格: 当前行是表格行 + 下一行是分隔行
        if _is_table_row(line) and i + 1 < n and _is_sep_row(lines[i + 1]):
            headers = _split_row(line)
            i += 2  # 跳过表头 + 分隔行
            while i < n and _is_table_row(lines[i]) and not _is_sep_row(lines[i]) \
                    and not _FENCE.match(lines[i]):
                out.append(_row_to_bullet(headers, _split_row(lines[i])))
                i += 1
            continue

        # 标题 → 加粗
        m = _HEADING.match(line)
        if m:
            out.append(f"**{m.group(2).strip()}**")
            i += 1
            continue

        # 引用 → »
        mq = _QUOTE.match(line)
        if mq:
            out.append(f"» {mq.group(1)}")
            i += 1
            continue

        out.append(line)
        i += 1

    return "\n".join(out)
