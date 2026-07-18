"""CLI: detect hosts, show drift, and deploy an Agent Skills distribution."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

from . import __version__
from .core import (
    DistributionPolicy,
    DeploymentError,
    Source,
    load_config,
    save_config,
    short,
)
from .hosts import Host, get_hosts
from .inventory import HOST_PROFILES, detect_conflicts, inventory_host
from .channels import DeployOptions
from .pipeline import DeploymentPipeline

SourceProvider = Callable[[], Source]

# --- tiny terminal helpers ---------------------------------------------------

_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def dim(t: str) -> str:
    return _c("2", t)


def bold(t: str) -> str:
    return _c("1", t)


def green(t: str) -> str:
    return _c("32", t)


def yellow(t: str) -> str:
    return _c("33", t)


def red(t: str) -> str:
    return _c("31", t)


# --- source resolution -------------------------------------------------------


def resolve_source(
    arg: str | None,
    policy: DistributionPolicy | None = None,
    source_provider: SourceProvider | None = None,
) -> Source:
    if source_provider is not None:
        if arg is not None:
            raise DeploymentError("a fixed formal source does not accept a local --source")
        return source_provider()
    if arg:
        return Source.load(Path(arg), policy)
    cfg = load_config()
    if cfg.get("source"):
        return Source.load(Path(cfg["source"]), policy)
    cwd = Path.cwd()
    skills_directory = policy.skills_directory if policy else "skills"
    if (cwd / skills_directory).is_dir():
        return Source.load(cwd, policy)
    raise DeploymentError(
        "no source given. Pass --source PATH, set a default with "
        "`skill-deploy config --source PATH`, or run inside a skills repo."
    )


# --- commands ----------------------------------------------------------------


def cmd_hosts(_args: argparse.Namespace) -> int:
    print(bold("Hosts"))
    for host in get_hosts():
        mark = green("available") if host.available() else dim("not installed")
        profile = HOST_PROFILES[host.key]
        print(
            f"  {host.key:<12} {host.label:<16} {mark:<20} "
            f"channel={profile.authoritative_channel} support={profile.support}"
        )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    source = resolve_source(
        getattr(args, "source", None),
        getattr(args, "distribution_policy", None),
        getattr(args, "source_provider", None),
    )
    head = source.head()
    print(f"{bold(source.marketplace)}  {dim(source.display_location)}")
    print(f"  source HEAD: {bold(short(head))}\n")
    hosts = get_hosts(args.host)
    for host in hosts:
        if not host.available():
            print(f"  {host.key:<12} {dim('host not installed')}")
            continue
        installations = inventory_host(host, source)
        authoritative = next((item for item in installations if item.authoritative), None)
        installed = authoritative.commit if authoritative else None
        if authoritative is None:
            state = yellow("not deployed / unknown")
        elif head and installed == head:
            state = green(f"up to date ({short(installed)})")
        elif installed is None:
            state = yellow("installed (commit unverifiable)")
        else:
            state = yellow(f"stale ({short(installed)} → {short(head)})")
        print(f"  {host.key:<12} {state}")
        for item in installations:
            authority = "authoritative" if item.authoritative else "additional"
            commit = f" commit={short(item.commit)}" if item.commit else ""
            print(
                f"    {item.channel:<18} {authority:<13} {item.location} "
                f"ownership={item.ownership}{commit}"
            )
        for conflict in detect_conflicts(host.key, installations):
            print(f"    {red(conflict.kind)}: {len(conflict.installations)} visible sources")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report source conflicts without changing any host state."""
    source = resolve_source(
        getattr(args, "source", None),
        getattr(args, "distribution_policy", None),
        getattr(args, "source_provider", None),
    )
    hosts = get_hosts(args.host)
    conflicts = 0
    print(
        f"{bold(source.display_name + ' deployment doctor')}  "
        f"{dim(source.display_location)}"
    )
    for host in hosts:
        if not host.available():
            print(f"  {host.key:<12} {dim('host not installed; skipped')}")
            continue
        installations = inventory_host(host, source)
        found = detect_conflicts(host.key, installations)
        if not found:
            print(f"  {host.key:<12} {green('ok')} ({len(installations)} visible source(s))")
            continue
        conflicts += len(found)
        for conflict in found:
            print(f"  {host.key:<12} {red(conflict.kind)}")
            for item in conflict.installations:
                authority = "authoritative" if item.authoritative else "additional"
                print(f"    {item.channel:<18} {authority:<13} {item.location}")
    if conflicts:
        print(yellow(f"\nFound {conflicts} conflict(s); no changes were made."))
        return 1
    print(
        green(
            f"\nNo duplicate {source.display_name} sources detected; "
            "no changes were made."
        )
    )
    return 0


def _deploy_one(host: Host, source: Source, args: argparse.Namespace) -> bool:
    print(f"\n{bold('→ ' + host.label)}")
    before = short(host.installed_commit(source))

    def log(msg: str) -> None:
        print(f"    {dim(msg)}")

    try:
        result = DeploymentPipeline().run(
            host,
            source,
            DeployOptions(dry_run=args.dry_run, adopt_legacy=args.adopt_legacy),
            log,
        )
    except DeploymentError as exc:
        print(f"    {red('failed:')} {exc}")
        return False
    head = source.head()
    if args.dry_run:
        print(f"    {yellow('planned')}  no state changed  stages={','.join(result.stages)}")
        return True
    after = short(host.installed_commit(source) or head)
    print(f"    {green('ok')}  {before} → {after}  stages={','.join(result.stages)}")
    return True


def _channel_isolation_errors(
    selected_hosts: list[Host], visible_hosts: list[Host]
) -> dict[str, str]:
    """Return per-host conflicts without blocking unrelated deployment targets."""
    errors: dict[str, str] = {}
    for selected in selected_hosts:
        selected_profile = HOST_PROFILES[selected.key]
        writers = {
            surface.path for surface in selected_profile.surfaces
            if surface.authoritative and surface.path is not None
        }
        cleanup_surfaces = {
            surface.path for surface in selected_profile.surfaces
            if not surface.authoritative and surface.path is not None
        }
        for other in visible_hosts:
            if other.key == selected.key:
                continue
            overlap = next((
                surface.path for surface in HOST_PROFILES[other.key].surfaces
                if surface.path in writers and not surface.authoritative
            ), None)
            if overlap is not None:
                errors[selected.key] = (
                    f"channel isolation failed: deploying {selected.key} into {overlap} "
                    f"would create an additional source visible to {other.key}"
                )
                break
            cleanup_overlap = next((
                surface.path for surface in HOST_PROFILES[other.key].surfaces
                if surface.path in cleanup_surfaces and surface.authoritative
            ), None)
            if cleanup_overlap is not None:
                errors[selected.key] = (
                    f"channel isolation failed: reconciling {selected.key} would remove "
                    f"{cleanup_overlap}, the authoritative source for {other.key}"
                )
                break
    return errors


def cmd_deploy(args: argparse.Namespace) -> int:
    source = resolve_source(
        getattr(args, "source", None),
        getattr(args, "distribution_policy", None),
        getattr(args, "source_provider", None),
    )
    source.require_formal_checkout()
    hosts = get_hosts(None if args.all else args.host)

    available = [h for h in hosts if h.available()]
    skipped = [h for h in hosts if not h.available()]
    if not available:
        raise DeploymentError("none of the selected hosts are installed on this machine.")

    all_available = [host for host in get_hosts() if host.available()]
    isolation_errors = _channel_isolation_errors(available, all_available)
    deployable = [host for host in available if host.key not in isolation_errors]
    blocked = [host for host in available if host.key in isolation_errors]
    if not deployable:
        for host in blocked:
            print(f"  {red(host.key + ':')} {isolation_errors[host.key]}")
        return 1

    if not args.skip_validate:
        result = source.validate()
        if result is None:
            print(dim("no validator in source repo; skipping validation"))
        elif not result.ok:
            print(red("validation failed — aborting (use --skip-validate to override):"))
            print(result.out or result.err)
            return 1
        else:
            print(green("validation passed"))

    if args.dry_run:
        print(yellow("\n[dry-run] mutating commands will be reported, not executed"))

    print(f"\nDeploying {bold(source.selector)} from {dim(source.display_location)}")
    for host in skipped:
        print(f"  {dim(host.key + ': host not installed, skipping')}")
    for host in blocked:
        print(f"  {red(host.key + ': skipped')} — {isolation_errors[host.key]}")

    ok = 0
    for host in deployable:
        if _deploy_one(host, source, args):
            ok += 1

    print(
        f"\n{bold('Done')}: {ok}/{len(available)} host(s) deployed"
        f"; {len(blocked)} blocked by channel isolation."
    )
    return 0 if ok == len(available) else 1


def cmd_config(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.source:
        source = Source.load(
            Path(args.source), getattr(args, "distribution_policy", None)
        )  # validate it exists
        cfg["source"] = str(source.path)
        save_config(cfg)
        print(f"default source set to {bold(str(source.path))}")
        return 0
    if not cfg:
        print(dim("no config set. Use `skill-deploy config --source PATH`."))
        return 0
    for key, value in cfg.items():
        print(f"  {key} = {value}")
    return 0


# --- parser ------------------------------------------------------------------


def build_parser(
    *,
    program: str = "skill-deploy",
    version: str = __version__,
    distribution_policy: DistributionPolicy | None = None,
    source_provider: SourceProvider | None = None,
) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=program,
        description="Deploy a portable skills repo across every local agent host.",
    )
    p.add_argument("--version", action="version", version=f"{program} {version}")
    p.set_defaults(distribution_policy=distribution_policy)
    p.set_defaults(source_provider=source_provider)
    sub = p.add_subparsers(dest="command", required=True)

    ph = sub.add_parser("hosts", help="list known hosts and whether they're installed")
    ph.set_defaults(func=cmd_hosts)

    ps = sub.add_parser("status", help="show source HEAD vs each host's installed commit")
    if source_provider is None:
        ps.add_argument("--source", help="path to the skills repo (default: config or cwd)")
    ps.add_argument("--host", action="append", help="limit to a host (repeatable)")
    ps.set_defaults(func=cmd_status)

    pdoc = sub.add_parser("doctor", help="read-only check for deployment-source conflicts")
    if source_provider is None:
        pdoc.add_argument("--source", help="path to the skills repo (default: config or cwd)")
    pdoc.add_argument("--host", action="append", help="limit to a host (repeatable)")
    pdoc.set_defaults(func=cmd_doctor)

    pd = sub.add_parser("deploy", help="validate, then (re)deploy to hosts")
    if source_provider is None:
        pd.add_argument("--source", help="path to the skills repo (default: config or cwd)")
    pd.add_argument("--host", action="append", help="limit to a host (repeatable)")
    pd.add_argument("--all", action="store_true", help="all installed hosts (the default)")
    pd.add_argument("--skip-validate", action="store_true", help="skip the source validator")
    pd.add_argument("--dry-run", action="store_true", help="report commands without running them")
    pd.add_argument(
        "--adopt-legacy", action="store_true",
        help="explicitly replace exact attributable legacy paths after inspection",
    )
    pd.set_defaults(func=cmd_deploy)

    if source_provider is None:
        pc = sub.add_parser("config", help="show or set the default source repo")
        pc.add_argument("--source", help="set the default source repo path")
        pc.set_defaults(func=cmd_config)

    return p


def main(
    argv: list[str] | None = None,
    *,
    program: str = "skill-deploy",
    version: str = __version__,
    distribution_policy: DistributionPolicy | None = None,
    source_provider: SourceProvider | None = None,
) -> int:
    parser = build_parser(
        program=program,
        version=version,
        distribution_policy=distribution_policy,
        source_provider=source_provider,
    )
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except DeploymentError as exc:
        print(red(f"error: {exc}"), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
