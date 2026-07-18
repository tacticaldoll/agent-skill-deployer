# agent-skill-deployer

Deploy a portable Agent Skills distribution across local agent hosts through one
inventory, ownership, and reconciliation engine.

The engine handles the host-specific mechanics that a skills registry should not
have to own: discovery surfaces, native plugin or extension refreshes, staged
directory copies, provenance, conflict detection, dry runs, verification, and safe
retry after partial filesystem failure.

Supported host profiles currently include Claude Code, Codex, Antigravity, Gemini
CLI, GitHub Copilot CLI, Cline, Cursor, and OpenCode. Claude and Codex are stable;
other profiles remain beta or experimental as marked by `skill-deploy hosts`.

## Install

```sh
pipx install .
```

The project uses only the Python standard library at runtime and requires Python
3.10 or newer.

## Use

```sh
skill-deploy config --source /path/to/skills-distribution
skill-deploy hosts
skill-deploy status
skill-deploy doctor
skill-deploy deploy --dry-run
skill-deploy deploy --all
```

Formal deployment requires a clean Git checkout with a remote `origin`. Native
plugin and extension hosts install from that Git origin. Directory-discovery hosts
receive staged copies from the committed checkout; they never receive symlinks back
to a development workspace.

When a source contains the vendor-neutral `distribution.json` contract, the engine reads its
identity, display name, and skills directory. Otherwise identity defaults to the source directory
name and can be overridden through `DistributionPolicy`. For a distribution named `example`,
directory hosts receive `example-<skill>` and ownership is recorded in `.example-install.json`.

## Library API

The supported integration surface is deliberately small:

```python
from agent_skill_deployer.cli import main
from agent_skill_deployer.core import DistributionPolicy, Source
from agent_skill_deployer.pipeline import DeploymentPipeline
```

Product CLIs can call `main(program="product-name", version="workspace-version")`.
Distribution-specific naming and layout can be passed through `DistributionPolicy`;
the engine has no dependency on a particular skills registry.

For a formal product CLI, bind a tagged remote release instead of exposing a local
source path:

```python
from agent_skill_deployer.cli import main
from agent_skill_deployer.core import DistributionPolicy, GitRelease

version = "0.1.0"
policy = DistributionPolicy.named("example", display_name="Example")
release = GitRelease(
    "https://github.com/example/skills",
    f"v{version}",
    policy,
    expected_version=version,
    version_manifest="distribution.json",
)
main(
    program="example",
    version=version,
    distribution_policy=policy,
    source_provider=release.materialize,
)
```

A fixed source provider removes `config` and every `--source` option from the
product CLI. Before checkout, `GitRelease` requires the release tag and remote
default HEAD to identify the same commit. It then materializes a detached managed
snapshot, verifies the declared manifest version, and reuses only a clean cache at
that exact commit. This keeps native plugin hosts and directory-copy hosts on one
source revision.

## Safety model

- Inventory every declared discovery surface before mutation.
- Assign one authoritative deployment channel per host.
- Replace or prune only entries with matching provenance.
- Treat unowned legacy paths as blocked unless `--adopt-legacy` is explicit.
- Never adopt entries carrying foreign provenance.
- Stage and atomically swap each directory-installed skill.
- Report partial progress cleanly when unexpected filesystem I/O interrupts a set.
- Verify the authoritative channel before recording success.

## Development

```sh
python -m unittest discover -s tests -v
python -m agent_skill_deployer --help
```

Required unit tests use only local neutral fixtures. The canonical reusable starting point is
[`agent-skills-distribution-template`](https://github.com/tacticaldoll/agent-skills-distribution-template),
whose CI consumes the released engine for cross-repository compatibility testing.

## License

MIT — see [LICENSE](LICENSE).
