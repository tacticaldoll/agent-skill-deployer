"""Read-only inventory of every distribution source visible to an agent host."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .core import Source, run


@dataclass(frozen=True)
class DiscoverySurface:
    channel: str
    path: Path | None
    scope: str = "personal"
    authoritative: bool = False


@dataclass(frozen=True)
class HostProfile:
    key: str
    authoritative_channel: str
    surfaces: tuple[DiscoverySurface, ...]
    support: str = "experimental"


@dataclass(frozen=True)
class Installation:
    host: str
    channel: str
    scope: str
    location: str
    authoritative: bool
    ownership: str
    commit: str | None = None


@dataclass(frozen=True)
class Conflict:
    host: str
    kind: str
    installations: tuple[Installation, ...]


@dataclass(frozen=True)
class DirectoryEntry:
    path: Path
    ownership: str
    commit: str | None = None


def _home(path: str) -> Path:
    return Path.home() / path


# A profile describes every surface a host can see, not only the surface the CLI writes.
HOST_PROFILES: dict[str, HostProfile] = {
    "claude": HostProfile(
        "claude", "plugin",
        (DiscoverySurface("plugin", None, authoritative=True),),
        support="stable",
    ),
    "codex": HostProfile(
        "codex", "plugin",
        (
            DiscoverySurface("plugin", None, authoritative=True),
            DiscoverySurface("host-directory", _home(".codex/skills")),
            DiscoverySurface("shared-directory", _home(".agents/skills")),
        ),
        support="stable",
    ),
    "antigravity": HostProfile(
        "antigravity", "host-directory",
        (DiscoverySurface("host-directory", _home(".gemini/config/skills"), authoritative=True),),
    ),
    "gemini": HostProfile(
        "gemini", "extension",
        (
            DiscoverySurface("extension", None, authoritative=True),
            DiscoverySurface("shared-directory", _home(".agents/skills")),
            DiscoverySurface("host-directory", _home(".gemini/skills")),
        ),
        support="beta",
    ),
    "copilot": HostProfile(
        "copilot", "host-directory",
        (
            DiscoverySurface("host-directory", _home(".copilot/skills"), authoritative=True),
            DiscoverySurface("shared-directory", _home(".agents/skills")),
            DiscoverySurface("plugin", None),
        ),
        support="beta",
    ),
    "cline": HostProfile(
        "cline", "host-directory",
        (DiscoverySurface("host-directory", _home(".cline/skills"), authoritative=True),),
        support="beta",
    ),
    "cursor": HostProfile(
        "cursor", "host-directory",
        (DiscoverySurface("host-directory", _home(".cursor/skills"), authoritative=True),),
    ),
    "opencode": HostProfile(
        "opencode", "host-directory",
        (
            DiscoverySurface("host-directory", _home(".config/opencode/skills"), authoritative=True),
            DiscoverySurface("host-directory", _home(".opencode/skills")),
        ),
    ),
}


def schema_one_is_unambiguous(
    record: dict, source: Source, host: str, target: Path
) -> bool:
    """Whether host-bound schema 1 proves ownership on a single-consumer surface."""
    consumers = {
        profile.key
        for profile in HOST_PROFILES.values()
        for surface in profile.surfaces
        if surface.path == target
    }
    return (
        record.get("schema") == 1
        and record.get("distribution") == source.distribution.identity
        and record.get("source") == str(source.path)
        and record.get("host") == host
        and consumers == {host}
    )


def source_skill_names(source: Source) -> set[str]:
    skills = source.skills_path
    if not skills.is_dir():
        return set()
    return {entry.name for entry in skills.iterdir() if entry.is_dir()}


def classify_ownership(path: Path, source: Source, host: str) -> tuple[str, dict | None]:
    """Classify one provenance-bearing directory entry for inventory and mutation."""
    provenance = path / source.distribution.provenance_file
    if not provenance.is_file():
        return "legacy-unverified", None
    try:
        record = json.loads(provenance.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "foreign", None
    same_distribution = record.get("distribution") == source.distribution.identity
    if not same_distribution:
        return "foreign", record
    same_source = record.get("source") == source.source_identity
    if record.get("schema") == 3 and same_source and record.get("target") == str(path.parent):
        return "managed", record
    if record.get("schema") == 2:
        legacy_local_match = record.get("source") == str(source.path)
        if source.expected_commit is not None:
            return "legacy-unverified", record
        if legacy_local_match and record.get("target") == str(path.parent):
            return "managed", record
        return "foreign", record
    if record.get("schema") == 1 and record.get("source") == str(source.path):
        ownership = (
            "managed"
            if schema_one_is_unambiguous(record, source, host, path.parent)
            else "legacy-unverified"
        )
        return ownership, record
    return "foreign", record


def _is_workspace_skill_link(path: Path, source: Source) -> bool:
    if not path.is_symlink():
        return False
    target = path.resolve(strict=False)
    skills = source.skills_path.resolve()
    return target.parent == skills and target.name == path.name


def discover_directory_entries(path: Path, source: Source, host: str) -> list[DirectoryEntry]:
    """Discover attributable entries, including stale names and workspace links."""
    if not path.is_dir():
        return []
    current_names = source_skill_names(source)
    entries: list[DirectoryEntry] = []
    for entry in path.iterdir():
        if _is_workspace_skill_link(entry, source):
            entries.append(DirectoryEntry(entry, "managed", source.head()))
            continue
        attributable = (
            entry.name.startswith(source.distribution.prefix)
            or entry.name in current_names
            or (entry / source.distribution.provenance_file).is_file()
        )
        if not attributable:
            continue
        ownership, record = classify_ownership(entry, source, host)
        commit = record.get("commit") if record else None
        entries.append(DirectoryEntry(entry, ownership, commit))
    return entries


def _directory_installation(
    path: Path, source: Source, host: str
) -> tuple[bool, str, str | None]:
    entries = discover_directory_entries(path, source, host)
    if not entries:
        return False, "absent", None
    if any(entry.ownership == "foreign" for entry in entries):
        return True, "foreign", None
    if any(entry.ownership == "legacy-unverified" for entry in entries):
        return True, "legacy-unverified", None
    commits = {entry.commit for entry in entries if entry.ownership == "managed"}
    if commits:
        commit = commits.pop() if len(commits) == 1 else None
        return True, "managed", commit
    return True, "legacy-unverified", None


def _channel_commit(host, source: Source, channel: str) -> tuple[bool, str | None, str]:
    """Return presence, commit, and evidence for a non-directory channel."""
    commit = host.installed_commit(source)
    if host.key == "codex" and channel == "plugin" and getattr(host, "binary", None):
        result = run([host.binary, "plugin", "list"])
        installed = False
        if result.ok:
            for line in result.out.splitlines():
                fields = line.split()
                if (
                    len(fields) >= 2
                    and fields[0] == source.selector
                    and fields[1].rstrip(",") == "installed"
                ):
                    installed = True
                    break
        return installed, commit if installed else None, "host-cli"
    if host.key == "gemini" and channel == "extension" and getattr(host, "binary", None):
        result = run([host.binary, "extensions", "list"])
        installed = result.ok and any(
            source.marketplace in line.split() for line in result.out.splitlines()
        )
        return installed, commit if installed else None, "host-cli"
    if host.key == "copilot" and channel == "plugin":
        # The current Copilot deploy driver records a directory deployment. That record cannot
        # establish whether a separate Copilot plugin is installed.
        return False, None, "unverified"
    # Claude exposes an installed-plugin registry through installed_commit. Other channel
    # probes remain explicitly recorded rather than claiming host-ground-truth discovery.
    return commit is not None, commit, "recorded"


def inventory_host(host, source: Source) -> list[Installation]:
    """Inspect known discovery surfaces without changing host state."""
    profile = HOST_PROFILES[host.key]
    found: list[Installation] = []
    for surface in profile.surfaces:
        if surface.path is None:
            if surface.channel not in {"plugin", "extension"}:
                continue
            present, commit, evidence = _channel_commit(host, source, surface.channel)
            if present:
                found.append(Installation(
                    host.key, surface.channel, surface.scope,
                    f"{source.selector} ({surface.channel})", surface.authoritative,
                    evidence, commit,
                ))
            continue
        present, ownership, commit = _directory_installation(surface.path, source, host.key)
        if present:
            found.append(Installation(
                host.key, surface.channel, surface.scope, str(surface.path),
                surface.authoritative, ownership, commit,
            ))
    return found


def detect_conflicts(host: str, installations: Iterable[Installation]) -> list[Conflict]:
    items = tuple(installations)
    conflicts: list[Conflict] = []
    if len(items) > 1:
        conflicts.append(Conflict(host, "duplicate-source", items))
    if items and not any(item.authoritative for item in items):
        conflicts.append(Conflict(host, "non-authoritative-source", items))
    legacy = tuple(item for item in items if item.ownership == "legacy-unverified")
    if legacy:
        conflicts.append(Conflict(host, "unverified-ownership", legacy))
    foreign = tuple(item for item in items if item.ownership == "foreign")
    if foreign:
        conflicts.append(Conflict(host, "foreign-source", foreign))
    return conflicts
