from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from agent_skill_deployer import core
from agent_skill_deployer.core import (
    DeploymentError,
    DistributionPolicy,
    GitRelease,
    RunResult,
    Source,
)


class CoreBoundaryTests(unittest.TestCase):
    def test_source_load_consumes_vendor_neutral_distribution_manifest(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "example-distribution"

        source = Source.load(fixture)

        self.assertEqual(source.distribution.identity, "example-distribution")
        self.assertEqual(source.display_name, "Example Distribution")
        self.assertEqual(source.skills_path, fixture / "skills")

    def test_distribution_manifest_rejects_nested_skills_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / "distribution.json"
            manifest.write_text(
                '{"schema": 1, "name": "example", "skills_directory": "nested/skills"}',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(DeploymentError, "one relative directory"):
                DistributionPolicy.from_distribution_manifest(manifest)

    def test_missing_executable_is_reported_as_deployment_error(self) -> None:
        with (
            patch("agent_skill_deployer.core.subprocess.run", side_effect=FileNotFoundError),
            self.assertRaisesRegex(DeploymentError, "required tool not found: missing-tool"),
        ):
            core.run(["missing-tool", "--version"])

    def test_corrupt_config_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.json"
            config.write_text("{broken", encoding="utf-8")

            with (
                patch.object(core, "CONFIG_PATH", config),
                self.assertRaisesRegex(DeploymentError, "invalid JSON"),
            ):
                core.load_config()

    def test_corrupt_state_warns_and_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state.json"
            state.write_text("{broken", encoding="utf-8")
            stderr = StringIO()

            with patch.object(core, "STATE_PATH", state), redirect_stderr(stderr):
                loaded = core.load_state()

        self.assertEqual(loaded, {})
        self.assertIn("ignoring corrupt deployment state", stderr.getvalue())

    def test_git_release_requires_tag_and_remote_head_to_match(self) -> None:
        release = GitRelease(
            "https://example.com/team/skills.git",
            "v0.1.0",
            DistributionPolicy.named("example"),
        )
        output = (
            "a" * 40 + "\tHEAD\n"
            + "b" * 40 + "\trefs/tags/v0.1.0\n"
        )

        with (
            patch.object(core, "run", return_value=RunResult([], 0, output, "")),
            self.assertRaisesRegex(DeploymentError, "HEAD=.*tag="),
        ):
            release._resolve_remote()

    def test_git_release_resolves_peeled_annotated_tag(self) -> None:
        release = GitRelease(
            "https://example.com/team/skills.git",
            "v0.1.0",
            DistributionPolicy.named("example"),
        )
        commit = "a" * 40
        output = (
            f"{commit}\tHEAD\n"
            + "b" * 40 + "\trefs/tags/v0.1.0\n"
            + f"{commit}\trefs/tags/v0.1.0^{{}}\n"
        )

        with patch.object(core, "run", return_value=RunResult([], 0, output, "")):
            self.assertEqual(release._resolve_remote(), commit)

    def test_git_release_rejects_invalid_version_contract(self) -> None:
        release = GitRelease(
            "https://example.com/team/skills.git",
            "v0.1.0",
            DistributionPolicy.named("example"),
            expected_version="0.1.0",
        )

        with self.assertRaisesRegex(DeploymentError, "requires both"):
            release._verify_version(
                core.Source(Path("/tmp/example"), "example", "example")
            )


if __name__ == "__main__":
    unittest.main()
