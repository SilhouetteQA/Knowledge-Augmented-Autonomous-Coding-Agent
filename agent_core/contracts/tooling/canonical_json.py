"""canonical JSON 与 canonical file bundle。

`Contract Payload Hash` 的输入范围与规范化口径（Master Spec §12.3–§12.4）：

```text
输入：agent_core/__init__.py + agent_core/contracts/**
排除：__pycache__、.pyc、临时文件、mtime、permissions、绝对路径

canonical file bundle：
  相对路径分隔符统一为 /
  → 按路径排序
  → UTF-8、不带 BOM
  → 文本 CRLF/CR 归一为 LF
  → JSON 文件做 canonical JSON
  → 拼接 path + NUL + content + NUL
  → SHA256
```

Python 与 Markdown **不做** AST 或语义规范化，只做编码与换行规范化；
因此注释或格式变化也会改变 Payload Hash —— 这正是镜像一致性检查期望的行为。

canonical JSON 的实现只有一处：:func:`agent_core.contracts.models.base.canonical_json_dumps`。
本模块复用它而不是另写一份，避免出现两套"同一算法"的实现。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Final

from agent_core.contracts.models.base import canonical_json_dumps as _canonical_json_dumps

#: Payload 的根包名。
PAYLOAD_PACKAGE: Final[str] = "agent_core"

#: Payload 允许的顶层形状：包根 ``__init__.py`` 与 ``contracts/**``。
PAYLOAD_TOP_LEVEL_INIT: Final[str] = "agent_core/__init__.py"
PAYLOAD_CONTRACTS_PREFIX: Final[str] = "agent_core/contracts/"

#: 目录名排除（缓存与版本控制元数据）。
EXCLUDED_DIR_NAMES: Final[frozenset[str]] = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git", ".svn"}
)

#: 后缀排除（字节码与临时文件）。
EXCLUDED_SUFFIXES: Final[tuple[str, ...]] = (".pyc", ".pyo", ".pyd", ".tmp", ".lock", ".orig", ".rej")

#: 视为 JSON 的后缀；这些文件在 bundle 前重新做 canonical JSON。
JSON_SUFFIXES: Final[tuple[str, ...]] = (".json",)


def canonical_json_dumps(value: object) -> str:
    """返回 canonical JSON 文本（sorted keys、compact separators、非 ASCII 原样）。"""
    return _canonical_json_dumps(value)


def canonical_json_bytes(value: object) -> bytes:
    return canonical_json_dumps(value).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """返回 ``sha256:<hex>`` 形式的内容标识。"""
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def read_text(path: Path) -> str:
    """以 UTF-8 读取并剥离 BOM；非 UTF-8（疑似二进制）显式失败。

    Payload 必须全部是文本：出现二进制文件说明白名单被污染。
    """
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:  # pragma: no cover - 白名单污染时才触发
        raise ValueError(f"payload 文件不是 UTF-8 文本，疑似二进制：{path}") from exc


def normalize_text(text: str) -> str:
    """换行规范化：CRLF 与孤立 CR 一律归一为 LF。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def normalize_relative_path(path: Path, root: Path) -> str:
    """把绝对路径规范化为相对 payload 根的 POSIX 路径。"""
    return path.resolve().relative_to(root.resolve()).as_posix()


def is_excluded(path: Path) -> bool:
    """判断路径是否属于缓存 / 临时文件排除项。"""
    if any(part in EXCLUDED_DIR_NAMES for part in path.parts):
        return True
    name = path.name
    if name.endswith(EXCLUDED_SUFFIXES):
        return True
    return name.endswith("~")


def iter_payload_files(root: Path) -> list[Path]:
    """列出 payload 内全部受控文件，按规范化相对路径排序。

    :param root: 仓库根（``agent_core`` 的父目录）。
    """
    package_dir = root / PAYLOAD_PACKAGE
    if not package_dir.is_dir():
        raise FileNotFoundError(f"找不到 payload 根包：{package_dir}")

    files: list[Path] = []
    for candidate in package_dir.rglob("*"):
        if not candidate.is_file():
            continue
        if is_excluded(candidate):
            continue
        files.append(candidate)

    files.sort(key=lambda item: normalize_relative_path(item, root))
    return files


def file_canonical_content(path: Path) -> str:
    """返回单个文件的 canonical 文本内容。

    JSON 文件重新序列化为 canonical JSON；其余文本只做换行规范化。
    """
    text = normalize_text(read_text(path))
    if path.suffix.lower() in JSON_SUFFIXES:
        return canonical_json_dumps(json.loads(text))
    return text


def canonical_file_bundle_digest(
    root: Path, files: Iterable[Path] | None = None
) -> tuple[str, dict[str, str]]:
    """计算 canonical file bundle 的 SHA256。

    :returns: ``(payload_hash, per_file_hashes)``，其中 per_file_hashes 是
        ``相对路径 -> sha256:...`` 的映射，用于诊断镜像差异。
    """
    selected = list(files) if files is not None else iter_payload_files(root)

    blob = bytearray()
    per_file: dict[str, str] = {}
    for path in selected:
        rel = normalize_relative_path(path, root)
        content = file_canonical_content(path)
        blob += rel.encode("utf-8") + b"\x00" + content.encode("utf-8") + b"\x00"
        per_file[rel] = sha256_hex(content.encode("utf-8"))

    return sha256_hex(bytes(blob)), per_file


def schema_content_hash(schema: Mapping[str, object]) -> str:
    """单个 JSON Schema 的规范化哈希。"""
    return sha256_hex(canonical_json_bytes(schema))


def schema_set_hash(schema_hashes: Mapping[str, str]) -> str:
    """按 schema ID 排序后对 ``id + NUL + hash`` 序列取 SHA256。

    只用于快速诊断 Payload 差异，**不是**第三套治理身份。
    """
    joined = "\x00".join(f"{schema_id}\x00{schema_hashes[schema_id]}" for schema_id in sorted(schema_hashes))
    return sha256_hex(joined.encode("utf-8"))


__all__ = [
    "PAYLOAD_PACKAGE",
    "PAYLOAD_TOP_LEVEL_INIT",
    "PAYLOAD_CONTRACTS_PREFIX",
    "EXCLUDED_DIR_NAMES",
    "EXCLUDED_SUFFIXES",
    "JSON_SUFFIXES",
    "canonical_json_dumps",
    "canonical_json_bytes",
    "sha256_hex",
    "read_text",
    "normalize_text",
    "normalize_relative_path",
    "is_excluded",
    "iter_payload_files",
    "file_canonical_content",
    "canonical_file_bundle_digest",
    "schema_content_hash",
    "schema_set_hash",
]
