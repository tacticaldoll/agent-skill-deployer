"""Distribution/source models, subprocess helpers, and local deployment state."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

CONFIG_DIR = Path(
    os.environ.get("AGENT_SKILL_DEPLOYER_HOME")
    or (Path.home() / ".config" / "agent-skill-deployer")
)
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_PATH = CONFIG_DIR / "state.json"
RELEASE_CACHE = Path(
    os.environ.get("AGENT_SKILL_DEPLOYER_CACHE")
    or (Path.home() / ".cache" / "agent-skill-deployer" / "releases")
)

# When True, run() skips commands marked mutating=True and reports them instead.
DRY_RUN = False


def set_dry_run(value: bool) -> None:
    global DRY_RUN
    DRY_RUN = value


class DeploymentError(Exception):
    """A user-facing failure; the CLI prints the message and exits non-zero."""


@dataclass
class RunResult:
    cmd: list[str]
    code: int
    out: str
    err: str
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0


def run(cmd, *, check: bool = False, cwd=None, mutating: bool = False) -> RunResult:
    """Run a command. In dry-run, mutating commands are reported, not executed."""
    if DRY_RUN and mutating:
        return RunResult(list(cmd), 0, "[dry-run] not executed", "", skipped=True)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    except FileNotFoundError as exc:
        raise DeploymentError(f"required tool not found: {cmd[0]}") from exc
    res = RunResult(list(cmd), proc.returncode, proc.stdout.strip(), proc.stderr.strip())
    if check and not res.ok:
        raise DeploymentError(
            f"command failed ({res.code}): {' '.join(cmd)}\n{res.err or res.out}"
        )
    return res


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


@dataclass(frozen=True)
class DistributionPolicy:
    """Distribution-owned naming and source-layout policy consumed by the engine."""

    identity: str
    prefix: str
    provenance_file: str
    skills_directory: str = "skills"
    display_name: str | None = None
    marketplace: str | None = None
    plugin: str | None = None
    validation_commands: tuple[tuple[str, ...], ...] = ()

    @classmethod
    def named(cls, identity: str, *, display_name: str | None = None) -> "DistributionPolicy":
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", identity):
            raise DeploymentError(
                "distribution identity must use lowercase letters, digits, and hyphens"
            )
        return cls(
            identity=identity,
            prefix=f"{identity}-",
            provenance_file=f".{identity}-install.json",
            display_name=display_name,
        )

    @classmethod
    def from_distribution_manifest(cls, path: Path) -> "DistributionPolicy":
        """Load vendor-neutral distribution identity and layout metadata."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise DeploymentError(f"cannot read distribution manifest: {path}") from exc
        except json.JSONDecodeError as exc:
            raise DeploymentError(f"invalid JSON in distribution manifest: {path}") from exc
        if not isinstance(data, dict) or data.get("schema") != 1:
            raise DeploymentError(f"unsupported distribution manifest schema: {path}")
        identity = data.get("name")
        display_name = data.get("display_name")
        skills_directory = data.get("skills_directory")
        if not isinstance(identity, str):
            raise DeploymentError(f"distribution manifest name is missing: {path}")
        policy = cls.named(
            identity,
            display_name=display_name if isinstance(display_name, str) else None,
        )
        if (
            not isinstance(skills_directory, str)
            or Path(skills_directory).is_absolute()
            or len(Path(skills_directory).parts) != 1
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", skills_directory)
        ):
            raise DeploymentError(
                f"distribution manifest skills_directory must be one relative directory: {path}"
            )
        return cls(
            identity=policy.identity,
            prefix=policy.prefix,
            provenance_file=policy.provenance_file,
            skills_directory=skills_directory,
            display_name=policy.display_name,
        )


@dataclass
class Source:
    """A portable Agent Skills distribution to deploy."""

    path: Path
    marketplace: str
    plugin: str
    origin: str | None = None
    policy: DistributionPolicy | None = None
    expected_commit: str | None = None
    release_ref: str | None = None

    def __post_init__(self) -> None:
        if self.policy is None:
            self.policy = DistributionPolicy.named(self.marketplace)

    @classmethod
    def load(
        cls, path: Path, policy: DistributionPolicy | None = None
    ) -> "Source":
        path = Path(path).expanduser().resolve()
        if not path.is_dir():
            raise DeploymentError(f"source is not a directory: {path}")
        if policy is None:
            manifest = path / "distribution.json"
            policy = (
                DistributionPolicy.from_distribution_manifest(manifest)
                if manifest.is_file()
                else DistributionPolicy.named(path.name)
            )
        marketplace = policy.marketplace or policy.identity
        plugin = policy.plugin or policy.identity
        remote = run(["git", "-C", str(path), "remote", "get-url", "origin"])
        origin = remote.out if remote.ok and remote.out else None
        return cls(
            path=path,
            marketplace=marketplace,
            plugin=plugin,
            origin=origin,
            policy=policy,
        )

    @property
    def distribution(self) -> DistributionPolicy:
        assert self.policy is not None
        return self.policy

    @property
    def display_name(self) -> str:
        return self.distribution.display_name or self.distribution.identity

    @property
    def skills_path(self) -> Path:
        return self.path / self.distribution.skills_directory

    @property
    def display_location(self) -> str:
        if self.origin and self.release_ref:
            return f"{self.origin}#{self.release_ref}"
        return str(self.path)

    @property
    def source_identity(self) -> str:
        """Stable ownership identity across release commits and local cache paths."""
        return self.origin if self.expected_commit and self.origin else str(self.path)

    @property
    def selector(self) -> str:
        return f"{self.plugin}@{self.marketplace}"

    @property
    def formal_source(self) -> str:
        if self.origin is None:
            raise DeploymentError(
                "formal deployment requires a remote Git origin; configure origin before deploying"
            )
        origin = self.origin.strip()
        remote_url = re.match(r"^(?:https?|ssh|git)://[^\s]+$", origin)
        scp_url = re.match(r"^[^/\\\s@]+@[^:\s]+:.+$", origin)
        if not remote_url and not scp_url:
            raise DeploymentError(
                "formal deployment requires a remote Git origin; local paths and file URLs "
                "are development sources"
            )
        return origin

    def require_formal_checkout(self) -> None:
        """Fail closed unless every host can deploy a committed source snapshot."""
        _ = self.formal_source
        head = self.head()
        if head is None:
            raise DeploymentError("formal deployment requires a committed Git HEAD")
        if self.expected_commit is not None and head != self.expected_commit:
            raise DeploymentError(
                f"formal snapshot HEAD {head} does not match resolved release "
                f"{self.expected_commit}"
            )
        if self.is_dirty():
            raise DeploymentError(
                "formal deployment requires a clean working tree; commit or discard local "
                "changes before deploying"
            )

    @property
    def state_key(self) -> str:
        """Stable local identity; repositories with the same marketplace must not share state."""
        return f"{self.marketplace}@{self.source_identity}"

    def head(self) -> str | None:
        r = run(["git", "-C", str(self.path), "rev-parse", "HEAD"])
        return r.out or None if r.ok else None

    def is_dirty(self) -> bool:
        """True if the git working tree has uncommitted changes (tracked or untracked)."""
        r = run(["git", "-C", str(self.path), "status", "--porcelain"])
        return bool(r.ok and r.out.strip())

    def validate(self) -> RunResult | None:
        """Run validation commands declared by the distribution policy."""
        commands = self.distribution.validation_commands
        if not commands:
            return None
        results: list[RunResult] = []
        for command in commands:
            result = run(list(command), cwd=str(self.path))
            results.append(result)
            if not result.ok:
                return result
        return RunResult(
            [part for result in results for part in result.cmd],
            0,
            "\n".join(result.out for result in results if result.out),
            "",
        )


@dataclass(frozen=True)
class GitRelease:
    """A tagged remote release materialized as a managed, immutable local snapshot."""

    remote: str
    tag: str
    policy: DistributionPolicy
    expected_version: str | None = None
    version_manifest: str | None = None
    cache_root: Path = RELEASE_CACHE

    @property
    def tag_ref(self) -> str:
        return self.tag if self.tag.startswith("refs/tags/") else f"refs/tags/{self.tag}"

    def _resolve_remote(self) -> str:
        probe = Source(Path("."), self.policy.marketplace or self.policy.identity,
                       self.policy.plugin or self.policy.identity, self.remote, self.policy)
        remote = probe.formal_source
        peeled = f"{self.tag_ref}^{{}}"
        result = run(
            ["git", "ls-remote", remote, "HEAD", self.tag_ref, peeled],
            check=True,
        )
        refs: dict[str, str] = {}
        for line in result.out.splitlines():
            fields = line.split()
            if len(fields) == 2:
                refs[fields[1]] = fields[0]
        commit = refs.get(peeled) or refs.get(self.tag_ref)
        if commit is None:
            raise DeploymentError(f"release tag not found on remote: {self.tag_ref}")
        remote_head = refs.get("HEAD")
        if remote_head != commit:
            raise DeploymentError(
                f"formal deployment requires remote HEAD and {self.tag_ref} to resolve "
                f"to the same commit; HEAD={remote_head or 'missing'} tag={commit}"
            )
        return commit

    def _source(self, path: Path, commit: str) -> Source:
        return Source(
            path=path,
            marketplace=self.policy.marketplace or self.policy.identity,
            plugin=self.policy.plugin or self.policy.identity,
            origin=self.remote,
            policy=self.policy,
            expected_commit=commit,
            release_ref=self.tag_ref,
        )

    def _verify_version(self, source: Source) -> None:
        if self.expected_version is None and self.version_manifest is None:
            return
        if self.expected_version is None or self.version_manifest is None:
            raise DeploymentError(
                "release version verification requires both expected_version and "
                "version_manifest"
            )
        manifest = source.path / self.version_manifest
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise DeploymentError(f"release version manifest not found: {manifest}") from exc
        except json.JSONDecodeError as exc:
            raise DeploymentError(f"invalid release version manifest {manifest}: {exc}") from exc
        if data.get("version") != self.expected_version:
            raise DeploymentError(
                f"release manifest version {data.get('version')!r} does not match "
                f"expected {self.expected_version!r}"
            )

    def _verified_source(self, path: Path, commit: str) -> Source:
        source = self._source(path, commit)
        source.require_formal_checkout()
        actual_origin = run(
            ["git", "-C", str(path), "remote", "get-url", "origin"], check=True
        ).out
        if actual_origin != self.remote:
            raise DeploymentError(
                f"release cache origin mismatch at {path}: {actual_origin}"
            )
        self._verify_version(source)
        return source

    def materialize(self) -> Source:
        """Resolve the tag, enforce native-host parity, and return a clean cached checkout."""
        commit = self._resolve_remote()
        target = self.cache_root / self.policy.identity / commit
        if target.exists():
            return self._verified_source(target, commit)

        staging = target.parent / f".{commit}.staging-{uuid.uuid4().hex}"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            run(
                ["git", "clone", "--no-checkout", "--filter=blob:none", self.remote,
                 str(staging)],
                check=True,
            )
            run(
                ["git", "-C", str(staging), "checkout", "--detach", commit],
                check=True,
            )
            self._verified_source(staging, commit)
            staging.rename(target)
        except OSError as exc:
            if target.exists():
                return self._verified_source(target, commit)
            raise DeploymentError(f"unable to materialize release snapshot: {exc}") from exc
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return self._verified_source(target, commit)


def short(commit: str | None) -> str:
    return commit[:7] if commit else "-"


def _read_json(path: Path, *, fail_closed: bool) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        if fail_closed:
            raise DeploymentError(f"invalid JSON in {path}: {exc}") from exc
        print(f"warning: ignoring corrupt deployment state {path}: {exc}", file=sys.stderr)
        return {}
    except OSError as exc:
        raise DeploymentError(f"unable to read {path}: {exc}") from exc


def load_config() -> dict:
    return _read_json(CONFIG_PATH, fail_closed=True)


def save_config(cfg: dict) -> None:
    _write_json(CONFIG_PATH, cfg)


def load_state() -> dict:
    return _read_json(STATE_PATH, fail_closed=False)


def save_state(state: dict) -> None:
    _write_json(STATE_PATH, state)


def _write_json(path: Path, value: dict) -> None:
    """Atomically replace a small JSON state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.parent / f".{path.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        staging.replace(path)
    finally:
        if staging.exists():
            staging.unlink()


def record_deploy(host_key: str, source: Source, commit: str | None) -> None:
    state = load_state()
    state.setdefault(source.state_key, {})[host_key] = {
        "commit": commit,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    save_state(state)


def recorded_commit(host_key: str, source: Source) -> str | None:
    return (
        load_state().get(source.state_key, {}).get(host_key, {}).get("commit")
    )
