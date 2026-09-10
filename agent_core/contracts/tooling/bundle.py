"""确定性 transport bundle：生成、校验与消费端原子提升。

bundle 只是 **transport**，不是第三个真相源。archive hash 可以记录，
但**永远不等于也不替代** Contract Payload Hash（Master Spec §12.7）。

确定性措施：

```text
ZIP_STORED（不压缩）      —— deflate 输出依赖 zlib 版本，会破坏跨平台确定性
条目按规范化相对路径排序
date_time 固定为 1980-01-01
create_system / external_attr 固定
条目内容为 canonical 文本（换行 LF、JSON canonical）
```

消费端流程（Master Spec §12.7）：解包到临时目录 → 校验 allowlist、完整性、descriptor、
Schema 可重放性、payload hash → 全部通过后才原子替换目标镜像；任一步失败保留原镜像不变。
"""
from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from agent_core.contracts.tooling.canonical_json import (
    PAYLOAD_PACKAGE,
    canonical_file_bundle_digest,
    file_canonical_content,
    iter_payload_files,
    normalize_relative_path,
    sha256_hex,
)
from agent_core.contracts.tooling.verify_payload import VerifyReport, verify_tree
from agent_core.contracts.version import CONTRACT_VERSION

#: 固定的 ZIP 条目时间戳，消除 mtime 差异。
FIXED_DATE_TIME: Final[tuple[int, int, int, int, int, int]] = (1980, 1, 1, 0, 0, 0)

#: 固定权限位（rw-r--r--），消除 permissions 差异。
FIXED_EXTERNAL_ATTR: Final[int] = 0o644 << 16

#: 消费端工作子目录名（位于目标仓库根，处理后即删除）。
INCOMING_DIRNAME: Final[str] = ".agent-core-bundle.incoming"
PREVIOUS_DIRNAME: Final[str] = ".agent-core-bundle.previous"


class BundleError(RuntimeError):
    """bundle 生成或应用失败。"""


@dataclass
class BundleResult:
    archive_path: Path
    archive_hash: str
    payload_hash: str
    file_count: int

    def as_dict(self) -> dict:
        return {
            "archive_path": str(self.archive_path),
            "archive_hash": self.archive_hash,
            "payload_hash": self.payload_hash,
            "file_count": self.file_count,
        }


@dataclass
class ApplyResult:
    applied: bool
    target_root: Path
    payload_hash: str = ""
    file_count: int = 0
    errors: list[str] = field(default_factory=list)
    rolled_back: bool = False

    @property
    def ok(self) -> bool:
        return self.applied and not self.errors


def default_archive_name(version: str = CONTRACT_VERSION) -> str:
    return f"contract-payload-{version}.zip"


def create_bundle(repo_root: Path, out_path: Path) -> BundleResult:
    """把 payload 打成确定性 transport bundle。

    payload 自身必须自洽；不自洽时直接失败，不产出 bundle。
    """
    report = verify_tree(repo_root)
    if not report.ok:
        raise BundleError("payload 不自洽，拒绝产出 bundle：" + "; ".join(report.errors))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    files = iter_payload_files(repo_root)

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for path in files:
            rel = normalize_relative_path(path, repo_root)
            data = file_canonical_content(path).encode("utf-8")
            info = zipfile.ZipInfo(filename=rel, date_time=FIXED_DATE_TIME)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 0
            info.external_attr = FIXED_EXTERNAL_ATTR
            archive.writestr(info, data)

    return BundleResult(
        archive_path=out_path,
        archive_hash=sha256_hex(out_path.read_bytes()),
        payload_hash=report.payload_hash,
        file_count=len(files),
    )


def inspect_bundle(archive_path: Path) -> dict:
    """列出 bundle 条目与 archive hash（只读）。"""
    with zipfile.ZipFile(archive_path) as archive:
        names = sorted(info.filename for info in archive.infolist())
    return {
        "archive_hash": sha256_hex(archive_path.read_bytes()),
        "entry_count": len(names),
        "entries": names,
    }


def _extract(archive_path: Path, work_root: Path) -> Path:
    """解包到工作目录，返回 payload 所在的仓库根。"""
    if work_root.exists():
        shutil.rmtree(work_root)
    work_root.mkdir(parents=True)
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            name = info.filename
            # 防御 zip slip：拒绝绝对路径与上跳路径
            if name.startswith("/") or ".." in Path(name).parts:
                raise BundleError(f"bundle 含非法条目路径：{name}")
            target = work_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(info))
    return work_root


def extract_and_verify(
    archive_path: Path, expected_payload_hash: str, work_root: Path
) -> tuple[Path, VerifyReport]:
    """解包并完整校验，返回 ``(解包出的仓库根, 校验报告)``。"""
    unpacked_root = _extract(archive_path, work_root)
    report = verify_tree(unpacked_root, expected_payload_hash=expected_payload_hash)
    return unpacked_root, report


def apply_bundle(
    archive_path: Path,
    target_root: Path,
    expected_payload_hash: str,
    work_root: Path | None = None,
) -> ApplyResult:
    """原子替换 ``target_root/agent_core`` 为 bundle 中的镜像。

    任一步失败都会保留原镜像不变，并清理工作目录。
    """
    target_root = target_root.resolve()
    incoming = (work_root or target_root) / INCOMING_DIRNAME
    previous = target_root / PREVIOUS_DIRNAME
    destination = target_root / PAYLOAD_PACKAGE
    result = ApplyResult(applied=False, target_root=target_root)

    try:
        unpacked_root, report = extract_and_verify(archive_path, expected_payload_hash, incoming)
    except (BundleError, zipfile.BadZipFile) as exc:
        result.errors.append(f"解包失败：{exc}")
        shutil.rmtree(incoming, ignore_errors=True)
        return result

    if not report.ok:
        result.errors.extend(report.errors)
        shutil.rmtree(incoming, ignore_errors=True)
        return result

    staged = unpacked_root / PAYLOAD_PACKAGE
    if not staged.is_dir():
        result.errors.append(f"bundle 内缺少 {PAYLOAD_PACKAGE}/")
        shutil.rmtree(incoming, ignore_errors=True)
        return result

    try:
        if previous.exists():
            shutil.rmtree(previous)
        if destination.exists():
            destination.rename(previous)
        try:
            staged.rename(destination)
        except OSError:
            # 回滚：把原镜像放回去
            if previous.exists() and not destination.exists():
                previous.rename(destination)
                result.rolled_back = True
            raise
        result.applied = True
        result.payload_hash = report.payload_hash
        result.file_count = report.file_count
    except OSError as exc:
        result.errors.append(f"原子替换失败：{exc}")
    finally:
        shutil.rmtree(incoming, ignore_errors=True)
        shutil.rmtree(previous, ignore_errors=True)

    # 替换后再复算一次目标树，确认落地结果与声明一致
    if result.applied:
        landed_hash, landed_files = canonical_file_bundle_digest(target_root)
        if landed_hash != expected_payload_hash:
            result.errors.append(
                f"落地校验失败：目标树 hash {landed_hash} != 期望 {expected_payload_hash}"
            )
            result.applied = False
        result.payload_hash = landed_hash
        result.file_count = len(landed_files)

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Foundation Contract payload transport bundle")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="生成确定性 bundle")
    create.add_argument("--repo-root", type=Path, default=None)
    create.add_argument("--out", type=Path, required=True)

    inspect = sub.add_parser("inspect", help="列出 bundle 条目")
    inspect.add_argument("--archive", type=Path, required=True)

    apply = sub.add_parser("apply", help="校验并原子提升到目标仓库")
    apply.add_argument("--archive", type=Path, required=True)
    apply.add_argument("--target-root", type=Path, required=True)
    apply.add_argument("--expect-payload-hash", required=True)

    args = parser.parse_args(argv)
    from agent_core.contracts.tooling import generate_schemas as gs

    if args.command == "create":
        repo_root = args.repo_root or gs.find_repo_root()
        result = create_bundle(repo_root, args.out)
        print("bundle created")
        print(f"  archive_path  : {result.archive_path}")
        print(f"  archive_hash  : {result.archive_hash}")
        print(f"  payload_hash  : {result.payload_hash}")
        print(f"  file_count    : {result.file_count}")
        return 0

    if args.command == "inspect":
        info = inspect_bundle(args.archive)
        print(f"archive_hash : {info['archive_hash']}")
        print(f"entries      : {info['entry_count']}")
        for name in info["entries"]:
            print(f"  {name}")
        return 0

    result = apply_bundle(args.archive, args.target_root, args.expect_payload_hash)
    if result.ok:
        print("bundle applied")
        print(f"  target_root   : {result.target_root}")
        print(f"  payload_hash  : {result.payload_hash}")
        print(f"  file_count    : {result.file_count}")
        return 0

    print("bundle apply FAILED:", file=sys.stderr)
    for error in result.errors:
        print(f"  {error}", file=sys.stderr)
    if result.rolled_back:
        print("  (已回滚到原镜像)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
