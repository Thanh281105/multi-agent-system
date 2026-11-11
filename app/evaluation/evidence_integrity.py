"""Read-only, byte-exact audits of supplied evidence manifests.

An intact manifest proves file consistency, not experimental validity. Never
repair a frozen hash or normalize source bytes to make an audit pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from app.evaluation.protocol import canonical_sha256

_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*\Z")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _newline_recovery(content: bytes, digest: str) -> tuple[str, bytes] | None:
    lf = content.replace(b"\r\n", b"\n")
    for mode, candidate in (("lf", lf), ("crlf", lf.replace(b"\n", b"\r\n"))):
        if candidate != content and hashlib.sha256(candidate).hexdigest() == digest:
            return mode, candidate
    return None


def audit_manifest(manifest_path: Path) -> dict[str, Any]:
    """Return every detected integrity issue, without opening unsafe paths."""
    issues: list[dict[str, str]] = []
    checked: list[dict[str, Any]] = []

    def issue(code: str, name: str) -> None:
        issues.append({"code": code, "path": name})

    report: dict[str, Any] = {
        "schema_version": "evidence_integrity_v1",
        "validation_scope": "file_integrity_only_not_semantic_validation",
        "status": "invalid",
        "manifest_name": manifest_path.name,
        "checked_files": checked,
        "line_ending_recovery_candidates": [],
        "issues": issues,
    }
    if (
        manifest_path.is_symlink()
        or manifest_path.parent.is_symlink()
        or manifest_path.parent.is_junction()
    ):
        issue("unsafe_manifest", manifest_path.name)
        return report
    try:
        raw = manifest_path.read_bytes()
        payload = json.loads(raw, object_pairs_hook=_unique_object)
    except (OSError, ValueError, UnicodeError):
        issue("unreadable_manifest", manifest_path.name)
        return report
    report["manifest_file_sha256"] = hashlib.sha256(raw).hexdigest()
    if not isinstance(payload, dict):
        issue("invalid_manifest", manifest_path.name)
        return report
    if "manifest_sha256" in payload:
        expected = payload["manifest_sha256"]
        try:
            actual = canonical_sha256(
                {
                    key: value
                    for key, value in payload.items()
                    if key != "manifest_sha256"
                }
            )
        except (TypeError, ValueError):
            actual = None
        if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
            issue("invalid_manifest_digest", manifest_path.name)
        elif actual != expected:
            issue("manifest_digest_mismatch", manifest_path.name)
    entries = payload.get("files")
    if entries is None and isinstance(payload.get("file_digests"), dict):
        entries = [
            {"path": name, "sha256": digest}
            for name, digest in payload["file_digests"].items()
        ]
    if not isinstance(entries, list) or not entries:
        issue("missing_file_entries", manifest_path.name)
        return report
    names: set[str] = set()
    base = manifest_path.parent.resolve()
    for entry in entries:
        if not isinstance(entry, dict):
            issue("invalid_file_entry", manifest_path.name)
            continue
        name = entry.get("path")
        if (
            not isinstance(name, str)
            or any(
                part in {".", ".."} or not _NAME.fullmatch(part)
                for part in name.split("/")
            )
            or name == manifest_path.name
        ):
            issue("unsafe_file_path", manifest_path.name)
            continue
        if name in names:
            issue("duplicate_file_path", name)
            continue
        names.add(name)
        expected_hash = entry.get("sha256")
        if not isinstance(expected_hash, str) or not _SHA256.fullmatch(expected_hash):
            issue("invalid_file_digest", name)
            continue
        size = entry.get("size_bytes")
        if "size_bytes" in entry and (type(size) is not int or size < 0):
            issue("invalid_file_size", name)
            continue
        path = base.joinpath(*PurePosixPath(name).parts)
        if (
            path.is_symlink()
            or path.is_junction()
            or not path.resolve().is_relative_to(base)
            or any(
                parent.is_symlink() or parent.is_junction()
                for parent in path.parents
                if parent != base and parent.is_relative_to(base)
            )
        ):
            issue("unsafe_file_path", name)
            continue
        try:
            content = path.read_bytes()
        except OSError:
            issue("missing_or_unreadable_file", name)
            continue
        actual_hash = hashlib.sha256(content).hexdigest()
        checked.append(
            {"path": name, "sha256": actual_hash, "size_bytes": len(content)}
        )
        if size is not None and len(content) != size:
            issue("file_size_mismatch", name)
        if actual_hash != expected_hash:
            issue("file_digest_mismatch", name)
            recovered = _newline_recovery(content, expected_hash)
            if recovered is not None and (size is None or len(recovered[1]) == size):
                report["line_ending_recovery_candidates"].append(
                    {
                        "path": name,
                        "line_endings": recovered[0],
                        "sha256": expected_hash,
                    }
                )
    report["status"] = "valid" if not issues else "invalid"
    return report


def recover_line_endings(manifest_path: Path, output: Path) -> dict[str, Any]:
    """Restore only bytes matching existing digests into a new bundle.

    Missing files, unrecognized edits, unsafe paths and manifest hash drift are
    irrecoverable here. No hash is rewritten and the supplied bundle is untouched.
    """
    if output.exists():
        raise FileExistsError("recovery output already exists")
    if output.resolve().is_relative_to(manifest_path.parent.resolve()):
        raise ValueError("recovery output must be outside the source bundle")
    report = audit_manifest(manifest_path)
    if any(
        item["code"] not in {"file_size_mismatch", "file_digest_mismatch"}
        for item in report["issues"]
    ):
        raise ValueError("bundle has issues beyond recoverable line endings")
    manifest_bytes = manifest_path.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != report.get("manifest_file_sha256"):
        raise ValueError("manifest changed during recovery")
    payload = json.loads(manifest_bytes, object_pairs_hook=_unique_object)
    entries = payload.get("files") or [
        {"path": name, "sha256": digest}
        for name, digest in payload["file_digests"].items()
    ]
    restored: dict[str, bytes] = {}
    for entry in entries:
        name, digest = entry["path"], entry["sha256"]
        content = (manifest_path.parent / name).read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            recovered = _newline_recovery(content, digest)
            if recovered is None:
                raise ValueError("file bytes cannot be recovered to the frozen digest")
            content = recovered[1]
        if "size_bytes" in entry and len(content) != entry["size_bytes"]:
            raise ValueError("recovered size does not match the manifest")
        restored[name] = content
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".newline-recovery-", dir=output.parent))
    try:
        for name, content in restored.items():
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (staging / manifest_path.name).write_bytes(manifest_bytes)
        recovered_report = audit_manifest(staging / manifest_path.name)
        if recovered_report["status"] != "valid":
            raise ValueError("recovered bundle failed integrity validation")
        staging.rename(output)
        return recovered_report
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recover-line-endings-to", type=Path)
    arguments = parser.parse_args(argv)
    report = audit_manifest(arguments.manifest)
    if arguments.recover_line_endings_to is not None:
        # Check the audit destination before creating any recovery artifacts.
        if arguments.output.exists():
            parser.exit(1, "Audit output already exists; choose a new file.\n")
        try:
            recovered_report = recover_line_endings(
                arguments.manifest, arguments.recover_line_endings_to
            )
        except (OSError, ValueError):
            report["recovery_status"] = "refused_missing_or_irrecoverable_evidence"
        else:
            report["recovery_status"] = recovered_report["status"]
            report["recovered_bundle"] = arguments.recover_line_endings_to.name
    # An audit can record failures. It must never overwrite the original evidence.
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    with arguments.output.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    print(f"Evidence integrity: {report['status']}; issues={len(report['issues'])}")
    return 0 if report.get("recovery_status", report["status"]) == "valid" else 1


if __name__ == "__main__":
    raise SystemExit(main())
