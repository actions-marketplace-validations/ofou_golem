"""golem run | registry | rollback | licence"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from golem import licence as licence_mod
from golem.registry import Registry
from golem.sandbox import Sandbox

DEFAULT_LICENCE = Path(__file__).resolve().parent.parent / "authority.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="golem", description="An agent that builds, tests, and keeps its own tools.")
    parser.add_argument("--repo", default=".", help="repository Golem works in (default: current directory)")
    parser.add_argument("--licence", default=None, help="licence file (default: <repo>/.golem/authority.json, else Golem's)")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="do one task")
    run_p.add_argument("task")
    run_p.add_argument("--attach", action="append", default=[], help="file to give the task (repeatable)")

    sub.add_parser("registry", help="list installed tools, versions, and usage")
    roll = sub.add_parser("rollback", help="point a tool back to an earlier version")
    roll.add_argument("name")
    roll.add_argument("version")
    sub.add_parser("licence", help="print the licence and its sha256")

    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    licence_path = Path(args.licence) if args.licence else (repo / ".golem" / "authority.json")
    if not licence_path.is_file():
        licence_path = DEFAULT_LICENCE
    registry = Registry(repo / ".golem")

    if args.command == "licence":
        lic = licence_mod.load(licence_path)
        print(lic.raw.decode("utf-8").rstrip())
        print(f"sha256 {lic.sha256}  ({licence_path})")
        return 0
    if args.command == "registry":
        return _print_registry(registry)
    if args.command == "rollback":
        print(registry.rollback(args.name, args.version))
        return 0

    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        print("OPENROUTER_API_KEY is not set", file=sys.stderr)
        return 2
    if not Sandbox.available():
        print("Docker is not running; Golem will not run generated code without its sandbox", file=sys.stderr)
        return 2
    from golem.agent import build_run, run_task

    lic = licence_mod.load(licence_path)
    run = build_run(repo, args.task, lic, key, [Path(item) for item in args.attach])
    answer = asyncio.run(run_task(run))
    print("\n" + "=" * 72 + "\n" + answer)
    return 0


def _print_registry(registry: Registry) -> int:
    active = registry.active()
    usage = registry.journal("usage")
    if not active:
        print("registry: empty")
        return 0
    for name, version in sorted(active.items()):
        manifest = registry.manifest(name, version)
        receipt = registry.receipt(name, version)
        calls = [row for row in usage if row["tool"].startswith(f"{name}@")]
        print(f"{name}@{version}  [{manifest['access']}]  versions: {', '.join(registry.versions(name))}")
        print(f"    {manifest['description']}")
        print(f"    tests {receipt['tests']['ok']}/{receipt['tests']['ran']}, blind {receipt['blind_tests']['ok']}/{receipt['blind_tests']['ran']}, "
              f"stub failed {int(receipt['stub_failed'] * 100)}% | calls {len(calls)}, errors {sum(1 for row in calls if not row['ok'])}")
        print(f"    gap: {json.dumps(manifest['gap'].get('task_quote', ''))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
