from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_skill_deployer import cli
from agent_skill_deployer.core import DeploymentError, Source
from agent_skill_deployer.inventory import DiscoverySurface, HostProfile


class FakeHost:
    def __init__(self, key):
        self.key = key
        self.label = key.title()

    def available(self):
        return True


class CliPolicyTests(unittest.TestCase):
    def test_fixed_source_cli_hides_config_and_local_source_options(self) -> None:
        provider = lambda: Source(Path("/release"), "example", "example")
        parser = cli.build_parser(source_provider=provider)

        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["config"])
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["deploy", "--source", "/workspace"])

    def test_fixed_source_provider_wins_without_workspace_discovery(self) -> None:
        expected = Source(Path("/release"), "example", "example")

        self.assertIs(cli.resolve_source(None, source_provider=lambda: expected), expected)

    def test_shared_writer_is_rejected_when_another_host_can_see_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            shared = Path(temp) / ".agents" / "skills"
            profiles = {
                "cline": HostProfile(
                    "cline", "shared-directory",
                    (DiscoverySurface("shared-directory", shared, authoritative=True),),
                ),
                "codex": HostProfile(
                    "codex", "plugin",
                    (
                        DiscoverySurface("plugin", None, authoritative=True),
                        DiscoverySurface("shared-directory", shared),
                    ),
                ),
            }
            with patch.dict(cli.HOST_PROFILES, profiles, clear=True):
                errors = cli._channel_isolation_errors(
                    [FakeHost("cline")], [FakeHost("cline"), FakeHost("codex")]
                )

        self.assertIn("additional source visible to codex", errors["cline"])

    def test_private_writer_has_no_isolation_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            private = Path(temp) / ".cursor" / "skills"
            profiles = {
                "cursor": HostProfile(
                    "cursor", "host-directory",
                    (DiscoverySurface("host-directory", private, authoritative=True),),
                ),
                "codex": HostProfile(
                    "codex", "plugin",
                    (DiscoverySurface("plugin", None, authoritative=True),),
                ),
            }
            with patch.dict(cli.HOST_PROFILES, profiles, clear=True):
                errors = cli._channel_isolation_errors(
                    [FakeHost("cursor")], [FakeHost("cursor"), FakeHost("codex")]
                )

        self.assertEqual(errors, {})

    def test_cleanup_cannot_remove_another_hosts_authoritative_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            shared = Path(temp) / ".agents" / "skills"
            profiles = {
                "codex": HostProfile(
                    "codex", "plugin",
                    (
                        DiscoverySurface("plugin", None, authoritative=True),
                        DiscoverySurface("shared-directory", shared),
                    ),
                ),
                "cline": HostProfile(
                    "cline", "shared-directory",
                    (DiscoverySurface("shared-directory", shared, authoritative=True),),
                ),
            }
            with patch.dict(cli.HOST_PROFILES, profiles, clear=True):
                errors = cli._channel_isolation_errors(
                    [FakeHost("codex")], [FakeHost("codex"), FakeHost("cline")]
                )

        self.assertIn("authoritative source for cline", errors["codex"])

    def test_all_skips_conflicting_hosts_and_deploys_unrelated_host(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "skills").mkdir()
            source = Source(root, "example", "example")
            shared = root / ".agents" / "skills"
            hosts = [FakeHost("claude"), FakeHost("codex"), FakeHost("cline")]
            profiles = {
                "claude": HostProfile(
                    "claude", "plugin",
                    (DiscoverySurface("plugin", None, authoritative=True),),
                ),
                "codex": HostProfile(
                    "codex", "plugin",
                    (
                        DiscoverySurface("plugin", None, authoritative=True),
                        DiscoverySurface("shared-directory", shared),
                    ),
                ),
                "cline": HostProfile(
                    "cline", "shared-directory",
                    (DiscoverySurface("shared-directory", shared, authoritative=True),),
                ),
            }
            args = SimpleNamespace(
                source=None,
                all=True,
                host=None,
                skip_validate=True,
                dry_run=True,
                adopt_legacy=False,
            )
            deployed = []
            with (
                patch.dict(cli.HOST_PROFILES, profiles, clear=True),
                patch.object(cli, "resolve_source", return_value=source),
                patch.object(source, "require_formal_checkout"),
                patch.object(cli, "get_hosts", return_value=hosts),
                patch.object(
                    cli, "_deploy_one",
                    side_effect=lambda host, _source, _args: deployed.append(host.key) or True,
                ),
                patch("builtins.print"),
            ):
                code = cli.cmd_deploy(args)

        self.assertEqual(deployed, ["claude"])
        self.assertEqual(code, 1)

    def test_deploy_uses_git_origin_for_formal_marketplace(self) -> None:
        source = Source(Path("/checkout/example"), "example", "example", "https://example/example.git")
        args = SimpleNamespace(
            source=None, all=True, host=None, skip_validate=True, dry_run=True,
            adopt_legacy=False,
        )
        host = FakeHost("claude")
        observed = []
        with (
            patch.object(cli, "resolve_source", return_value=source),
            patch.object(source, "require_formal_checkout"),
            patch.object(cli, "get_hosts", return_value=[host]),
            patch.object(
                cli, "_deploy_one",
                side_effect=lambda _host, selected, _args: observed.append(
                    selected.formal_source
                ) or True,
            ),
            patch("builtins.print"),
        ):
            self.assertEqual(cli.cmd_deploy(args), 0)

        self.assertEqual(observed, ["https://example/example.git"])

    def test_deploy_fails_closed_without_git_origin(self) -> None:
        source = Source(Path("/checkout/example"), "example", "example")
        args = SimpleNamespace(
            source=None, all=True, host=None, skip_validate=True, dry_run=True,
            adopt_legacy=False,
        )
        with (
            patch.object(cli, "resolve_source", return_value=source),
            self.assertRaisesRegex(DeploymentError, "requires a remote Git origin"),
        ):
            cli.cmd_deploy(args)

    def test_formal_source_fails_closed_without_git_origin(self) -> None:
        source = Source(Path("/checkout/example"), "example", "example")

        with self.assertRaisesRegex(DeploymentError, "requires a remote Git origin"):
            _ = source.formal_source

    def test_formal_source_rejects_local_git_origin(self) -> None:
        for origin in ("/checkout/example", "../example", "file:///checkout/example"):
            with self.subTest(origin=origin):
                source = Source(Path("/checkout/example"), "example", "example", origin)
                with self.assertRaisesRegex(DeploymentError, "development sources"):
                    _ = source.formal_source

    def test_formal_source_accepts_ssh_git_origin(self) -> None:
        source = Source(
            Path("/checkout/example"), "example", "example", "git@example.com:team/example.git"
        )

        self.assertEqual(source.formal_source, "git@example.com:team/example.git")

    def test_formal_checkout_rejects_dirty_working_tree(self) -> None:
        source = Source(
            Path("/checkout/example"), "example", "example", "https://example/example.git"
        )

        with (
            patch.object(source, "head", return_value="a" * 40),
            patch.object(source, "is_dirty", return_value=True),
            self.assertRaisesRegex(DeploymentError, "clean working tree"),
        ):
            source.require_formal_checkout()

    def test_deploy_parser_has_no_local_marketplace_escape_hatch(self) -> None:
        parser = cli.build_parser()

        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["deploy", "--local-marketplace"])


if __name__ == "__main__":
    unittest.main()
