"""Fixed-stage deployment orchestration with injected channel adapters."""

from __future__ import annotations

from dataclasses import dataclass
import shutil
from typing import Callable

from .channels import CHANNELS, DeployOptions, DeploymentChannel
from .core import DeploymentError, Source, record_deploy
from .inventory import HOST_PROFILES, detect_conflicts, discover_directory_entries, inventory_host


@dataclass(frozen=True)
class PipelineResult:
    host: str
    stages: tuple[str, ...]


class DeploymentPipeline:
    def __init__(
        self,
        channels: dict[str, DeploymentChannel] | None = None,
        recorder: Callable[[str, Source, str | None], None] = record_deploy,
    ):
        self.channels = CHANNELS if channels is None else channels
        self.recorder = recorder

    @staticmethod
    def _check_preconditions(conflicts, options: DeployOptions) -> None:
        foreign = {
            item.location
            for conflict in conflicts
            for item in conflict.installations
            if item.ownership == "foreign"
        }
        if foreign:
            raise DeploymentError(
                "foreign distribution-like installation requires manual resolution: "
                + ", ".join(sorted(foreign))
            )
        legacy = {
            item.location
            for conflict in conflicts
            for item in conflict.installations
            if item.ownership == "legacy-unverified"
        }
        if legacy and not options.adopt_legacy:
            raise DeploymentError(
                "unowned or legacy distribution source requires explicit reconciliation: "
                + ", ".join(sorted(legacy))
            )

    @staticmethod
    def _reconcile_additional_directories(
        source: Source, profile, installations, adopt_legacy: bool, log
    ) -> None:
        for item in installations:
            removable = item.ownership == "managed" or (
                adopt_legacy and item.ownership == "legacy-unverified"
            )
            if item.authoritative or not removable:
                continue
            root = next(
                (surface.path for surface in profile.surfaces if str(surface.path) == item.location),
                None,
            )
            if root is None:
                continue
            for entry in discover_directory_entries(root, source, item.host):
                removable_entry = entry.ownership == "managed" or (
                    adopt_legacy and entry.ownership == "legacy-unverified"
                )
                if not removable_entry:
                    continue
                if entry.path.is_dir() and not entry.path.is_symlink():
                    shutil.rmtree(entry.path)
                elif entry.path.exists() or entry.path.is_symlink():
                    entry.path.unlink()
            log(
                f"removed reconciled non-authoritative {source.display_name} paths "
                f"from {root}"
            )

    @staticmethod
    def _verify_authoritative(host, source: Source, profile):
        installations = inventory_host(host, source)
        if not any(item.authoritative for item in installations):
            raise DeploymentError(
                f"verification failed: {host.key} authoritative channel "
                f"{profile.authoritative_channel} was not discovered after deployment"
            )
        return installations

    def run(self, host, source: Source, options: DeployOptions, log) -> PipelineResult:
        stages: list[str] = []
        stages.append("inventory")
        before = inventory_host(host, source)
        conflicts = detect_conflicts(host.key, before)
        self._check_preconditions(conflicts, options)
        stages.append("plan")
        profile = HOST_PROFILES[host.key]
        channel = self.channels.get(profile.authoritative_channel)
        if channel is None:
            raise DeploymentError(
                f"no deployment channel registered for {profile.authoritative_channel}"
            )
        log(f"channel={profile.authoritative_channel} stages=inventory→plan→apply→verify→record")
        stages.append("apply")
        channel.apply(host, source, options, log)
        stages.append("verify")
        if options.dry_run:
            for item in before:
                removable = item.ownership == "managed" or (
                    options.adopt_legacy and item.ownership == "legacy-unverified"
                )
                if not item.authoritative and removable:
                    log(
                        f"[dry-run] would remove non-authoritative "
                        f"{source.display_name} paths from {item.location}"
                    )
        if not options.dry_run:
            after = self._verify_authoritative(host, source, profile)
            self._reconcile_additional_directories(
                source, profile, after, options.adopt_legacy, log
            )
            remaining = detect_conflicts(host.key, inventory_host(host, source))
            if remaining:
                raise DeploymentError(
                    f"verification failed: conflicting {source.display_name} sources "
                    "remain after reconciliation"
                )
        stages.append("record")
        if not options.dry_run:
            self.recorder(host.key, source, source.head())
        return PipelineResult(host.key, tuple(stages))
