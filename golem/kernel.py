"""The kernel: the only code the model can reach, and none of it is domain logic.

The model starts with four kernel tools:
    list_files, read_file    read the repository snapshot and the task's attachments
    make_tool                create a tool and prove it in the sandbox
    install_tool             install the exact bytes that passed

Every installed tool is loaded as one more tool, executed in the sandbox.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from openrouter_agent import tool

from golem import jev, lint, schema, snapshot, tester
from golem.licence import Licence, violations
from golem.registry import NAME, Registry
from golem.sandbox import Sandbox, TestReport

RESERVED = {"list_files", "read_file", "make_tool", "install_tool"}
MIN_TESTS = 3
MIN_STUB_FAIL_RATIO = 0.8
TESTER_BUDGET_USD = 0.30
TESTER_TRIES = 2
MAX_DROPS_PER_SUITE = 2
DISPUTE_BUDGET_USD = 0.10
MIN_USD_TO_MAKE = 0.03  # refuse to start a build that could not pay for its blind tests


@dataclass
class Candidate:
    id: str
    manifest: dict
    folder: Path
    receipt: dict


@dataclass
class Run:
    repo: Path
    task: str
    licence: Licence
    registry: Registry
    sandbox: Sandbox
    snapshot_dir: Path
    files: list[str]
    api_key: str
    client: object
    hooks: object
    run_dir: Path
    run_id: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    spent: float = 0.0
    made: int = 0
    attempts: dict = field(default_factory=dict)
    candidates: dict = field(default_factory=dict)
    jev_asked: set = field(default_factory=set)
    blind_suites: dict = field(default_factory=dict)
    dropped: dict = field(default_factory=dict)
    installed_now: list = field(default_factory=list)
    used: list = field(default_factory=list)
    models_by_role: dict = field(default_factory=dict)
    tester_hooks: object = None

    def say(self, kind: str, text: str, **data) -> None:
        print(f"[{kind}] {text}", flush=True)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with open(self.run_dir / "events.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"ts": round(time.time(), 3), "kind": kind, "text": text, **data}, ensure_ascii=False, default=str) + "\n")

    def charge(self, usd: float, source: str) -> None:
        self.spent += float(usd or 0.0)
        self.registry.record("ledger", {"run": self.run_id, "source": source, "usd": round(float(usd or 0.0), 6)})

    def over_budget(self) -> bool:
        return self.spent >= self.licence.budget("max_usd_per_task")

    def remaining(self) -> float:
        return max(self.licence.budget("max_usd_per_task") - self.spent, 0.0)


# -- read tools ------------------------------------------------------------------


def read_tools(run: Run) -> list:
    def list_files(args, _context=None):
        pattern = args.get("pattern") or "*"
        hits = [path for path in run.files if fnmatch.fnmatch(path, pattern)]
        return {"files": hits[:400], "total": len(hits)}

    def read_file(args, _context=None):
        path = snapshot.resolve(run.snapshot_dir, args["path"])
        if not path.is_file():
            return {"error": f"no such file: {args['path']}"}
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(int(args.get("start_line") or 1), 1)
        count = min(int(args.get("max_lines") or 400), 1000)
        chunk = lines[start - 1:start - 1 + count]
        return {"path": args["path"], "start_line": start, "lines": len(chunk), "total_lines": len(lines), "text": "\n".join(chunk)}

    return [
        tool(
            name="list_files",
            description="List files in the repository snapshot and the task's attachments (under _inputs/). Glob pattern, e.g. '*.py' or 'src/*'.",
            input_schema={"type": "object", "properties": {"pattern": {"type": "string"}}},
            execute=list_files,
        ),
        tool(
            name="read_file",
            description="Read a text file from the repository snapshot or the task's attachments (_inputs/...). Returns up to max_lines lines from start_line.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"}, "max_lines": {"type": "integer"}},
                "required": ["path"],
            },
            execute=read_file,
        ),
    ]


# -- make_tool -------------------------------------------------------------------

MAKE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "snake_case, 3-41 characters"},
        "description": {"type": "string", "description": "What the tool does, so a later session can find and trust it"},
        "access": {"type": "string", "enum": ["pure", "repository-read", "registry-read"]},
        "input_schema": {"type": "object", "description": "JSON Schema for run(args). Keywords: type, properties, required, items, enum, additionalProperties, description, min/max"},
        "output_schema": {"type": "object", "description": "JSON Schema for the returned dict"},
        "code": {"type": "string", "description": "tool.py: standard-library Python defining run(args: dict) -> dict"},
        "tests": {"type": "string", "description": "test_tool.py: unittest tests that do `from tool import run` and assert concrete values"},
        "gap": {
            "type": "object",
            "properties": {
                "task_quote": {"type": "string", "description": "Words copied exactly from the task that this tool serves"},
                "why_needed": {"type": "string"},
                "why_existing_insufficient": {"type": "string"},
            },
            "required": ["task_quote", "why_needed"],
        },
        "revises": {"type": "string", "description": "Name of an installed tool this becomes a new version of, if any"},
        "disputes": {
            "type": "array",
            "description": "Blind tests you believe assert something false about the real inputs. The test writer re-checks each one; only tests it agrees are wrong are dropped, and every drop is recorded.",
            "items": {"type": "object", "properties": {"test": {"type": "string"}, "reason": {"type": "string"}}, "required": ["test", "reason"]},
        },
    },
    "required": ["name", "description", "access", "input_schema", "output_schema", "code", "tests", "gap"],
}


def make_tool_tool(run: Run):
    async def make_tool(args, _context=None):
        return await _make_tool(run, args)

    return tool(
        name="make_tool",
        description=(
            "Create a new tool for a capability this task needs and you lack, then prove it: the licence check, a lint, "
            "your tests, blind tests written by another model, and a stub check, all in a sandbox with no network. "
            "Returns pass or fail with the failing tests. Nothing is installed until install_tool."
        ),
        input_schema=MAKE_TOOL_SCHEMA,
        execute=make_tool,
    )


async def _make_tool(run: Run, args: dict) -> dict:
    args = _decode_json_fields(args, ("input_schema", "output_schema", "gap", "disputes"))
    name = str(args.get("name", ""))
    limit_made, limit_attempts = run.licence.budget("max_make_tool_per_task"), run.licence.budget("max_attempts_per_tool")
    if run.remaining() < MIN_USD_TO_MAKE:
        return _refuse(run, name, f"spend cap reached (${run.spent:.2f} of ${run.licence.budget('max_usd_per_task')}; a build needs ${MIN_USD_TO_MAKE:.2f} left for its blind tests)")
    if run.made >= limit_made:
        return _refuse(run, name, f"make_tool cap reached ({limit_made} per task)")
    gap = args.get("gap") or {}
    key = _gap_key(gap)
    if run.attempts.get(key, 0) >= limit_attempts:
        return _refuse(run, name, f"attempt cap reached for this gap ({limit_attempts}); renaming the tool does not reset it")
    if not NAME.fullmatch(name) or name in RESERVED:
        return _refuse(run, name, "invalid or reserved name; use 3-41 characters of a-z, 0-9, _")
    active = run.registry.active()
    revises = args.get("revises") or None
    if name in active and revises != name:
        return _refuse(run, name, f"{name}@{active[name]} is installed; call it, or set revises='{name}' to make a new version")
    if not _quotes_task(gap.get("task_quote", ""), run.task):
        return _refuse(run, name, "gap.task_quote must copy words from the task; a gap has to come from the task")
    problems = schema.check_schema(args.get("input_schema"), "input_schema") + schema.check_schema(args.get("output_schema"), "output_schema")
    if problems:
        return _refuse(run, name, "; ".join(problems[:6]))

    manifest = {
        "name": name,
        "version": run.registry.next_version(name),
        "shape": "function",
        "access": args.get("access"),
        "description": str(args.get("description", ""))[:600],
        "input_schema": args["input_schema"],
        "output_schema": args["output_schema"],
        "gap": {**gap, "task": run.task[:2000]},
        "created_by": {"run": run.run_id, "builder": run.licence.model("builder"), "tester": run.licence.model("tester")},
    }
    authority = violations(manifest, run.licence)
    authority += lint.scan(args.get("code", ""), run.licence.data["forbidden_imports"], run.licence.data["forbidden_calls"])
    authority += [f"in tests: {item}" for item in lint.scan(args.get("tests", ""), run.licence.data["forbidden_imports"], run.licence.data["forbidden_calls"], for_tests=True)]
    run.attempts[key] = run.attempts.get(key, 0) + 1
    run.made += 1
    attempt = f"{run.attempts[key]}/{limit_attempts}"
    run.say("create", f"{name}@{manifest['version']} attempt {attempt} ({manifest['access']}): {manifest['description'][:140]}")
    run.say("gap", f"task says: \"{gap.get('task_quote', '')[:160]}\" | why: {gap.get('why_needed', '')[:200]}")
    if authority:
        run.say("refused", f"{name}: " + "; ".join(authority))
        return {"status": "refused", "attempt": attempt, "reasons": authority, "next": "Stay inside the licence: standard library only, no network, no subprocess, no environment, no writes."}

    if name not in run.jev_asked:
        run.jev_asked.add(name)
        advice = jev.advise_gap(run.api_key, run.licence.model("advisor"), run.task, {**manifest, "gap": gap}, run.registry.listing())
        run.charge(advice.cost, "jev")
        run.say("jev", advice.line(), probabilities=advice.probabilities)
        if advice.defer:
            return {"status": "deferred", "attempt": attempt, "advisor": advice.line(), "next": "Reconsider. If the tool is still needed, call make_tool again with the reason in gap."}

    folder = run.run_dir / "candidates" / f"{name}-{manifest['version']}-{run.made}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "tool.py").write_text(args["code"], encoding="utf-8")
    (folder / "test_tool.py").write_text(args["tests"], encoding="utf-8")

    suite_key = f"{name}:{_hash(json.dumps([manifest['input_schema'], manifest['output_schema'], manifest['access']], sort_keys=True))}"
    if suite_key not in run.blind_suites:
        run.say("blind", f"{run.licence.model('tester')} writes acceptance tests for {name} without seeing its code")
        blind, errors = None, []
        for _try in range(TESTER_TRIES):
            try:
                blind = await tester.write_blind_tests(
                    run.client, run.licence.model("tester"), manifest, run.task, read_tools(run), min(TESTER_BUDGET_USD, run.remaining()),
                    run.tester_hooks or run.hooks,
                    stop=[lambda _options: run.over_budget()],
                    plugins=run.licence.data["models"].get("tester_plugins"),
                )
                break
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {str(exc)[:300]}")
                run.say("blind", f"blind test writer failed ({len(errors)}/{TESTER_TRIES}): {errors[-1]}")
                if run.over_budget():
                    break
        if blind is None:
            # The tester's failure is infrastructure, not the builder's: give the attempt back.
            run.attempts[key] -= 1
            run.made -= 1
            return {"status": "retry", "reasons": [f"blind test writer failed: {errors[-1] if errors else 'unknown'}"],
                    "next": "Nothing was tested and your attempt was not counted. Call make_tool again with the same arguments."}
        builder, blind_by = run.models_by_role.get("builder", set()), run.models_by_role.get("tester", set())
        run.say("blind", f"blind tests written by {', '.join(sorted(blind_by)) or '?'}; builder served by {', '.join(sorted(builder)) or '?'}")
        blind_lint = lint.scan(blind, run.licence.data["forbidden_imports"], run.licence.data["forbidden_calls"], for_tests=True)
        if blind_lint:
            run.say("blind", "blind tests broke the licence and were discarded: " + "; ".join(blind_lint))
            run.attempts[key] -= 1
            run.made -= 1
            return {"status": "retry", "reasons": ["the blind tests were rejected by the lint; your attempt was not counted"],
                    "next": "Call make_tool again with the same arguments."}
        run.blind_suites[suite_key] = blind
    await _settle_disputes(run, manifest, suite_key, args.get("disputes") or [])
    (folder / "test_blind.py").write_text(run.blind_suites[suite_key], encoding="utf-8")

    own = run.sandbox.run_tests(folder, manifest["access"], "test_tool.py")
    _show(run, "your tests", own)
    blind_report = run.sandbox.run_tests(folder, manifest["access"], "test_blind.py")
    _show(run, "blind tests", blind_report)
    stubs = {kind: run.sandbox.run_tests_against_stub(folder, manifest["access"], kind=kind) for kind in ("raises", "empty")}
    survivors = sorted({name for report in stubs.values() for name in report.ok})
    total = max((report.ran for report in stubs.values()), default=0)
    stub_ratio = ((total - len(survivors)) / total) if total else 0.0
    run.say("stub", f"against stubs that raise or return {{}}: {total - len(survivors)}/{total} tests fail on both (need >= {int(MIN_STUB_FAIL_RATIO * 100)}%)")

    reasons = []
    if not own.passed:
        reasons.append(f"your tests: {own.summary()}")
    if own.ran < MIN_TESTS:
        reasons.append(f"your tests: {own.ran} ran, need at least {MIN_TESTS}")
    if not blind_report.passed:
        reasons.append(f"blind tests: {blind_report.summary()}")
    if stub_ratio < MIN_STUB_FAIL_RATIO:
        reasons.append(f"vacuous tests pass without any implementation: {', '.join(survivors[:8])}")
    passed = not reasons

    files = {fname: _hash((folder / fname).read_text(encoding="utf-8")) for fname in ("tool.py", "test_tool.py", "test_blind.py")}
    receipt = {
        "candidate": folder.name,
        "passed": passed,
        "tests": {"ran": own.ran, "ok": len(own.ok)},
        "blind_tests": {"ran": blind_report.ran, "ok": len(blind_report.ok)},
        "stub_failed": round(stub_ratio, 3),
        "blind_tests_dropped": run.dropped.get(suite_key, []),
        "sandbox": own.command,
        "sha256": files,
        "licence_sha256": run.licence.sha256,
        "models_served": {role: sorted(models) for role, models in run.models_by_role.items()},
        "independent_tester": not (run.models_by_role.get("builder", set()) & run.models_by_role.get("tester", set())),
        "run": run.run_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (folder / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    candidate_id = f"cand-{uuid.uuid4().hex[:8]}"
    run.candidates[candidate_id] = Candidate(id=candidate_id, manifest=manifest, folder=folder, receipt=receipt)
    run.say("verdict", f"{name}@{manifest['version']}: {'PASS' if passed else 'FAIL'}" + ("" if passed else " - " + "; ".join(reasons)))

    if passed:
        return {"status": "passed", "candidate_id": candidate_id, "version": manifest["version"], "attempt": attempt,
                "tests": own.summary(), "blind_tests": blind_report.summary(), "next": "Call install_tool with this candidate_id."}
    return {
        "status": "failed", "candidate_id": candidate_id, "attempt": attempt, "reasons": reasons,
        "your_test_failures": own.failed[:8],
        "blind_test_failures": [{"test": item["test"], "message": item["message"]} for item in blind_report.failed[:8]],
        "next": "Fix the code (or your tests) and call make_tool again with the same name.",
    }


async def _settle_disputes(run: Run, manifest: dict, suite_key: str, disputes: list) -> None:
    """The test writer re-checks each disputed blind test. Only tests it agrees are wrong are dropped."""
    dropped = run.dropped.setdefault(suite_key, [])
    for item in disputes[:MAX_DROPS_PER_SUITE]:
        test_name, reason = str(item.get("test", "")), str(item.get("reason", ""))
        suite = run.blind_suites[suite_key]
        if len(dropped) >= MAX_DROPS_PER_SUITE:
            run.say("dispute", f"{test_name}: refused, {MAX_DROPS_PER_SUITE} blind tests already dropped for this interface")
            break
        source = tester.test_source(suite, test_name)
        if source is None:
            run.say("dispute", f"{test_name}: no such blind test")
            continue
        if tester.count_tests(suite) - 1 < MIN_TESTS:
            run.say("dispute", f"{test_name}: refused, fewer than {MIN_TESTS} blind tests would remain")
            continue
        try:
            review = await tester.review_dispute(
                run.client, run.licence.model("tester"), manifest, test_name, source, reason, read_tools(run),
                min(DISPUTE_BUDGET_USD, run.remaining()), run.tester_hooks or run.hooks, stop=[lambda _options: run.over_budget()],
                plugins=run.licence.data["models"].get("tester_plugins"),
            )
        except Exception as exc:
            review = {"verdict": "keep", "why": f"review failed ({type(exc).__name__}), so the test stays"}
        run.say("dispute", f"{test_name}: {review['verdict'].upper()} - {review['why']} (implementer said: {reason[:160]})")
        if review["verdict"] == "drop":
            run.blind_suites[suite_key] = tester.drop_test(suite, test_name)
            dropped.append({"test": test_name, "implementer": reason[:400], "reviewer": review["why"]})


# -- install_tool ----------------------------------------------------------------


def install_tool_tool(run: Run):
    def install_tool(args, _context=None):
        candidate = run.candidates.get(args.get("candidate_id", ""))
        if candidate is None:
            return {"status": "refused", "reason": "unknown candidate_id"}
        if not candidate.receipt["passed"]:
            return {"status": "refused", "reason": "that candidate did not pass; nothing is installed without passing tests"}
        current = {fname: _hash((candidate.folder / fname).read_text(encoding="utf-8")) for fname in candidate.receipt["sha256"]}
        if current != candidate.receipt["sha256"]:
            return {"status": "refused", "reason": "the candidate's files changed after testing"}
        installed = run.registry.install(candidate.folder, candidate.manifest, candidate.receipt)
        run.registry.record("gaps", {"run": run.run_id, "tool": installed, "gap": candidate.manifest["gap"]})
        run.installed_now.append(installed)
        run.say("install", f"{installed} installed (receipt: {candidate.receipt['tests']['ok']} own + {candidate.receipt['blind_tests']['ok']} blind tests, stub failed {int(candidate.receipt['stub_failed'] * 100)}%)")
        return {"status": "installed", "tool": installed, "note": "This session ends now. A fresh session continues the task with the tool loaded. Leave a short handoff in your reply."}

    return tool(
        name="install_tool",
        description="Install the exact candidate that passed make_tool into the registry. The session then restarts fresh with the tool available.",
        input_schema={"type": "object", "properties": {"candidate_id": {"type": "string"}}, "required": ["candidate_id"]},
        execute=install_tool,
    )


# -- installed tools -------------------------------------------------------------


def installed_tools(run: Run) -> list:
    loaded = []
    for name, version in sorted(run.registry.active().items()):
        if name in RESERVED:
            continue
        manifest = run.registry.manifest(name, version)
        loaded.append(tool(
            name=name,
            description=f"{manifest['description']} [Golem tool {name}@{version}, {manifest['access']}]",
            input_schema=manifest["input_schema"],
            execute=_proxy(run, name, version, manifest),
        ))
    return loaded


def _proxy(run: Run, name: str, version: str, manifest: dict):
    bundle = run.registry.bundle(name, version)

    def call(args, _context=None):
        problems = schema.validate(args, manifest["input_schema"])
        if problems:
            return {"error": "arguments do not match the input schema", "problems": problems}
        outcome = run.sandbox.invoke(bundle, manifest["access"], args)
        if outcome.get("ok"):
            problems = schema.validate(outcome["result"], manifest["output_schema"])
            if problems:
                outcome = {"ok": False, "error": "output does not match the output schema", "problems": problems, "seconds": outcome.get("seconds")}
        run.registry.record("usage", {"run": run.run_id, "tool": f"{name}@{version}", "ok": bool(outcome.get("ok")), "seconds": outcome.get("seconds")})
        run.used.append(f"{name}@{version}")
        run.say("call", f"{name}@{version} in sandbox: {'ok' if outcome.get('ok') else 'error'} ({outcome.get('seconds')}s)")
        return outcome.get("result") if outcome.get("ok") else {"error": outcome.get("error"), "problems": outcome.get("problems", [])}

    return call


# -- helpers ---------------------------------------------------------------------


def _refuse(run: Run, name: str, reason: str) -> dict:
    run.say("refused", f"make_tool {name or '?'}: {reason}")
    return {"status": "refused", "reason": reason}


def _show(run: Run, label: str, report: TestReport) -> None:
    run.say("test", f"{label}: {report.summary()} in {report.seconds}s | {report.command}")
    for item in report.failed[:6]:
        run.say("test", f"  {item['status']} {item['test']}: {item['message'][:200]}")


def _decode_json_fields(args: dict, fields: tuple[str, ...]) -> dict:
    """Some models send nested objects as JSON strings. Decode those; leave anything else for the checks."""
    decoded = dict(args)
    for key in fields:
        value = decoded.get(key)
        if isinstance(value, str) and value.strip()[:1] in ("{", "["):
            try:
                decoded[key] = json.loads(value)
            except json.JSONDecodeError:
                pass
    return decoded


def _gap_key(gap: dict) -> str:
    """Attempts are counted per gap, by the task words it quotes, so a rename cannot reset them."""
    return "gap:" + re.sub(r"\s+", " ", str(gap.get("task_quote", ""))).strip().lower()


def _quotes_task(quote: str, task: str) -> bool:
    def norm(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip().lower()

    quote = norm(quote)
    return len(quote) >= 8 and quote in norm(task)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clean_candidates(run: Run) -> None:
    shutil.rmtree(run.run_dir / "candidates", ignore_errors=True)
