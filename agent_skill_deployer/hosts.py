"""Per-host deploy drivers.

Each host identifies availability and native plugin/extension lifecycle behavior.
Directory mutation belongs exclusively to the provenance-aware channel adapter.

Plugin hosts get their namespace from the source manifest. Directory-discovery hosts
have no manifest namespace, so the distribution policy's prefix is applied to the
installed directory and skill metadata. Source skills stay unchanged.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Callable

from . import core
from .core import RunResult, Source, have, recorded_commit, run

Logger = Callable[[str], None]


def _rewrite_name(path: Path, new_name: str) -> None:
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    new = re.sub(
        r"^name:\s*\S+",
        lambda _match: f"name: {new_name}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if new != text:
        path.write_text(new, encoding="utf-8")


class Host:
    key: str = ""
    label: str = ""
    binary: str = ""

    def available(self) -> bool:
        return have(self.binary)

    def installed_commit(self, source: Source) -> str | None:
        """Ground-truth commit if exposed; otherwise the engine's last record."""
        return recorded_commit(self.key, source)

    def deploy(self, source: Source, log: Logger) -> list[RunResult]:
        raise NotImplementedError

    def _replace_marketplace(self, source: Source, log: Logger) -> list[RunResult]:
        log(f"registering marketplace from {source.formal_source}")
        run(
            [self.binary, "plugin", "marketplace", "remove", source.marketplace],
            mutating=True,
        )
        return [
            run(
                [self.binary, "plugin", "marketplace", "add", source.formal_source],
                check=True,
                mutating=True,
            )
        ]


class Claude(Host):
    key = "claude"
    label = "Claude Code"
    binary = "claude"

    def installed_commit(self, source: Source) -> str | None:
        path = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            return None
        entries = data.get("plugins", {}).get(source.selector) or []
        if entries and isinstance(entries[0], dict):
            return entries[0].get("gitCommitSha")
        return None

    def deploy(self, source: Source, log: Logger) -> list[RunResult]:
        run([self.binary, "plugin", "uninstall", source.selector], mutating=True)
        results = self._replace_marketplace(source, log)
        log("updating marketplace")
        results.append(
            run([self.binary, "plugin", "marketplace", "update", source.marketplace],
                mutating=True)
        )
        # `plugin update` is version-gated: 0.1.0 -> 0.1.0 is skipped even when the commit
        # moved. Uninstall + install forces the cache to refresh.
        log("refreshing plugin (uninstall + install to bust the version-pinned cache)")
        results.append(
            run([self.binary, "plugin", "install", source.selector], check=True, mutating=True)
        )
        return results


class Codex(Host):
    key = "codex"
    label = "Codex"
    binary = "codex"

    def _cache_dir(self, source: Source) -> Path:
        return Path.home() / ".codex" / "plugins" / "cache" / source.marketplace

    def deploy(self, source: Source, log: Logger) -> list[RunResult]:
        log("refreshing plugin (remove + purge version-pinned cache + add)")
        run([self.binary, "plugin", "remove", source.selector], mutating=True)
        results = self._replace_marketplace(source, log)
        cache = self._cache_dir(source)
        if cache.exists():
            if core.DRY_RUN:
                log(f"[dry-run] would purge {cache}")
            else:
                shutil.rmtree(cache, ignore_errors=True)
        results.append(
            run([self.binary, "plugin", "add", source.selector], check=True, mutating=True)
        )
        return results


class DirDiscoveryHost(Host):
    """Open Agent Skills directory-discovery host availability metadata.

    The deployment target and namespace policy live in ``HostProfile`` and
    ``SkillsDirectoryChannel``. Candidates here are detection hints only.
    """

    candidates: tuple[Path, ...] = ()

    def available(self) -> bool:
        if self.binary and have(self.binary):
            return True
        return any(parent.exists() for parent in self.candidates)

class Antigravity(DirDiscoveryHost):
    key = "antigravity"
    label = "Antigravity"
    binary = "agy"
    candidates = (Path.home() / ".gemini" / "config",)

class Gemini(Host):
    """Gemini CLI — installs via `gemini extensions` into ~/.gemini/extensions/.

    `extensions install` is interactive by default (a security-consent prompt, then a
    settings step); a deploy must not block on a TTY, so it passes `--consent
    --skip-settings`. `extensions uninstall` takes no confirmation.
    """

    key = "gemini"
    label = "Gemini CLI"
    binary = "gemini"

    def deploy(self, source: Source, log: Logger) -> list[RunResult]:
        log("re-installing extension (gemini extensions)")
        run([self.binary, "extensions", "uninstall", source.marketplace], mutating=True)
        return [
            run([self.binary, "extensions", "install", source.formal_source,
                 "--consent", "--skip-settings"],
                check=True, mutating=True)
        ]


class Copilot(DirDiscoveryHost):
    key = "copilot"
    label = "GitHub Copilot CLI"
    binary = "copilot"
    candidates = (Path.home() / ".copilot",)


class Cline(DirDiscoveryHost):
    key = "cline"
    label = "Cline"
    candidates = (Path.home() / ".cline",)

    def available(self) -> bool:
        # ~/.agents is shared by several hosts and does not prove Cline itself is installed.
        return (Path.home() / ".cline").exists()


class Cursor(DirDiscoveryHost):
    key = "cursor"
    label = "Cursor"
    candidates = (Path.home() / ".cursor",)


class OpenCode(DirDiscoveryHost):
    key = "opencode"
    label = "OpenCode"
    candidates = (Path.home() / ".config" / "opencode", Path.home() / ".opencode")


ALL_HOSTS: list[Host] = [
    Claude(), Codex(), Antigravity(), Gemini(),
    Copilot(), Cline(), Cursor(), OpenCode(),
]
_BY_KEY = {h.key: h for h in ALL_HOSTS}


def get_hosts(keys: list[str] | None = None) -> list[Host]:
    if not keys:
        return list(ALL_HOSTS)
    out: list[Host] = []
    for key in keys:
        host = _BY_KEY.get(key)
        if host is None:
            from .core import DeploymentError

            raise DeploymentError(f"unknown host: {key} (known: {', '.join(_BY_KEY)})")
        out.append(host)
    return out
