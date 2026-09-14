"""Coding 仓的 FileEvidenceSink（项目本地实现，与 Wiki 版本独立）。

共享契约来自 agent_core.contracts；本模块只负责本仓的落盘策略：
    <root>/<run_id>/events/<event_id>.json
根目录默认 output/contract-validation/staging，可用 AGENT_CONTRACT_EVIDENCE_DIR 覆盖。

原子写入（Master Spec 11.1）：canonical serialize -> 同目录 .json.tmp ->
flush/fsync/close -> os.replace。读取端只看 .json，.tmp 属于未完成的过程态。

失败处理：递增进程内 failure counter、写结构化日志、抛 SinkFailure
（evidence.* 基础设施码）。绝不静默丢弃合法记录，也绝不在失败时递归再调 sink。
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

from agent_core.contracts.enums.evidence import (
    EVIDENCE_INVALID_RUN_ID,
    EVIDENCE_SINK_WRITE_FAILED,
)
from agent_core.contracts.models.base import canonical_json_dumps
from agent_core.contracts.models.evidence import (
    EvidenceRecord,
    is_canonical_event_id,
    is_safe_run_id,
)
from agent_core.contracts.protocols.evidence_sink import SinkFailure

EVIDENCE_DIR_ENV = "AGENT_CONTRACT_EVIDENCE_DIR"
DEFAULT_EVIDENCE_ROOT = Path("output") / "contract-validation" / "staging"
EVENTS_DIRNAME = "events"
FORMAL_SUFFIX = ".json"
TEMP_SUFFIX = ".json.tmp"

logger = logging.getLogger(__name__)


def default_evidence_root() -> Path:
    """staging 根：环境变量优先，否则用缺省相对路径。"""
    raw = os.environ.get(EVIDENCE_DIR_ENV, "").strip()
    if raw:
        return Path(raw)
    return DEFAULT_EVIDENCE_ROOT


def _escapes(root: Path, target: Path) -> bool:
    """target 是否逃出 root（目录穿越的最后一道防线）。

    用 ``os.path.abspath`` 做纯词法规范化（不解析 symlink、不访问文件系统），
    避免 ``Path.resolve`` 对未 mkdir 目录的并发竞态（Spec 09 并发测试暴露）。
    """
    base = Path(os.path.abspath(root))
    resolved = Path(os.path.abspath(target))
    return base != resolved and base not in resolved.parents


class FileEvidenceSink:
    """把 EvidenceRecord 原子写入本仓 staging 目录。"""

    def __init__(self, root: Path | str | None = None, *, logger: logging.Logger | None = None) -> None:
        self._root = Path(root) if root is not None else default_evidence_root()
        self._log = logger if logger is not None else logging.getLogger(__name__)
        self._failures = 0

    @property
    def root(self) -> Path:
        return self._root

    @property
    def sink_failure_count(self) -> int:
        """Smoke Gate 要求该值为 0（任何 sink 失败都令该 run 的证据失效）。"""
        return self._failures

    def events_dir(self, run_id: str) -> Path:
        """某个 run 的事件目录（不创建）。"""
        return self._root / run_id / EVENTS_DIRNAME

    def emit(self, record: EvidenceRecord) -> None:
        """原子写入一条记录；失败抛 SinkFailure。"""
        run_id, event_id = record.run_id, record.event_id

        # 模型层已校验；此处冗余再验，确保不安全标识绝不参与路径构造。
        if not is_safe_run_id(run_id) or not is_canonical_event_id(event_id):
            self._abort(
                EVIDENCE_INVALID_RUN_ID,
                f"不安全的证据标识 run_id={run_id!r} event_id={event_id!r}",
                run_id,
                event_id,
            )

        events = self.events_dir(run_id)
        formal = events / f"{event_id}{FORMAL_SUFFIX}"
        temp = events / f"{event_id}{TEMP_SUFFIX}"
        if _escapes(self._root, formal) or _escapes(self._root, temp):
            self._abort(
                EVIDENCE_INVALID_RUN_ID,
                "证据路径逃出 staging 根，拒绝写入",
                run_id,
                event_id,
            )

        blob = canonical_json_dumps(record.model_dump(mode="json")).encode("utf-8")

        try:
            events.mkdir(parents=True, exist_ok=True)
            handle = open(temp, "wb")
            try:
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                handle.close()
            os.replace(temp, formal)
        except OSError as exc:
            try:
                temp.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - 清理失败则留下 .tmp，由 Gate 发现
                self._log.warning("coding: 无法清理临时证据 %s", temp)
            self._abort(
                EVIDENCE_SINK_WRITE_FAILED,
                f"{type(exc).__name__}: {exc}",
                run_id,
                event_id,
            )

    def _abort(self, code: str, detail: str, run_id: str, event_id: str) -> None:
        """计一次失败、记结构化日志、抛 SinkFailure；不递归。"""
        self._failures += 1
        self._log.error(
            "evidence sink failure code=%s run_id=%s event_id=%s root=%s count=%d detail=%s",
            code,
            run_id,
            event_id,
            self._root,
            self._failures,
            detail,
        )
        raise SinkFailure(code, detail, event_id=event_id)


def iter_published_events(events_dir: Path) -> Iterator[Path]:
    """列出已发布的证据文件，只认 .json。"""
    if not events_dir.is_dir():
        return
    for item in sorted(events_dir.iterdir()):
        if item.is_file() and item.name.endswith(FORMAL_SUFFIX):
            yield item


def has_residual_temp_files(events_dir: Path) -> bool:
    """是否存在残留 .tmp；存在即表示该 run 证据不完整。"""
    if not events_dir.is_dir():
        return False
    return any(
        item.name.endswith(TEMP_SUFFIX) for item in events_dir.iterdir() if item.is_file()
    )


__all__ = [
    "EVIDENCE_DIR_ENV",
    "DEFAULT_EVIDENCE_ROOT",
    "EVENTS_DIRNAME",
    "FORMAL_SUFFIX",
    "TEMP_SUFFIX",
    "FileEvidenceSink",
    "default_evidence_root",
    "iter_published_events",
    "has_residual_temp_files",
]
