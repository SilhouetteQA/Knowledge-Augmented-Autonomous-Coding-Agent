# tools/git_diff_parse.py
"""git diff 文件块解析（通用工具）：quotepath 引号路径解码 + 删除文件提取。

git core.quotepath 默认开启：非 ASCII 路径按 C 风格整段引号包裹，字节以 \\ooo
八进制表示；本模块负责解码并提取文件级结构（新增/删除/修改），供交付一致性
核查等纯 diff 分析场景使用（与 git 子进程无耦合，输入为 diff 文本）。
"""
import re
from dataclasses import dataclass


@dataclass
class DiffFile:
    """diff 中一个文件块：a 侧相对路径（已解引号）+ 是否为整文件删除。"""
    path: str
    deleted: bool = False


def unquote_git_path(s: str) -> str:
    """解析 git 引号包裹的路径：\\ooo 八进制字节序列 / \\" / \\\\ / 字面字符 → UTF-8 解码。"""
    if not (s.startswith('"') and '"' in s[1:]):
        return s
    out = bytearray()
    i = 1
    while i < len(s):
        c = s[i]
        if c == '"':
            break
        if c == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt in ('"', "\\"):
                out += nxt.encode("utf-8")
                i += 2
                continue
            if nxt.isdigit():
                digits = nxt
                j = i + 2
                while j < len(s) and s[j].isdigit() and len(digits) < 3:
                    digits += s[j]
                    j += 1
                out.append(int(digits, 8) & 0xFF)
                i = j
                continue
            out += ("\\" + nxt).encode("utf-8")
            i += 2
            continue
        out += c.encode("utf-8")
        i += 1
    return out.decode("utf-8", errors="replace")


def _strip_a_prefix(p: str) -> str:
    """去掉路径的 a/ 前缀（可能包在引号内）。"""
    p = p.strip()
    if p.startswith('"'):
        inner = unquote_git_path(p)
        return inner[2:] if inner.startswith("a/") else inner
    return p[2:] if p.startswith("a/") else p


def _header_a_path(rest: str) -> str:
    """diff --git 头的 a 侧路径：完整引号对或第一个未引号 ' b/' 分隔。"""
    if rest.startswith('"'):
        parsed = unquote_git_path(rest)
        return _strip_a_prefix(parsed)
    idx = rest.find(" b/")
    return _strip_a_prefix(rest[:idx] if idx != -1 else rest)


def parse_diff_files(diff: str) -> list[DiffFile]:
    """解析 diff 的文件块：diff --git 头 + deleted file mode 标记，路径取 a 侧。

    删除块固定带 deleted file mode 与 --- a/<path> 行（git 输出可能带尾部制表符，
    先 strip 再取路径）；改造块只取 diff --git 头路径（带空格文件名 git 不引号包裹，
    直接整行取用）。
    """
    files: list[DiffFile] = []
    cur: DiffFile | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            cur = DiffFile(path=_header_a_path(line[len("diff --git "):]))
            files.append(cur)
            continue
        if line.startswith("--- ") and cur is not None:
            cur.path = _strip_a_prefix(line[4:])
            continue
        if line.startswith("deleted file mode") and cur is not None:
            cur.deleted = True
    # 统一正斜杠（git diff 恒用 /，Windows os.sep 为 \\ 会破坏比较）
    for f in files:
        f.path = f.path.replace("\\", "/")
    return [f for f in files if f.path]


def parse_deleted_paths(diff: str) -> list[str]:
    """diff 中删除文件（deleted file mode 块）的路径列表，按出现顺序。"""
    return [f.path for f in parse_diff_files(diff) if f.deleted]
