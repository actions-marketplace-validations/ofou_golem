"""Check the hackathon's definition of done against what a repository's .golem recorded.

    python scripts/dod_check.py REPO

Each `golem run` is one process and one folder under REPO/.golem/runs. The checks read
those folders' events.jsonl and result.md, the registry's manifests and receipts, and the
usage journal. They say nothing about whether an answer is right; that needs ground truth.

`scripts/local_runner.py` entries with `"dod": true` run a multi-task sequence on one
checkout (build, build, manage, combine) and then invoke this script.

  1  a task exposed a missing capability: an installed tool whose gap quotes the task
     of the run that built it
  2  the agent created, tested and registered it, then answered that task
  3  the agent built tooling to discover or manage its capabilities: a registry-read
     tool it made and installed itself
  4  in a later run (a new process), a different task called at least two tools that
     earlier runs built, and made no make_tool call at all
  5  nothing was wired by hand: every run reports the same licence sha256, unchanged
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def load(repo: Path) -> tuple[list[dict], dict, list[dict]]:
    golem = repo / ".golem"
    runs = []
    for folder in (
        sorted((golem / "runs").iterdir()) if (golem / "runs").is_dir() else []
    ):
        events = _jsonl(folder / "events.jsonl")
        if not events:
            continue
        task = next((e["text"] for e in events if e["kind"] == "task"), "")
        answer = (
            (folder / "result.md").read_text(encoding="utf-8").strip()
            if (folder / "result.md").is_file()
            else ""
        )
        runs.append(
            {
                "id": folder.name,
                "task": task,
                "events": events,
                "answer": answer,
                "makes": [
                    e
                    for e in events
                    if e["kind"] == "create"
                    or (e["kind"] == "refused" and e["text"].startswith("make_tool"))
                ],
                "installs": [
                    e["text"].split(" ", 1)[0] for e in events if e["kind"] == "install"
                ],
                "licence": sorted(
                    {
                        e["text"].split()[-1]
                        if e["text"].startswith("shem")
                        else e["text"].split()[1]
                        for e in events
                        if e["kind"] == "licence"
                    }
                ),
            }
        )
    tools = {}
    registry = golem / "registry"
    active = (
        json.loads((registry / "active.json").read_text())["tools"]
        if (registry / "active.json").is_file()
        else {}
    )
    for name, version in active.items():
        folder = registry / name / version
        tools[f"{name}@{version}"] = {
            "manifest": json.loads((folder / "manifest.json").read_text()),
            "receipt": json.loads((folder / "receipt.json").read_text()),
        }
    return runs, tools, _jsonl(golem / "usage.jsonl")


def check(repo: Path) -> int:
    runs, tools, usage = load(repo)
    by_id = {run["id"]: run for run in runs}
    built_in = {}
    for run in runs:
        for tool in run["installs"]:
            built_in[tool] = run["id"]
    results = []

    def report(number: int, ok: bool, title: str, lines: list[str]) -> None:
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {number}. {title}")
        for line in lines:
            print(f"        {line}")

    from_task, completed = [], []
    for tool, data in sorted(tools.items()):
        manifest, receipt = data["manifest"], data["receipt"]
        run = by_id.get(manifest.get("created_by", {}).get("run", ""))
        quote = manifest.get("gap", {}).get("task_quote", "")
        if run and _norm(quote) and _norm(quote) in _norm(run["task"]):
            from_task.append(
                f'{tool}: gap "{quote[:90]}" is in the task of run {run["id"]}'
            )
            tests_ok = (
                receipt["tests"]["ok"] == receipt["tests"]["ran"] >= 3
                and receipt["blind_tests"]["ok"] == receipt["blind_tests"]["ran"] >= 3
            )
            verdicts = [
                e["text"]
                for e in run["events"]
                if e["kind"] == "verdict" and e["text"].startswith(tool.split("@")[0])
            ]
            answered = bool(run["answer"]) and not run["answer"].startswith(
                "The session ended"
            )
            line = (
                f"{tool}: own {receipt['tests']['ok']}/{receipt['tests']['ran']}, blind {receipt['blind_tests']['ok']}/{receipt['blind_tests']['ran']}, "
                f"stubs fail {int(receipt['stub_failed'] * 100)}%, verdicts {[v.split(': ', 1)[1][:4] for v in verdicts]}, "
                f"installed, answer {'given' if answered else 'MISSING'} ({len(run['answer'])} chars)"
            )
            if (
                tests_ok
                and receipt["stub_failed"] >= 0.8
                and receipt["passed"]
                and answered
            ):
                completed.append(line)
            else:
                completed.append("not complete: " + line)
    report(
        1,
        bool(from_task),
        "a task exposed a missing capability",
        from_task
        or ["no installed tool's gap quotes the task of the run that built it"],
    )
    report(
        2,
        any(not line.startswith("not complete") for line in completed),
        "created, tested, registered, then the task answered",
        completed or ["nothing installed"],
    )

    managers = [
        f"{tool}: {data['manifest']['description'][:110]}"
        for tool, data in tools.items()
        if data["manifest"].get("access") == "registry-read" and tool in built_in
    ]
    report(
        3,
        bool(managers),
        "the agent built tooling to discover or manage its capabilities",
        managers or ["no registry-read tool built and installed by a run"],
    )

    combined = []
    for run in runs:
        called = sorted(
            {row["tool"] for row in usage if row["run"] == run["id"] and row.get("ok")}
        )
        earlier = [
            tool for tool in called if tool in built_in and built_in[tool] < run["id"]
        ]
        earlier_tasks = {_norm(r["task"]) for r in runs if r["id"] < run["id"]}
        if len(earlier) >= 2:
            combined.append(
                {
                    "ok": not run["makes"] and _norm(run["task"]) not in earlier_tasks,
                    "line": f"run {run['id']}: called {earlier} (built in runs {sorted({built_in[t] for t in earlier})}); "
                    f"make_tool calls {len(run['makes'])}; different task: {_norm(run['task']) not in earlier_tasks}",
                }
            )
    report(
        4,
        any(item["ok"] for item in combined),
        "a later run combined earlier tools without rebuilding",
        [item["line"] for item in combined]
        or ["no run called two or more tools built by earlier runs"],
    )

    hashes = {h for run in runs for h in run["licence"]}
    changed = [
        run["id"]
        for run in runs
        if not any(
            e["kind"] == "licence" and e["text"].endswith("unchanged")
            for e in run["events"]
        )
    ]
    report(
        5,
        len(hashes) == 1 and not changed,
        "no hand wiring: one licence, unchanged in every run",
        [
            f"{len(runs)} runs, licence sha256 {sorted(hashes)}",
            f"runs without an 'unchanged' line: {changed or 'none'}",
        ],
    )

    print(
        f"\n{sum(results)}/{len(results)} checks pass. Runs: "
        + ", ".join(f"{r['id']} ({len(r['makes'])} make_tool)" for r in runs)
    )
    return 0 if all(results) else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    raise SystemExit(check(Path(sys.argv[1]).resolve()))
