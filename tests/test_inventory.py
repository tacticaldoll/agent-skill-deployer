from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_skill_deployer import cli, inventory
from agent_skill_deployer.channels import CHANNELS
from agent_skill_deployer.core import Source
from agent_skill_deployer.core import RunResult


class FakeHost:
    key = "codex"
    label = "Codex"
    binary = ""

    def __init__(self, commit: str | None = None):
        self.commit = commit

    def available(self) -> bool:
        return True

    def installed_commit(self, _source: Source) -> str | None:
        return self.commit


class InventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source_path = self.root / "example"
        (self.source_path / "skills" / "orient-repo").mkdir(parents=True)
        self.source = Source(self.source_path, "example", "example")
        self.shared = self.root / "home" / ".agents" / "skills"
        self.profile = inventory.HostProfile(
            "codex",
            "plugin",
            (
                inventory.DiscoverySurface("plugin", None, authoritative=True),
                inventory.DiscoverySurface("shared-directory", self.shared),
            ),
            support="stable",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_plugin_and_shared_directory_are_reported_as_duplicate(self) -> None:
        (self.shared / "example-orient-repo").mkdir(parents=True)
        host = FakeHost("a" * 40)

        with patch.dict(inventory.HOST_PROFILES, {"codex": self.profile}, clear=True):
            installations = inventory.inventory_host(host, self.source)
            conflicts = inventory.detect_conflicts(host.key, installations)

        self.assertEqual([item.channel for item in installations], ["plugin", "shared-directory"])
        self.assertTrue(installations[0].authoritative)
        self.assertEqual(conflicts[0].kind, "duplicate-source")

    def test_codex_workspace_symlink_is_a_managed_additional_source(self) -> None:
        personal = self.root / ".codex" / "skills"
        personal.mkdir(parents=True)
        (personal / "orient-repo").symlink_to(
            self.source_path / "skills" / "orient-repo",
            target_is_directory=True,
        )
        profile = inventory.HostProfile(
            "codex",
            "plugin",
            (
                inventory.DiscoverySurface("plugin", None, authoritative=True),
                inventory.DiscoverySurface("host-directory", personal),
            ),
        )

        with patch.dict(inventory.HOST_PROFILES, {"codex": profile}, clear=True):
            installations = inventory.inventory_host(FakeHost("a" * 40), self.source)

        self.assertEqual(len(installations), 2)
        self.assertEqual(installations[1].ownership, "managed")
        self.assertEqual(installations[1].location, str(personal))

    def test_stale_managed_additional_skill_is_discovered_after_source_removal(self) -> None:
        stale = self.shared / "example-removed-skill"
        stale.mkdir(parents=True)
        (stale / self.source.distribution.provenance_file).write_text(
            json.dumps({
                "schema": 2,
                "distribution": "example",
                "source": str(self.source.path),
                "target": str(self.shared),
                "commit": "b" * 40,
            }),
            encoding="utf-8",
        )

        with patch.dict(inventory.HOST_PROFILES, {"codex": self.profile}, clear=True):
            installations = inventory.inventory_host(FakeHost("a" * 40), self.source)
            conflicts = inventory.detect_conflicts("codex", installations)

        self.assertEqual([item.channel for item in installations], ["plugin", "shared-directory"])
        self.assertEqual(installations[1].ownership, "managed")
        self.assertEqual(conflicts[0].kind, "duplicate-source")

    def test_current_bare_name_collision_is_reported_as_unverified(self) -> None:
        bare = self.shared / "orient-repo"
        bare.mkdir(parents=True)

        with patch.dict(inventory.HOST_PROFILES, {"codex": self.profile}, clear=True):
            installations = inventory.inventory_host(FakeHost("a" * 40), self.source)
            conflicts = inventory.detect_conflicts("codex", installations)

        self.assertEqual(installations[1].ownership, "legacy-unverified")
        self.assertEqual(
            [conflict.kind for conflict in conflicts],
            ["duplicate-source", "unverified-ownership"],
        )

    def test_unmarked_stale_bare_name_is_not_attributed_to_example(self) -> None:
        stale_bare = self.shared / "removed-skill"
        stale_bare.mkdir(parents=True)

        with patch.dict(inventory.HOST_PROFILES, {"codex": self.profile}, clear=True):
            installations = inventory.inventory_host(FakeHost("a" * 40), self.source)

        self.assertEqual(len(installations), 1)

    def test_every_profile_has_one_authoritative_registered_channel(self) -> None:
        for profile in inventory.HOST_PROFILES.values():
            authoritative = [surface for surface in profile.surfaces if surface.authoritative]
            self.assertEqual(len(authoritative), 1, profile.key)
            self.assertEqual(authoritative[0].channel, profile.authoritative_channel)
            self.assertIn(profile.authoritative_channel, CHANNELS)

    def test_foreign_directory_is_not_modified(self) -> None:
        skill = self.shared / "example-orient-repo"
        skill.mkdir(parents=True)
        marker = skill / "user-file.txt"
        marker.write_text("keep me", encoding="utf-8")

        with patch.dict(inventory.HOST_PROFILES, {"codex": self.profile}, clear=True):
            installations = inventory.inventory_host(FakeHost(), self.source)

        self.assertEqual(installations[0].ownership, "legacy-unverified")
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep me")

    def test_shared_provenance_is_managed_for_every_consumer_host(self) -> None:
        skill = self.shared / "example-orient-repo"
        skill.mkdir(parents=True)
        (skill / self.source.distribution.provenance_file).write_text(
            json.dumps({
                "schema": 2,
                "distribution": "example",
                "source": str(self.source.path),
                "host": "cline",
                "target": str(self.shared),
            }),
            encoding="utf-8",
        )
        profiles = {
            "cline": inventory.HostProfile(
                "cline", "shared-directory",
                (inventory.DiscoverySurface("shared-directory", self.shared, authoritative=True),),
            ),
            "codex": self.profile,
        }
        cline = FakeHost()
        cline.key = "cline"

        with patch.dict(inventory.HOST_PROFILES, profiles, clear=True):
            cline_install = inventory.inventory_host(cline, self.source)
            codex_install = inventory.inventory_host(FakeHost(), self.source)

        self.assertEqual(cline_install[0].ownership, "managed")
        self.assertEqual(codex_install[0].ownership, "managed")

    def test_schema_one_host_binding_migrates_as_legacy_not_foreign(self) -> None:
        skill = self.shared / "example-orient-repo"
        skill.mkdir(parents=True)
        (skill / self.source.distribution.provenance_file).write_text(
            json.dumps({
                "schema": 1,
                "distribution": "example",
                "source": str(self.source.path),
                "host": "cline",
            }),
            encoding="utf-8",
        )

        with patch.dict(inventory.HOST_PROFILES, {"codex": self.profile}, clear=True):
            installations = inventory.inventory_host(FakeHost(), self.source)

        self.assertEqual(installations[0].ownership, "legacy-unverified")

    def test_release_source_treats_path_based_schema_two_as_legacy(self) -> None:
        skill = self.shared / "example-orient-repo"
        skill.mkdir(parents=True)
        (skill / self.source.distribution.provenance_file).write_text(
            json.dumps({
                "schema": 2,
                "distribution": "example",
                "source": str(self.source.path),
                "target": str(self.shared),
                "commit": "a" * 40,
            }),
            encoding="utf-8",
        )
        release_source = Source(
            self.source.path,
            "example",
            "example",
            "https://example.com/example",
            self.source.policy,
            expected_commit="a" * 40,
            release_ref="refs/tags/v0.1.0",
        )

        with patch.dict(inventory.HOST_PROFILES, {"codex": self.profile}, clear=True):
            installations = inventory.inventory_host(FakeHost(), release_source)

        self.assertEqual(installations[0].ownership, "legacy-unverified")

    def test_private_schema_one_matching_host_is_unambiguous_managed(self) -> None:
        private = self.root / "home" / ".cursor" / "skills"
        skill = private / "example-orient-repo"
        skill.mkdir(parents=True)
        (skill / self.source.distribution.provenance_file).write_text(
            json.dumps({
                "schema": 1,
                "distribution": "example",
                "source": str(self.source.path),
                "host": "cursor",
            }),
            encoding="utf-8",
        )
        profile = inventory.HostProfile(
            "cursor", "host-directory",
            (inventory.DiscoverySurface("host-directory", private, authoritative=True),),
        )
        host = FakeHost()
        host.key = "cursor"

        with patch.dict(inventory.HOST_PROFILES, {"cursor": profile}, clear=True):
            installations = inventory.inventory_host(host, self.source)

        self.assertEqual(installations[0].ownership, "managed")

    def test_codex_does_not_treat_stale_record_as_installed_plugin(self) -> None:
        host = FakeHost("a" * 40)
        host.binary = "codex"

        with (
            patch.dict(inventory.HOST_PROFILES, {"codex": self.profile}, clear=True),
            patch.object(
                inventory,
                "run",
                return_value=RunResult(
                    ["codex", "plugin", "list"], 0,
                    "example@example  not installed  /tmp/example", "",
                ),
            ),
        ):
            installations = inventory.inventory_host(host, self.source)

        self.assertEqual(installations, [])

    def test_codex_accepts_installed_enabled_status(self) -> None:
        host = FakeHost("a" * 40)
        host.binary = "codex"

        with (
            patch.dict(inventory.HOST_PROFILES, {"codex": self.profile}, clear=True),
            patch.object(
                inventory,
                "run",
                return_value=RunResult(
                    ["codex", "plugin", "list"], 0,
                    "example@example  installed, enabled  0.1.0  /tmp/example", "",
                ),
            ),
        ):
            installations = inventory.inventory_host(host, self.source)

        self.assertEqual(installations[0].channel, "plugin")
        self.assertTrue(installations[0].authoritative)

    def test_copilot_directory_record_does_not_imply_plugin_presence(self) -> None:
        host = FakeHost("a" * 40)
        host.key = "copilot"
        profile = inventory.HostProfile(
            "copilot", "host-directory",
            (inventory.DiscoverySurface("plugin", None),),
        )

        with patch.dict(inventory.HOST_PROFILES, {"copilot": profile}, clear=True):
            installations = inventory.inventory_host(host, self.source)

        self.assertEqual(installations, [])

    def test_doctor_returns_nonzero_and_names_both_channels(self) -> None:
        (self.shared / "example-orient-repo").mkdir(parents=True)
        host = FakeHost("b" * 40)

        with (
            patch.dict(inventory.HOST_PROFILES, {"codex": self.profile}, clear=True),
            patch.object(cli, "HOST_PROFILES", inventory.HOST_PROFILES),
            patch.object(cli, "resolve_source", return_value=self.source),
            patch.object(cli, "get_hosts", return_value=[host]),
            patch("builtins.print") as output,
        ):
            code = cli.cmd_doctor(type("Args", (), {"source": None, "host": None})())

        rendered = "\n".join(" ".join(map(str, call.args)) for call in output.call_args_list)
        self.assertEqual(code, 1)
        self.assertIn("duplicate-source", rendered)
        self.assertIn("plugin", rendered)
        self.assertIn("shared-directory", rendered)
        self.assertIn("no changes were made", rendered)


if __name__ == "__main__":
    unittest.main()
