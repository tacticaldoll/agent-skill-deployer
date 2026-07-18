from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_skill_deployer import core
from agent_skill_deployer.channels import DeployOptions, NativeHostChannel
from agent_skill_deployer.core import (
    DeploymentError,
    DistributionPolicy,
    RunResult,
    Source,
)
from agent_skill_deployer.inventory import DiscoverySurface, HOST_PROFILES, HostProfile
from agent_skill_deployer.pipeline import DeploymentPipeline


class DirectoryHost:
    key = "cursor"
    label = "Cursor"
    binary = ""

    def installed_commit(self, _source):
        return None


class NativeHost:
    key = "codex"

    def __init__(self):
        self.saw_dry_run = False

    def deploy(self, _source, _log):
        self.saw_dry_run = core.DRY_RUN
        return [RunResult(["host", "install"], 0, "", "", skipped=core.DRY_RUN)]


class FailingNativeHost(NativeHost):
    def deploy(self, _source, _log):
        return [RunResult(["host", "install"], 7, "", "failed")]


class PluginHost:
    key = "claude"

    def __init__(self):
        self.commit = None

    def installed_commit(self, _source):
        return self.commit


class FakePluginChannel:
    name = "plugin"

    def __init__(self, install: bool):
        self.install = install

    def apply(self, host, _source, _options, _log):
        if self.install:
            host.commit = "c" * 40
        return []


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source_path = self.root / "source"
        skill = self.source_path / "skills" / "orient-repo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: orient-repo\ndescription: test\n---\n", encoding="utf-8"
        )
        (skill / "skill.yaml").write_text("name: orient-repo\n", encoding="utf-8")
        self.source = Source(self.source_path, "example", "example")
        self.provenance_file = self.source.distribution.provenance_file
        self.target = self.root / "home" / ".cursor" / "skills"
        self.profile = HostProfile(
            "cursor", "host-directory",
            (DiscoverySurface("host-directory", self.target, authoritative=True),),
        )
        self.host = DirectoryHost()

    def add_source_skill(self, name: str) -> None:
        skill = self.source_path / "skills" / name
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: test\n---\n", encoding="utf-8"
        )
        (skill / "skill.yaml").write_text(f"name: {name}\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_pipeline(self, **kwargs):
        with patch.dict(HOST_PROFILES, {"cursor": self.profile}, clear=True):
            return DeploymentPipeline(recorder=lambda *_args: None).run(
                self.host, self.source, DeployOptions(**kwargs), lambda _message: None
            )

    def pipeline_with_recorder(self, recorder, **kwargs):
        with patch.dict(HOST_PROFILES, {"cursor": self.profile}, clear=True):
            return DeploymentPipeline(recorder=recorder).run(
                self.host, self.source, DeployOptions(**kwargs), lambda _message: None
            )

    def test_walking_skeleton_installs_and_verifies_managed_skill(self) -> None:
        result = self.run_pipeline()
        installed = self.target / "example-orient-repo"
        provenance = json.loads(
            (installed / self.provenance_file).read_text(encoding="utf-8")
        )

        self.assertEqual(result.stages, ("inventory", "plan", "apply", "verify", "record"))
        self.assertEqual(provenance["distribution"], "example")
        self.assertEqual(provenance["host"], "cursor")
        self.assertIn("name: example-orient-repo", (installed / "SKILL.md").read_text())

    def test_distribution_policy_controls_names_and_provenance(self) -> None:
        self.source.policy = DistributionPolicy.named("nebula", display_name="Nebula")

        self.run_pipeline()

        installed = self.target / "nebula-orient-repo"
        provenance = json.loads(
            (installed / ".nebula-install.json").read_text(encoding="utf-8")
        )
        self.assertEqual(provenance["distribution"], "nebula")
        self.assertIn("name: nebula-orient-repo", (installed / "SKILL.md").read_text())

    def test_dry_run_has_zero_mutation(self) -> None:
        records = []
        result = self.pipeline_with_recorder(lambda *args: records.append(args), dry_run=True)

        self.assertIn("apply", result.stages)
        self.assertFalse(self.target.exists())
        self.assertEqual(records, [])

    def test_legacy_install_fails_closed_without_adoption(self) -> None:
        legacy = self.target / "example-orient-repo"
        legacy.mkdir(parents=True)
        marker = legacy / "user.txt"
        marker.write_text("keep", encoding="utf-8")

        with self.assertRaisesRegex(DeploymentError, "explicit reconciliation"):
            self.run_pipeline()

        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_explicit_adoption_replaces_exact_legacy_path(self) -> None:
        legacy = self.target / "example-orient-repo"
        legacy.mkdir(parents=True)
        (legacy / "old.txt").write_text("old", encoding="utf-8")

        self.run_pipeline(adopt_legacy=True)

        self.assertFalse((legacy / "old.txt").exists())
        self.assertTrue((legacy / self.provenance_file).exists())

    def test_explicit_adoption_removes_current_bare_name_from_authoritative_surface(self) -> None:
        bare = self.target / "orient-repo"
        bare.mkdir(parents=True)
        (bare / "old.txt").write_text("old", encoding="utf-8")

        self.run_pipeline(adopt_legacy=True)

        self.assertFalse(bare.exists())
        self.assertTrue((self.target / "example-orient-repo").exists())

    def test_unambiguous_private_schema_one_auto_migrates(self) -> None:
        installed = self.target / "example-orient-repo"
        installed.mkdir(parents=True)
        (installed / self.provenance_file).write_text(
            json.dumps({
                "schema": 1,
                "distribution": "example",
                "source": str(self.source.path),
                "host": "cursor",
            }),
            encoding="utf-8",
        )

        self.run_pipeline()

        provenance = json.loads((installed / self.provenance_file).read_text(encoding="utf-8"))
        self.assertEqual(provenance["schema"], 3)
        self.assertEqual(provenance["target"], str(self.target))

    def test_foreign_provenance_is_never_adopted(self) -> None:
        foreign = self.target / "example-orient-repo"
        foreign.mkdir(parents=True)
        (foreign / self.provenance_file).write_text(
            json.dumps({"distribution": "someone-else"}), encoding="utf-8"
        )

        with self.assertRaisesRegex(DeploymentError, "foreign"):
            self.run_pipeline(adopt_legacy=True)

        self.assertEqual(
            json.loads((foreign / self.provenance_file).read_text())["distribution"],
            "someone-else",
        )

    def test_other_example_source_cannot_claim_installation(self) -> None:
        foreign = self.target / "example-orient-repo"
        foreign.mkdir(parents=True)
        (foreign / self.provenance_file).write_text(
            json.dumps({
                "schema": 1,
                "distribution": "example",
                "source": "/different/source",
                "host": "cursor",
            }),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(DeploymentError, "foreign"):
            self.run_pipeline(adopt_legacy=True)

        self.assertEqual(
            json.loads((foreign / self.provenance_file).read_text())["source"],
            "/different/source",
        )

    def test_mixed_legacy_and_foreign_root_is_never_adopted(self) -> None:
        self.add_source_skill("map-codebase")
        legacy = self.target / "example-orient-repo"
        legacy.mkdir(parents=True)
        foreign = self.target / "example-map-codebase"
        foreign.mkdir()
        (foreign / self.provenance_file).write_text(
            json.dumps({
                "schema": 1,
                "distribution": "example",
                "source": "/different/source",
                "host": "cursor",
            }),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(DeploymentError, "foreign"):
            self.run_pipeline(adopt_legacy=True)

        self.assertTrue(legacy.exists())
        self.assertTrue(foreign.exists())

    def test_source_symlink_is_rejected(self) -> None:
        external = self.root / "external.txt"
        external.write_text("outside", encoding="utf-8")
        link = self.source_path / "skills" / "orient-repo" / "linked.txt"
        link.symlink_to(external)

        with self.assertRaisesRegex(DeploymentError, "symlink"):
            self.run_pipeline()

        self.assertFalse(self.target.exists())

    def test_native_channel_enforces_dry_run_without_cli_global(self) -> None:
        host = NativeHost()
        self.assertFalse(core.DRY_RUN)

        NativeHostChannel("plugin").apply(
            host, self.source, DeployOptions(dry_run=True), lambda _message: None
        )

        self.assertTrue(host.saw_dry_run)
        self.assertFalse(core.DRY_RUN)

    def test_native_channel_surfaces_command_failure(self) -> None:
        with self.assertRaisesRegex(DeploymentError, "host install"):
            NativeHostChannel("plugin").apply(
                FailingNativeHost(), self.source, DeployOptions(), lambda _message: None
            )

    def test_injected_plugin_channel_must_pass_verification_before_record(self) -> None:
        host = PluginHost()
        profile = HostProfile(
            "claude", "plugin",
            (DiscoverySurface("plugin", None, authoritative=True),),
        )
        records = []
        pipeline = DeploymentPipeline(
            channels={"plugin": FakePluginChannel(install=False)},
            recorder=lambda *args: records.append(args),
        )

        with patch.dict(HOST_PROFILES, {"claude": profile}, clear=True):
            with self.assertRaisesRegex(DeploymentError, "verification failed"):
                pipeline.run(host, self.source, DeployOptions(), lambda _message: None)

        self.assertEqual(records, [])

    def test_injected_plugin_channel_records_after_verification(self) -> None:
        host = PluginHost()
        profile = HostProfile(
            "claude", "plugin",
            (DiscoverySurface("plugin", None, authoritative=True),),
        )
        records = []
        pipeline = DeploymentPipeline(
            channels={"plugin": FakePluginChannel(install=True)},
            recorder=lambda *args: records.append(args),
        )

        with patch.dict(HOST_PROFILES, {"claude": profile}, clear=True):
            pipeline.run(host, self.source, DeployOptions(), lambda _message: None)

        self.assertEqual(len(records), 1)

    def test_stale_managed_skill_is_pruned_but_unrelated_entry_survives(self) -> None:
        self.run_pipeline()
        stale = self.target / "example-removed-skill"
        stale.mkdir()
        (stale / self.provenance_file).write_text(
            json.dumps({
                "schema": 2,
                "distribution": "example",
                "source": str(self.source.path),
                "host": "cursor",
                "target": str(self.target),
            }),
            encoding="utf-8",
        )
        unrelated = self.target / "other-skill"
        unrelated.mkdir()

        self.run_pipeline()

        self.assertFalse(stale.exists())
        self.assertTrue(unrelated.exists())

    def test_managed_non_authoritative_copy_is_reconciled(self) -> None:
        additional_root = self.root / "home" / ".agents" / "skills"
        additional = additional_root / "example-orient-repo"
        additional.mkdir(parents=True)
        (additional / self.provenance_file).write_text(
            json.dumps({
                "schema": 2,
                "distribution": "example",
                "source": str(self.source.path),
                "host": "cursor",
                "target": str(additional_root),
            }),
            encoding="utf-8",
        )
        profile = HostProfile(
            "cursor", "host-directory",
            (
                DiscoverySurface("host-directory", self.target, authoritative=True),
                DiscoverySurface("shared-directory", additional_root),
            ),
        )

        with patch.dict(HOST_PROFILES, {"cursor": profile}, clear=True):
            DeploymentPipeline(recorder=lambda *_args: None).run(
                self.host, self.source, DeployOptions(), lambda _message: None
            )

        self.assertFalse(additional.exists())
        self.assertTrue((self.target / "example-orient-repo").exists())

    def test_removed_skill_is_pruned_from_non_authoritative_surface(self) -> None:
        additional_root = self.root / "home" / ".agents" / "skills"
        stale = additional_root / "example-removed-skill"
        stale.mkdir(parents=True)
        (stale / self.provenance_file).write_text(
            json.dumps({
                "schema": 2,
                "distribution": "example",
                "source": str(self.source.path),
                "host": "cursor",
                "target": str(additional_root),
            }),
            encoding="utf-8",
        )
        profile = HostProfile(
            "cursor",
            "host-directory",
            (
                DiscoverySurface("host-directory", self.target, authoritative=True),
                DiscoverySurface("shared-directory", additional_root),
            ),
        )

        with patch.dict(HOST_PROFILES, {"cursor": profile}, clear=True):
            DeploymentPipeline(recorder=lambda *_args: None).run(
                self.host, self.source, DeployOptions(), lambda _message: None
            )

        self.assertFalse(stale.exists())
        self.assertTrue((self.target / "example-orient-repo").exists())

    def test_stale_provenance_managed_bare_name_is_pruned_from_additional_surface(self) -> None:
        additional_root = self.root / "home" / ".agents" / "skills"
        stale = additional_root / "removed-skill"
        stale.mkdir(parents=True)
        (stale / self.provenance_file).write_text(
            json.dumps({
                "schema": 2,
                "distribution": "example",
                "source": str(self.source.path),
                "host": "cursor",
                "target": str(additional_root),
            }),
            encoding="utf-8",
        )
        profile = HostProfile(
            "cursor",
            "host-directory",
            (
                DiscoverySurface("host-directory", self.target, authoritative=True),
                DiscoverySurface("shared-directory", additional_root),
            ),
        )

        with patch.dict(HOST_PROFILES, {"cursor": profile}, clear=True):
            DeploymentPipeline(recorder=lambda *_args: None).run(
                self.host, self.source, DeployOptions(), lambda _message: None
            )

        self.assertFalse(stale.exists())

    def test_workspace_symlink_is_reconciled_from_non_authoritative_surface(self) -> None:
        additional_root = self.root / "home" / ".codex" / "skills"
        additional_root.mkdir(parents=True)
        workspace_link = additional_root / "orient-repo"
        workspace_link.symlink_to(
            self.source_path / "skills" / "orient-repo",
            target_is_directory=True,
        )
        profile = HostProfile(
            "cursor",
            "host-directory",
            (
                DiscoverySurface("host-directory", self.target, authoritative=True),
                DiscoverySurface("host-directory", additional_root),
            ),
        )

        with patch.dict(HOST_PROFILES, {"cursor": profile}, clear=True):
            DeploymentPipeline(recorder=lambda *_args: None).run(
                self.host, self.source, DeployOptions(), lambda _message: None
            )

        self.assertFalse(workspace_link.exists())
        self.assertFalse(workspace_link.is_symlink())

    def test_failed_atomic_swap_restores_previous_managed_install(self) -> None:
        self.run_pipeline()
        installed = self.target / "example-orient-repo"
        marker = installed / "previous.txt"
        marker.write_text("working", encoding="utf-8")
        original_rename = Path.rename

        def fail_staging_swap(path, target):
            if ".staging-" in path.name:
                raise OSError("injected swap failure")
            return original_rename(path, target)

        with patch.object(Path, "rename", fail_staging_swap):
            with self.assertRaisesRegex(
                DeploymentError,
                "directory deployment failed while installing orient-repo.*retry is safe",
            ):
                self.run_pipeline()

        self.assertEqual(marker.read_text(encoding="utf-8"), "working")

    def test_mid_set_filesystem_failure_reports_partial_progress_and_retry(self) -> None:
        self.add_source_skill("plan-testing")
        original_copytree = shutil.copytree

        def fail_second_copy(source, target, *args, **kwargs):
            if Path(source).name == "plan-testing":
                raise OSError("disk full")
            return original_copytree(source, target, *args, **kwargs)

        with patch("agent_skill_deployer.channels.shutil.copytree", side_effect=fail_second_copy):
            with self.assertRaisesRegex(
                DeploymentError,
                "installing plan-testing after 1/2 skills completed.*retry is safe",
            ):
                self.run_pipeline()

        self.assertTrue((self.target / "example-orient-repo").exists())
        self.assertFalse((self.target / "example-plan-testing").exists())

        self.run_pipeline()

        self.assertTrue((self.target / "example-orient-repo").exists())
        self.assertTrue((self.target / "example-plan-testing").exists())

    def test_record_happens_only_after_successful_verification(self) -> None:
        records = []
        self.pipeline_with_recorder(lambda *args: records.append(args))

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0][0], "cursor")


if __name__ == "__main__":
    unittest.main()
