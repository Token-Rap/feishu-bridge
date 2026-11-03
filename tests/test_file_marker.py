"""bot 发文件: `[[文件: /绝对路径]]` 标记 (契约 fleet_api §2.24)。

桥这一侧只管两件事, 每件只住一处:
· **来源路径能不能发** —— 只有桥见过原始路径, 所以这条规则住在桥:
  须在 `$HOME` 或 `/tmp` 下, 且**不含 `.` 开头的目录段**。bot 可能被提示注入,
  这道挡的是 `~/.ssh` `~/.aws` `~/.claude` `~/.fleet-*` 这类地方。
· **把它交给网关** —— 与图片同一个出口, 但广播 `ev:"file"` 而不是 `ev:"media"`。
类型 / 大小由网关判 (files.py), 桥**不重复写那份白名单** —— 两份迟早分叉。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-test-home-")]
import fleet_bridge as b  # noqa: E402
import app_frontend  # noqa: E402
sys.argv = _argv

_OK_DIR = Path(tempfile.mkdtemp(prefix="filemark-", dir="/tmp"))


def _mk(rel: str, body: bytes = b"PK") -> Path:
    p = _OK_DIR / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return p


# ── 标记提取 ────────────────────────────────────────────────────────
def test_file_marker_is_extracted_and_stripped():
    p = _mk("报表.xlsx")
    text, items = b._extract_media_markers(f"做好了\n[[文件: {p}]]\n请查收")
    assert items == [("file", str(p))], items
    assert "[[文件" not in text and "做好了" in text


def test_english_alias_file():
    p = _mk("r.csv")
    _t, items = b._extract_media_markers(f"[[file: {p}]]")
    assert items == [("file", str(p))]


def test_image_and_video_markers_are_unchanged():
    """回归: 加一种 kind 不许把原来两种带歪。"""
    img, vid = _mk("a.png"), _mk("v.mp4")
    _t, items = b._extract_media_markers(f"[[图片: {img}]]\n[[视频: {vid}]]")
    assert items == [("image", str(img)), ("video", str(vid))], items


# ── 来源路径规则 (只在桥判) ─────────────────────────────────────────
def test_dot_directory_sources_are_refused_visibly():
    """`.` 开头的目录段一律拒 —— 被注入的 bot 发不出 ~/.ssh / ~/.claude 里的东西。
    拒绝要**看得见**: 原地换成一句说明, 不许静默吞掉。"""
    p = _mk(".secret/notes.md")
    text, items = b._extract_media_markers(f"[[文件: {p}]]")
    assert items == [], "dot 目录里的文件被放行了"
    assert "不允许" in text, f"拒绝没有留下可见说明: {text!r}"


def test_sources_outside_home_and_tmp_are_refused():
    text, items = b._extract_media_markers("[[文件: /etc/hosts]]")
    assert items == [] and "不允许" in text, text


def test_path_rule_does_not_apply_to_images():
    """路径规则**只管文件**: 图片走的还是原来那条 (网关 /media 有自己的闸)。
    别让这次改动悄悄收紧了发图 —— 那是另一个功能的行为改变。"""
    img = _mk(".cache/shot.png")
    _t, items = b._extract_media_markers(f"[[图片: {img}]]")
    assert items == [("image", str(img))]


# ── 交给网关 ────────────────────────────────────────────────────────
class _Front:
    def __init__(self):
        self.events = []
        self._sock_path = _OK_DIR / "app.sock"

    def broadcast(self, ev):
        self.events.append(ev)

    relays_to_gateway = False             # 本机模式 (真对象上是 property)

    def relay_media(self, _p):
        return None                       # 本机模式: 不中继


class _Feishu:
    def send_media_file(self, _chat, _path, _kind):
        return True


def test_app_transport_emits_ev_file_not_ev_media():
    p = _mk("out.xlsx")
    front = _Front()
    app_frontend.TeeTransport(_Feishu(), front).send_media_file("chat", str(p), "file")
    evs = [e for e in front.events if e.get("ev") in ("file", "media")]
    assert evs and evs[0]["ev"] == "file", f"文件被当成媒体广播了: {evs}"
    assert evs[0]["name"] == "out.xlsx" and evs[0]["path"] == str(p)


def test_feishu_transport_does_not_upload_a_file_as_an_image():
    """飞书模式原来只分 video 与"其他", "其他"一律**当图片上传** ——
    xlsx 会被当图片传, 失败后报"发送失败"。改成发一句说明, 并返回成功
    (App 那侧照常收库; 飞书本来就发不了文件)。"""
    sent, uploads = [], []

    class _S:
        def send_text(self, chat, text): sent.append(text)
        def upload_image(self, path): uploads.append(path); return None
        def send_image(self, *_a): return False

    ft = b.FeishuTransport.__new__(b.FeishuTransport)
    ft._s = _S()
    assert ft.send_media_file("chat", str(_mk("x.xlsx")), "file") is True
    assert not uploads, "xlsx 被当成图片上传了"
    assert sent and "x.xlsx" in sent[0]


# ── bot 得知道有这个标记 ────────────────────────────────────────────
def test_bridge_note_tells_bots_about_the_file_marker():
    """**不写进系统提示, 没有一个 bot 会用它。**"""
    src = (Path(__file__).resolve().parents[1] / "fleet_bridge.py").read_text(encoding="utf-8")
    note = src[src.index("桥接能力:"):src.index("桥接能力:") + 1200]
    assert "[[文件:" in note, "系统提示里的桥接能力没提 [[文件:]] —— bot 不会知道能发文件"


# ── Windows: 仓在 D 盘, 不在 $HOME 下 ────────
def test_files_in_the_bots_own_repo_are_allowed_even_outside_home(monkeypatch):
    """**说明书许诺了"仓目录可以", 规则就必须真的放行仓目录。**

    原规则只认 `$HOME` 或 `/tmp`。Mac 上仓都在 `~/dev` 下, 于是说明书那句"放仓目录下"
    **碰巧**成立; Windows 上 robot-learning 的仓在 `D:/g1-imitation`, 既不在 $HOME 下,
    也没有 /tmp ⇒ 说明书说能发的地方, 规则拒绝。
    这里用 Mac 的 /var/folders (tempfile 默认位置, 不在 $HOME 也不在 /tmp) 冒充"别的盘上的仓"。
    """
    repo = Path(tempfile.mkdtemp(prefix="fake-repo-"))            # → /var/folders/…
    assert not repo.resolve().is_relative_to(Path.home().resolve())
    assert not repo.resolve().is_relative_to(Path("/tmp").resolve())
    monkeypatch.setattr(b, "REPO", str(repo))
    f = repo / "data" / "out.xlsx"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"PK")
    _t, items = b._extract_media_markers(f"[[文件: {f}]]")
    assert items == [("file", str(f))], "仓目录里的文件被拒了 —— 说明书说可以"


def test_dot_rule_still_applies_inside_the_repo(monkeypatch):
    """放宽的是"仓目录", 不是"仓里的一切": `.git` `.env` 这类照样拒。"""
    repo = Path(tempfile.mkdtemp(prefix="fake-repo-"))
    monkeypatch.setattr(b, "REPO", str(repo))
    f = repo / ".git" / "notes.md"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x")
    text, items = b._extract_media_markers(f"[[文件: {f}]]")
    assert items == [] and "不允许" in text
