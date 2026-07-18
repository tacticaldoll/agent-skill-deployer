from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_skill_deployer.core import RunResult, Source
from agent_skill_deployer.hosts import Gemini, _rewrite_name


class HostSourcePolicyTests(unittest.TestCase):
    def test_gemini_installs_extension_from_formal_git_origin(self) -> None:
        source = Source(
            Path("/checkout/example"),
            "example",
            "example",
            "https://example/example.git",
        )
        commands = []

        def fake_run(command, **_kwargs):
            commands.append(command)
            return RunResult(command, 0, "", "")

        with patch("agent_skill_deployer.hosts.run", side_effect=fake_run):
            Gemini().deploy(source, lambda _message: None)

        self.assertEqual(
            commands[-1],
            [
                "gemini", "extensions", "install", "https://example/example.git",
                "--consent", "--skip-settings",
            ],
        )

    def test_rewrite_name_treats_backslashes_as_literal_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / "SKILL.md"
            manifest.write_text("---\nname: old-name\n---\n", encoding="utf-8")

            _rewrite_name(manifest, r"example-name\g<1>")

            self.assertIn(r"name: example-name\g<1>", manifest.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
