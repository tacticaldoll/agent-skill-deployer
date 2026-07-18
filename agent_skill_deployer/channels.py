"""Deployment-channel adapters used by the fixed deployment pipeline."""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from . import core
from .core import DeploymentError, RunResult, Source
from .hosts import Host, _rewrite_name
from .inventory import (
    HOST_PROFILES,
    classify_ownership,
    discover_directory_entries,
)


@dataclass(frozen=True)
class DeployOptions:
    dry_run: bool = False
    adopt_legacy: bool = False


class DeploymentChannel(Protocol):
    name: str

    def apply(self, host: Host, source: Source, options: DeployOptions, log) -> list[RunResult]: ...


class NativeHostChannel:
    """Plugin/extension channel whose lifecycle is owned by the host CLI."""

    def __init__(self, name: str):
        self.name = name

    def apply(self, host: Host, source: Source, options: DeployOptions, log) -> list[RunResult]:
        previous_dry_run = core.DRY_RUN
        core.set_dry_run(options.dry_run)
        try:
            results = host.deploy(source, log)
        finally:
            core.set_dry_run(previous_dry_run)
        failed = next((result for result in results if not result.ok), None)
        if failed:
            raise DeploymentError(
                f"channel command failed: {' '.join(failed.cmd)}\n{failed.err or failed.out}"
            )
        return results


class SkillsDirectoryChannel:
    name = "skills-directory"

    @staticmethod
    def _provenance(source: Source, host: Host, name: str, target: Path) -> dict:
        return {
            "schema": 3,
            "distribution": source.distribution.identity,
            "source": source.source_identity,
            "commit": source.head(),
            "host": host.key,
            "channel": "skills-directory",
            "target": str(target),
            "skill": name,
            "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    @staticmethod
    def _ownership(path: Path, source: Source, host: Host) -> str:
        ownership, _record = classify_ownership(path, source, host.key)
        return ownership

    def _target(self, host: Host) -> Path:
        profile = HOST_PROFILES[host.key]
        surface = next((
            surface for surface in profile.surfaces
            if surface.authoritative and surface.path is not None
        ), None)
        if surface is None:
            raise DeploymentError(f"{host.key} has no authoritative directory surface")
        return surface.path

    def _install_one(
        self,
        host: Host,
        source: Source,
        target: Path,
        skills_dir: Path,
        name: str,
        adopt_legacy: bool,
    ) -> None:
        prefix = source.distribution.prefix
        dest = target / f"{prefix}{name}"
        if dest.exists() or dest.is_symlink():
            ownership = self._ownership(dest, source, host)
            if ownership == "foreign":
                raise DeploymentError(f"refusing to replace foreign installation: {dest}")
            if ownership != "managed" and not adopt_legacy:
                raise DeploymentError(
                    f"refusing to replace unowned installation: {dest}; "
                    f"inspect it, then pass --adopt-legacy to replace this exact "
                    f"{source.display_name} path"
                )
        staging = target / f".{prefix}{name}.staging-{uuid.uuid4().hex}"
        backup = target / f".{prefix}{name}.backup-{uuid.uuid4().hex}"
        try:
            shutil.copytree(skills_dir / name, staging)
            _rewrite_name(staging / "SKILL.md", f"{prefix}{name}")
            _rewrite_name(staging / "skill.yaml", f"{prefix}{name}")
            (staging / source.distribution.provenance_file).write_text(
                json.dumps(self._provenance(source, host, name, target), indent=2) + "\n",
                encoding="utf-8",
            )
            had_dest = dest.exists() or dest.is_symlink()
            if had_dest:
                dest.rename(backup)
            try:
                staging.rename(dest)
            except Exception:
                if had_dest and backup.exists():
                    backup.rename(dest)
                raise
            if backup.is_dir() and not backup.is_symlink():
                shutil.rmtree(backup)
            elif backup.exists() or backup.is_symlink():
                backup.unlink()
        finally:
            if staging.is_dir():
                shutil.rmtree(staging)
            if backup.exists() and not dest.exists():
                backup.rename(dest)

    def _prune(
        self,
        host: Host,
        source: Source,
        target: Path,
        names: list[str],
        adopt_legacy: bool,
    ) -> None:
        desired = {f"{source.distribution.prefix}{name}" for name in names}
        for entry in discover_directory_entries(target, source, host.key):
            if entry.path.name in desired:
                continue
            removable = entry.ownership == "managed" or (
                adopt_legacy and entry.ownership == "legacy-unverified"
            )
            if not removable:
                continue
            if entry.path.is_dir() and not entry.path.is_symlink():
                shutil.rmtree(entry.path)
            elif entry.path.exists() or entry.path.is_symlink():
                entry.path.unlink()

    def apply(self, host: Host, source: Source, options: DeployOptions, log) -> list[RunResult]:
        target = self._target(host)
        skills_dir = source.skills_path
        phase = "reading source skills"
        completed = 0
        total: int | None = None
        try:
            names = sorted(entry.name for entry in skills_dir.iterdir() if entry.is_dir())
            total = len(names)
            phase = "checking source skills"
            symlink = next((entry for entry in skills_dir.rglob("*") if entry.is_symlink()), None)
            if symlink is not None:
                raise DeploymentError(f"refusing to deploy symlink from source skill tree: {symlink}")
            if options.dry_run:
                log(f"[dry-run] reconcile {total} managed skills into {target}")
                return []
            phase = "creating the target directory"
            target.mkdir(parents=True, exist_ok=True)
            for name in names:
                phase = f"installing {name}"
                self._install_one(
                    host, source, target, skills_dir, name, options.adopt_legacy
                )
                completed += 1
            phase = "pruning stale entries"
            self._prune(host, source, target, names, options.adopt_legacy)
        except OSError as exc:
            total_label = str(total) if total is not None else "unknown"
            raise DeploymentError(
                f"directory deployment failed while {phase} after {completed}/{total_label} "
                f"skills completed; completed swaps remain valid and retry is safe: {exc}"
            ) from exc
        log(
            f"installed {len(names)} managed '{source.distribution.prefix}*' "
            f"skills into {target}"
        )
        return []


CHANNELS: dict[str, DeploymentChannel] = {
    "plugin": NativeHostChannel("plugin"),
    "extension": NativeHostChannel("extension"),
    "host-directory": SkillsDirectoryChannel(),
    "shared-directory": SkillsDirectoryChannel(),
}
