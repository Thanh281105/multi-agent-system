"""Byte-exact provenance for a clean, explicitly committed evaluation source."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.evaluation.protocol import canonical_json_bytes, canonical_sha256


class SourceIdentityError(RuntimeError):
    """Stable admission failure without source contents or credentials."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SourceFileIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


class SourceIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["evaluation_source_v1"] = "evaluation_source_v1"
    git_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    files: tuple[SourceFileIdentity, ...] = Field(min_length=1)
    identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def check_identity(self) -> SourceIdentity:
        paths = [item.path for item in self.files]
        if paths != sorted(set(paths)):
            raise ValueError("source paths must be unique and sorted")
        for value in paths:
            path = PurePosixPath(value)
            if path.is_absolute() or ".." in path.parts or "\\" in value:
                raise ValueError("source path is unsafe")
        if (
            canonical_sha256(self.model_dump(mode="json", exclude={"identity_sha256"}))
            != self.identity_sha256
        ):
            raise ValueError("source identity hash mismatch")
        return self


def _git(root: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceIdentityError("source_git_unavailable") from exc
    if result.returncode != 0:
        raise SourceIdentityError("source_git_unavailable")
    return result.stdout


def capture_source_identity(root: Path) -> SourceIdentity:
    """Reject ancestor repositories, dirty trees, symlinks and ignored app code."""
    root = root.resolve()
    top = Path(_git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if top != root:
        raise SourceIdentityError("source_repository_root_mismatch")
    revision = _git(root, "rev-parse", "--verify", "HEAD").decode().strip()
    if _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all"):
        raise SourceIdentityError("source_tree_dirty")
    paths = sorted(
        item.decode("utf-8")
        for item in _git(root, "ls-files", "-z").split(b"\0")
        if item
    )
    tracked = set(paths)
    for directory in (root / "app", root / "migrations"):
        for path in directory.rglob("*"):
            if path.suffix in {".py", ".json"} and path.is_file():
                if path.relative_to(root).as_posix() not in tracked:
                    raise SourceIdentityError("source_runtime_file_untracked")
    files = []
    for relative in paths:
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise SourceIdentityError("source_path_unsafe")
        if path.name == ".env" or (
            path.name.startswith(".env.") and path.name != ".env.example"
        ):
            raise SourceIdentityError("source_secret_file_tracked")
        if not path.is_file():
            raise SourceIdentityError("source_file_unavailable")
        content = path.read_bytes()
        files.append(
            SourceFileIdentity(
                path=relative,
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
            )
        )
    if revision != _git(root, "rev-parse", "--verify", "HEAD").decode().strip() or _git(
        root, "status", "--porcelain=v1", "-z", "--untracked-files=all"
    ):
        raise SourceIdentityError("source_changed_during_capture")
    payload = {
        "schema_version": "evaluation_source_v1",
        "git_revision": revision,
        "files": [item.model_dump(mode="json") for item in files],
    }
    return SourceIdentity.model_validate(
        {
            **payload,
            "identity_sha256": canonical_sha256(payload),
        }
    )


def require_same_source(root: Path, identity: SourceIdentity) -> None:
    if capture_source_identity(root) != identity:
        raise SourceIdentityError("source_identity_drift")


def freeze_source_identity(path: Path, identity: SourceIdentity) -> None:
    if path.exists():
        if SourceIdentity.model_validate_json(path.read_bytes()) != identity:
            raise SourceIdentityError("stored_source_identity_drift")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(canonical_json_bytes(identity))
