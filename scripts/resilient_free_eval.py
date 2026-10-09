#!/usr/bin/env python3
"""Run DoD + smoke with golem:free-dod; unique name to avoid pkill races."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("EVAL_ROOT", str(Path.home() / "golem-runner-free")))
GOLEM = Path(__file__).resolve().parents[1]
DOCKER = GOLEM / "scripts" / "golem-docker"
DOD_CHECK = GOLEM / "scripts" / "dod_check.py"
IMAGE = "golem:free-dod"
LOG = ROOT / "logs"
WORK = ROOT / "work"
SRC = ROOT / "src"

DOD = [
  ("pi", SRC / "pi", [
    ("build", "List the internal dependencies between the packages directly under packages/ (each package.json). Count a dependency, devDependency, or peerDependency when its name is another of those packages. Give an order in which the packages can be built so that every package comes after the packages it depends on. If the graph has a cycle, name one cycle instead of an order."),
    ("build", "For each package directory directly under packages/ that has a package.json, how many files does the repository snapshot see under that package directory, including nested files? List each package.json name with its file count."),
    ("manage", "Using the registry export (manifests, receipts, gaps, usage — not tool code), list every installed tool with its version, the task words its gap quotes, and which installed tools' output fields could feed another installed tool's input fields. If no registry-read tool is installed yet, create, test, and install one that answers this from the registry export, then answer."),
    ("combine", "Without calling make_tool and without rebuilding any tool: report the internal package build order (or one cycle), and for each package report how many snapshot files sit under its directory. Use only installed Golem tools."),
  ]),
  ("hermes", SRC / "hermes", [
    ("build", "Under tools/, which Python modules import other modules from tools/, and which tools/ module is imported by the most other tools/ modules? Resolve relative imports and imports that start with tools. Do not count a file as importing itself. If several modules tie for the most imported, name all of them."),
    ("build", "Under tools/, how many .py files are there in each immediate subdirectory of tools/ (count nested .py files toward that subdirectory)? Which immediate subdirectory has the most .py files? If several tie, name all of them."),
    ("manage", "Using the registry export (manifests, receipts, gaps, usage — not tool code), list every installed tool with its version, the task words its gap quotes, and which installed tools' output fields could feed another installed tool's input fields. If no registry-read tool is installed yet, create, test, and install one that answers this from the registry export, then answer."),
    ("combine", "Without calling make_tool and without rebuilding any tool: name the tools/ module imported by the most other tools/ modules, and name the immediate tools/ subdirectory with the most .py files. Use only installed Golem tools."),
  ]),
]

SMOKE = [
  ("cline", SRC / "cline", "List the internal dependencies between the packages directly under sdk/packages/ (each package.json). Count a dependency, devDependency, or peerDependency when its name is another of those packages. Give an order in which the packages can be built so that every package comes after the packages it depends on. If the graph has a cycle, name one cycle instead of an order."),
  ("omp", SRC / "omp", "List the internal dependencies between the packages directly under packages/ (each package.json). Count a dependency, devDependency, or peerDependency when its name is another of those packages. Give an order in which the packages can be built so that every package comes after the packages it depends on. If the graph has a cycle, name one cycle instead of an order."),
]

def _exe(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise SystemExit(f"{name} is not on PATH")
    return found

def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with (LOG / "resilient.log").open("a") as f:
        f.write(line + "\n")

def stop_competitors() -> None:
    docker = shutil.which("docker")
    if docker is None:
        return
    try:
        out = subprocess.check_output(  # noqa: S603
            [docker, "ps", "--format", "{{.ID}} {{.Image}}"],
            text=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        cid, img = parts
        if img in ("golem:free-dod", "golem:dod"):
            continue
        if img.startswith("golem:") or "stepfun" in img or "quick" in img:
            subprocess.run(  # noqa: S603
                [docker, "stop", cid],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            log(f"stopped competitor {img} {cid[:12]}")

def run_task(repo: Path, work: Path, task: str, log_path: Path, retries: int = 2) -> int:
    env = os.environ.copy()
    env["GOLEM_IMAGE"] = IMAGE
    env["GOLEM_WORK"] = str(work)
    code = 1
    for attempt in range(1, retries + 1):
        stop_competitors()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w") as handle:
            handle.write(f"repo {repo}\ntask {task}\nattempt {attempt}\n\n")
            handle.flush()
            code = subprocess.run(  # noqa: S603
                [_exe("bash"), str(DOCKER), str(repo), "run", task],
                stdout=handle, stderr=subprocess.STDOUT, env=env, check=False,
            ).returncode
        log(f"  exit {code} attempt {attempt} log={log_path.name}")
        if code not in (137, 143, -9):
            return code
        log(f"  retry after kill signal {code}")
        time.sleep(5)
    return code

def extract_brief(path: Path) -> str:
    if not path.exists():
        return ""
    text = path.read_text(errors="replace")
    bits = []
    for key in ("[models]", "[spend]", "[registry] after", "PASS", "FAIL"):
        for line in text.splitlines():
            if key in line:
                bits.append(line.strip())
    return " | ".join(bits[-6:])

def clone_if_needed(name: str, github: str, sparse: list[str]) -> Path:
    dest = SRC / name
    if dest.exists() and (dest / ".git").exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    log(f"cloning {github} → {dest}")
    git = shutil.which("git")
    if git is None:
        raise OSError("git is not on PATH")
    subprocess.check_call(  # noqa: S603
        [git, "clone", "--depth", "1", "--filter=blob:none", "--sparse", f"https://github.com/{github}.git", str(dest)]
    )
    subprocess.check_call(  # noqa: S603
        [git, "-C", str(dest), "sparse-checkout", "set", "--cone", *sparse]
    )
    return dest

def run_dod() -> list[dict]:
    rows = []
    for name, dest, tasks in DOD:
        if not dest.exists():
            log(f"SKIP {name}: missing src")
            rows.append({"name": name, "status": "failed", "reason": "missing src"})
            continue
        golem_dir = dest / ".golem"
        if golem_dir.exists():
            shutil.rmtree(golem_dir)
        work = WORK / name
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        log(f"=== {name} DoD ({len(tasks)} tasks) ===")
        steps = []
        failed = False
        for i, (phase, task) in enumerate(tasks):
            lp = LOG / f"{name}-{i:02d}-{phase}.log"
            log(f"{name} task {i+1}/{len(tasks)} ({phase})")
            code = run_task(dest, work, task, lp)
            steps.append({"index": i, "phase": phase, "exit": code, "log": str(lp), "brief": extract_brief(lp)})
            if code != 0:
                failed = True
                rows.append({"name": name, "status": "failed", "exit": code, "dod": True, "failed_at": i, "steps": steps})
                break
        if failed:
            continue
        dod_path = LOG / f"{name}-dod_check.log"
        log(f"{name} dod_check")
        with dod_path.open("w") as handle:
            dod_code = subprocess.run(  # noqa: S603
                [sys.executable, str(DOD_CHECK), str(dest)],
                stdout=handle,
                stderr=subprocess.STDOUT,
                check=False,
            ).returncode
        rows.append({
            "name": name, "status": "ok" if dod_code == 0 else "failed", "exit": dod_code,
            "dod": True, "dod_ok": dod_code == 0, "dod_log": str(dod_path),
            "steps": steps, "brief": extract_brief(dod_path),
        })
        log(f"{name} dod_check {'PASS' if dod_code==0 else 'FAIL'} exit={dod_code}")
    return rows

def run_smoke() -> list[dict]:
    catalog = {
        "cline": ("cline/cline", ["sdk/packages"]),
        "omp": ("can1357/oh-my-pi", ["packages"]),
    }
    rows = []
    for name, dest, task in SMOKE:
        github, sparse = catalog[name]
        try:
            dest = clone_if_needed(name, github, sparse)
        except (subprocess.CalledProcessError, OSError) as e:
            rows.append({"name": name, "status": "failed", "reason": f"clone: {e}"})
            continue
        golem_dir = dest / ".golem"
        if golem_dir.exists():
            shutil.rmtree(golem_dir)
        work = WORK / name
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        lp = LOG / f"{name}.log"
        log(f"=== smoke {name} ===")
        code = run_task(dest, work, task, lp)
        rows.append({"name": name, "status": "ok" if code == 0 else "failed", "exit": code, "dod": False, "log": str(lp), "brief": extract_brief(lp)})
    return rows

def main() -> int:
    LOG.mkdir(parents=True, exist_ok=True)
    (LOG / "resilient.log").write_text("")
    if not os.environ.get("OPENROUTER_API_KEY"):
        key = Path.home() / ".config/golem/openrouter.key"
        if key.exists():
            os.environ["OPENROUTER_API_KEY"] = key.read_text().strip()
    log(f"start IMAGE={IMAGE} ROOT={ROOT}")
    out = subprocess.check_output(  # noqa: S603
        [_exe("docker"), "run", "--rm", "--entrypoint", "cat", IMAGE, "/opt/golem/authority.json"],
        text=True,
    )
    models = json.loads(out)["models"]
    log(f"image models {models}")
    rows = run_dod()
    rows.extend(run_smoke())
    (ROOT / "results.json").write_text(json.dumps(rows, indent=2) + "\n")
    home_runner = Path.home() / "golem-runner"
    home_runner.mkdir(exist_ok=True)
    (home_runner / "results.json").write_text(json.dumps(rows, indent=2) + "\n")
    log_dir = home_runner / "logs"
    log_dir.mkdir(exist_ok=True)
    for p in LOG.glob("*-dod_check.log"):
        shutil.copy2(p, log_dir / p.name)
    for p in LOG.glob("*.log"):
        if p.name.endswith("-build.log") or p.name.endswith("-manage.log") or p.name.endswith("-combine.log") or p.name in ("cline.log", "omp.log", "resilient.log"):
            shutil.copy2(p, log_dir / p.name)
    log(f"wrote {ROOT/'results.json'}")
    ok = all(r.get("status") == "ok" for r in rows)
    return 0 if ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
