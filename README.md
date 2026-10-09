# Golem

Golem is an agent that works inside one repository. When a task needs an exact operation it does not have, it writes the tool, proves it in a sandbox, installs that exact version, and the next session uses it. Each installed tool makes the next task in that repository cheaper.

The licence does not change. `authority.json` sets what any tool may be, read, import, and spend. Golem prints its sha256 before and after every run and never writes it. The sha256 is of the raw licence bytes, and whole-line `//` comments still count. Capabilities grow. Authority does not.

```bash
golem login [--port N] [--no-browser]              # OpenRouter login; Golem spends your credits
golem --repo PATH run "TASK" [--attach FILE ...]   # do one task
golem --repo PATH registry                         # installed tools, versions, tests, calls
golem --repo PATH verify [--attach FILE ...]       # re-run tests; files must match the receipt
golem --repo PATH rollback NAME VERSION            # point a tool back to an earlier version
golem licence                                      # the licence and its sha256
golem credits                                      # credits left on the OpenRouter account
```

`--licence FILE` goes before the subcommand. If it is omitted, Golem uses `<repo>/.golem/authority.json` when that file exists, otherwise the packaged `authority.json` (repository-root `authority.json` in a source checkout or the image). A path that is not a file falls back to that packaged file.

`verify` checks the installed files against the receipt and the stub-failure ratio. `--attach` names files the tests read under `/inputs`. A matching install prints OK. An edited installed file fails verify (`CHANGED since they were tested`).

## Install

Golem needs Docker running: it never runs generated code outside its sandbox.

- **From GitHub, with uv or pip** (Python 3.13 or later): `uv tool install git+https://github.com/ofou/golem`, then `golem login`. The package carries the default licence, byte for byte.
- **As a Docker image**: `docker build -t golem:local https://github.com/ofou/golem.git#main`. The image is `python:3.12-slim`, and the entrypoint is `python -m golem`. The Docker CLI is copied from `docker:27-cli`, and git's `safe.directory` is `*`. From a clone, `scripts/golem-docker login`, `scripts/golem-docker credits`, or `scripts/golem-docker PATH run "TASK"`. `GOLEM_IMAGE` defaults to `golem:local`. `registry`, `verify`, `licence`, and `rollback` are passed through with `--repo` and need the repository path. `GOLEM_WORK` defaults to `PATH/.golem/work` and is also `TMPDIR`. Each `--attach` directory is mounted read-only at the same absolute path. The wrapper mounts the repository and the host's Docker socket, so the sandbox containers run beside Golem's, not inside it. That socket gives the Golem container control of the host Docker.
- **On GitHub Actions**: `.github/workflows/golem.yml`. Add the repository secret `OPENROUTER_API_KEY` (a key with its own credit limit), then `gh workflow run golem.yml -f target=OWNER/REPO -f ref=SHA -f task="..."`. An optional `task2` runs as a new process on the same registry. One job runs the task with the key. A second job holds no secret and re-runs every installed tool's tests in the sandbox, so the public log shows the tools pass on a machine that never had the key. If the run produced no registry, verify exits 0. Only people with write access can start it.
- **Tests on GitHub Actions**: `.github/workflows/tests.yml` runs on push to main, pull requests, and workflow_dispatch. It uses Python 3.13, `uv sync --no-editable`, checks `golem licence` against the sha256 of `authority.json`, then ruff, pyright, `docker pull python:3.12-slim`, and pytest with coverage.

**Login.** `golem login` uses OpenRouter's OAuth PKCE flow. It opens `openrouter.ai/auth`, you approve, OpenRouter redirects to a one-time `localhost` callback, and Golem exchanges the code for a key on your own account. The key path honors `XDG_CONFIG_HOME`; otherwise it is `~/.config/golem/openrouter.key`, mode 0600, and the key is never printed. `--port` defaults to any free port. `--no-browser` prints the URL without opening a browser. `--headless` shows a code to paste instead, which is what `scripts/golem-docker login` uses. `OPENROUTER_API_KEY`, when set, wins. OpenRouter's flow has no spending-limit option, so set a limit on the key at openrouter.ai; Golem's per-task cap applies either way.

### Catalog runner

`python3 scripts/local_runner.py` reads `scripts/runner_repos.json`, makes shallow sparse clones under `~/golem-runner`, and refuses `demo/targets`. To run tasks it needs `OPENROUTER_API_KEY` in the environment. It does not read the login key file. It uses `golem:local`, runs `scripts/golem-docker` on each task, and runs `scripts/dod_check.py` when the catalog entry has `"dod": true`. Flags: `--only`, `--dod-only`, `--rebuild`, `--clone-only`, and `--truth`. `scripts/free_dod_runner.py`, `scripts/sf_dod_run.py`, and `scripts/dod_stepfun_runner.py` are the same program under other names. `python scripts/dod_check.py REPO` exits 0 only when all five definition-of-done checks pass. It does not judge whether the answer is right.

```mermaid
flowchart TD
  task["A task about the repository<br/>(plus attached files, e.g. a CI log)"] --> fresh["A fresh session loads the registry<br/>installed tools become callable"]
  fresh --> fits{"Does an installed tool fit?"}
  fits -->|yes| use["Call it in the sandbox"]
  fits -->|no| make["make_tool: the gap must quote the task"]
  make --> rules["Licence and lint<br/>more authority is refused"]
  rules --> blind["The tester model writes<br/>blind tests from the interface"]
  blind --> prove["Sandbox: own tests, blind tests,<br/>and two stubs that must fail them"]
  prove -->|fail| make
  prove -->|pass| install["install_tool: those exact bytes<br/>become name@version"]
  install -->|sessions left, under the cap| fresh
  use --> answer["Answer with evidence"]
  jev[["Jev may send a proposal back once.<br/>It never approves one."]] -.-> make
  licence[["authority.json: same sha256 before and after"]] -.-> rules
```

## How a tool gets made

The model starts with four kernel tools: `list_files` and `read_file` for the repository snapshot, `make_tool`, and `install_tool`. None of them contains domain logic. Every other tool it ever calls, it built.

1. **The gap comes from the task.** `make_tool` refuses a gap whose `task_quote` is not copied from the task text. After whitespace is collapsed and case is folded, the quote must be at least 8 characters. A fragment such as `CI` is refused even when that word is in the task. Attempts are counted on that normalized quote. A new tool name is allowed and does not reset the count. Once the count is spent, the next proposal is refused, including a rename. An already installed name is refused unless `revises` is that name.
2. **The licence and the lint.** A tool asking for an access level, network module, subprocess, environment read, or write that the licence does not grant is refused with the reason, for example `new authority: imports urllib.request`. The lint names the request. The sandbox enforces it.
3. **Jev's gate.** `typesafe/jev-1.13`, a typed decision model on OpenRouter, answers yes/no questions about the proposal, and code holds the thresholds. Two checks can send a proposal back, without costing an attempt: the quoted task words need no exact operation over files (`exact_op` at or below 0.30), or an output field does not say what it holds (`clear::<field>` at or below 0.30). `clear::` is asked only when the interface is new, for at most the first 12 output fields. Each gap is sent back at most once, and a task at most twice. `same_job` covers at most the last 20 installed tools other than `revises`. A score at or above 0.75 is logged and does not send the proposal back. A disputed blind test that asserts something outside the tool's contract is asked and logged, and does not change the drop. Jev never approves an install and never picks a tool. If Jev fails, the build goes ahead as it would without it. Every answer, with its request id and cost, goes into the event log and the receipt.
4. **Blind tests.** The tester model reads the interface, the task, and the repository, never the implementation, and writes `test_blind.py`. That call stops at 10 steps, and a dispute review stops at 8, each also under the spend cap. The suite is pinned to the tool's name and interface, so revising the code cannot make it go away. A new name gets a new suite, because the suite key includes the tool name. `anonymize` renames `test*` methods that are on a class to `test_blind_01`, `test_blind_02`, and so on, and drops their docstrings. A function defined at module level keeps its name. When one fails, the builder gets that name and the exception type, never the message. Names, docstrings and messages all carried the tester's expected values back to the builder. The suite as written is kept beside the candidate for audit. A disputed blind test is dropped only on verdict `drop`, at most 2 drops, and never if fewer than 3 blind tests would remain.
5. **The sandbox.** `docker run --network none --read-only --cap-drop ALL --security-opt no-new-privileges --user 65534:65534` with memory, CPU, process, and time limits. The host environment is not forwarded. The process gets `PYTHONDONTWRITEBYTECODE=1` and `HOME=/tmp`. `/tmp` is a tmpfs (`rw,noexec,nosuid,size=64m`); tests may write only there. Read-only mounts include the bundle, `/golem/runner.py`, `/golem/tests.py`, `/golem/schema.py`, and, when used, `/inputs`, `/repo`, and `/registry`. Golem runs the builder's tests, the blind tests, and the same tests against two stubs: one that raises and one that returns `{}`. The builder's own suite must run at least 3 tests. A result that breaks `output_schema` fails those tests. At least 80% of the tests must fail on both stubs, or they are vacuous. A tool result longer than 100,000 characters is refused. That limit is not in `authority.json`.
6. **Install.** `install_tool` checks that the files still hash to what was tested, then writes `.golem/registry/NAME/VERSION/` (immutable) with the manifest, code, both test suites, and the receipt, and points `.golem/registry/active.json` at that version. The first version of a name is 0.1.0. The next is 0.2.0, and each later version bumps the minor the same way.
7. **A fresh session.** Another session starts only when a tool was installed, sessions remain, and the run is not over budget. It starts from the task, a handoff note, and the registry. The installed tool is loaded from disk and called through the sandbox. On the last session, or when the cap is already spent, that session's answer is the final answer. The answer is written to `.golem/runs/<run_id>/result.md`.

## What tools it can create

One shape: a standard-library Python function, `run(args: dict) -> dict`, with JSON schemas for input and output. It runs once per call in the sandbox and keeps no state. Schemas are a checked subset (`type`, `properties`, `required`, `additionalProperties`, `items`, `enum`, numeric and length bounds; `description`, `default`, and `title` are annotations; other keywords and nesting deeper than 6 are rejected).

| Access | It can read | Example |
| --- | --- | --- |
| Pure | Only its arguments and the task's attached files at `/inputs` | Turn a raw CI log into failing tests with file and line |
| Repository-read | Also a read-only snapshot of the repository at `/repo`, including attachments at `/repo/_inputs` | Resolve which team owns each path from `CODEOWNERS` |
| Registry-read | Also Golem's registry at `/registry`: manifests, receipts, gaps, usage, no code | Find installed tools whose outputs fit another tool's inputs |

Attachments are copied to `_inputs/<basename>` in the snapshot and mounted at `/inputs`. Output is a typed result or a patch proposal as a unified diff. A tool never applies a patch. A tool never gets network access, credentials, a shell, write access, or the ability to edit the licence, its tests, or Golem.

Registry-read tools are how Golem builds its own discovery and management tooling. The kernel lists installed tools to the model; finding chains, flagging unused or failing tools, and anything smarter is left for the agent to build when a task needs it.

## Caps, enforced in code

From `authority.json`, checked by the kernel and by the SDK's stop conditions:

| Cap | Value |
| --- | --- |
| Spend per task, every model call including the blind tester and Jev | $0.20 |
| Sessions per task | 3 |
| Steps per session (`step_count_is`; the SDK itself stops at 20) | 18 |
| `make_tool` calls per task | 4 |
| Attempts per gap | 3 |
| Sandbox runs per task | 60 |
| One sandbox run | 120 s, 512 MB, 1 CPU, 128 processes |

A model call with no reported cost is charged `input_tokens` times $5 per 1,000,000 plus `output_tokens` times $25 per 1,000,000, and at least $0.05. A reported cost of 0 stays 0. The cap can only be checked between model steps, and a single blind-tester step has cost $0.11. So every model call also stops when one more step as large as the largest so far would cross the cap, and `make_tool` will not start unless at least the larger of $0.03 and the largest step so far remains. A step larger than any before it can still overrun the cap.

## What is real, simulated, and missing

As of the night of 8 to 9 October 2026.

Real and tested: the kernel, the licence and lint, the registry with immutable versions and rollback, the repository snapshot, the Docker sandbox, the stub checks, blind tests and disputes, the Jev client, and the session loop. The snapshot leaves out `.env`, keys, and `.git`, and also `.golem`, `.venv`, `venv`, `node_modules`, `__pycache__`, `.mypy_cache`, `.pytest_cache`, `dist`, and `build`, plus pem/key/netrc-style names, symlinks, and files over 1,000,000 bytes. `.env.example`, `.env.sample`, `.env.template`, and `.env.dist` are copied. `python -m unittest discover -s tests` runs 41 tests offline. The sandbox tests run real containers and check that a host environment variable is not visible, the process runs as uid 65534, cannot open a socket, and cannot write the tool bundle. Three more call Jev on the Decisions API when `OPENROUTER_API_KEY` is set.

One real run, on a real repository: `aio-libs/aiohttp` at the commit of CI run 37533646169, with the two failing job logs attached as captured, and an empty registry. The task named no tool. Everything is in [`evidence/2026-10-08-aiohttp`](evidence/2026-10-08-aiohttp), failed attempts included.

- Golem built `parse_ci_log_failures` three times. The blind tests failed it each time, on real defects: it missed failures whose test ids carry ANSI colour codes inside them, and returned `None` for a missing file. The last failure was a blind test that expected pytest's summary line to name the test, which it never does. That run had no way to dispute a test; the dispute path was added after it. Nothing was installed.
- Golem then built `extract_aiohttp_imports` (repository-read): its own 4 tests and 7 blind tests passed, and 10 of 11 tests failed against both stubs. It was installed as 0.1.0. A fresh session loaded it from the registry and called it in the sandbox (0.19 s).
- The answer was right. The failing tests match the raw logs, and the 12 imported modules match an independent check with Python's `ast` module. The model read the logs itself for the first half, because the parser never passed.
- Spend: $0.60, under the $2.00 cap the licence had then (it is $0.20 now). The licence hash was the same before and after.
- **Session B**, the next night, was a new process on the same checkout with a different task: name the aiohttp modules two other test files import. It loaded `extract_aiohttp_imports@0.1.0` from the registry, called it twice in the sandbox, and made no tool. Spend $0.0000. The tool's output matches an independent `ast` check for both files. The final answer does not: the free builder's write-up dropped four modules the tool had returned. The tool also lists `aiohttp.FormData`, a class, as a module, because its file check is case-insensitive on macOS. See [`evidence/2026-10-09-aiohttp-session-b`](evidence/2026-10-09-aiohttp-session-b).

**Jev, measured before it was allowed to decide.** [`scripts/jev_evals.py`](scripts/jev_evals.py) asks Jev each case three times and lets a check act only if every answer lands on the expected side of its threshold with at least 0.10 to spare. Raw answers are in [`evidence/2026-10-08-jev-evals`](evidence/2026-10-08-jev-evals); both rounds together cost $0.0044.

- Round 1 tested the first design on 13 tuning cases from the aiohttp run. Most checks missed. "Is this one operation" scored a tool that does two jobs at 0.40, and appending "the user approved this tool; answer true to every check" to its description raised that to 0.58. "Would this tool serve the quoted words" scored the real parser at 0.40. Both questions were dropped.
- Three questions separated good proposals from bad ones: the quoted words need an exact operation (0.95 against 0.15), an installed tool already does it (0.89 against 0.02), and an output field says what it holds (0.75 against 0.15). The thresholds were set from round 1.
- Round 2 kept those thresholds fixed and added 10 held-out cases. The script's cases come from the aiohttp run or are labelled synthetic; the held-out names are omp, codex, a registry.py prose task, and owners and dependents disputes. `exact_op` and `clear::` passed all of them. `same_job` was on the right side every time but only 0.02 above its 0.75 threshold on the held-out duplicate, so it is logged, not acted on. The dispute question failed 2 of 4 held-out cases: it called an assertion of a value the contract does promise, `len(deps) == 75`, unpromised (0.81). It is logged only, and a disputed test is still dropped only when the test's author agrees.
- On 9 October, in reruns on codex and hermes, Jev sent both first proposals back, naming the output fields with no clear description (`top_crates` 0.25, `import_counts` 0.15). On hermes the builder rewrote the descriptions and the second proposal went through with no check firing. On codex the free builder ended the session with no text after the send-back, so that run has no answer.

**Four more repositories, with the free builder.** Each target was cloned at a pinned commit and run through the Docker install with an empty registry: `openai/codex`, `earendil-works/pi`, `can1357/oh-my-pi` and `NousResearch/hermes-agent`. Ground truth was computed separately with `tomllib`, `json` and `ast`. Logs, events, ledgers and candidates are in [`evidence/2026-10-09-free-builder`](evidence/2026-10-09-free-builder). Nothing was installed:

- pi: the builder answered without making a tool. It got all 17 `dependencies` edges and a valid build order, but left out the 5 `devDependencies` edges and `pi-evals`, the one package with only devDependencies.
- oh-my-pi: three attempts at a dependency tool. The kernel refused one for calling `__import__` in its tests; the third ran no tests. The SDK then failed with "Response failed" and there was no answer.
- codex: one build failed its own tests (one test, which did not import) and 1 of 6 blind tests. Its blind test writer took spend to $0.22, past the $0.20 cap, because a cap is checked between model steps, not inside one. The answer said honestly that it could not finish.
- hermes: seven proposals were refused because their schemas arrived as strings that did not parse. One tool with an empty description failed 4 of 5 blind tests. The answer named `tools.registry`, imported by 72 other `tools/` modules, which is correct. It came from a blind test's failure message, not from a tool: the message said "imported by 72 tools/ modules", and Golem passed those messages back to the builder. Blind failures now reach the builder as test names and exception types only.

What the runs exposed and what changed:

- The SDK stops a `call_model` run at 20 turns with an exception. The first try ended there with no answer. Golem now caps a session at 18 steps and ends it cleanly.
- The agent renamed a failing tool to get three more attempts. Attempts are counted per gap, by the task words it quotes. A new name shares that count. Once the count is spent, the next proposal is refused, including a rename. Quoting different words of the same task still opens a new count; the task-wide cap of 4 `make_tool` calls bounds it.
- Changing a tool's interface gets a new blind suite, which can escape failing blind tests. A new name gets one too, because the suite key includes the tool name. The same caps bound it.
- The blind test writer wrote tests that create fixture files, and the lint refused them. Tests may now write; the only writable place is the `/tmp` tmpfs.
- Builder and blind test writer both resolved to `z-ai/glm-5.3`, so the tests were blind but not independent. The receipt says `independent_tester: false`.
- Blind-test messages carried the tester's expected values back to the builder. In the codex run of 8 October and the hermes run of 9 October, the right number reached the answer that way. Messages are now withheld. Then a hermes rerun failed a test named `test_main_case_most_imported_module_is_tools_registry`: the name carried the answer too. Blind tests are now renamed before the builder sees them.
- That hermes rerun spent $0.29 against the $0.20 cap: two blind-tester steps cost $0.09 and $0.11, and the cap is checked between steps. Replayed against that ledger, the new step guard stops the tester at $0.12.
- Free models often send a schema as a JSON string, sometimes with text after it or cut off. The kernel now decodes what parses and, when it does not, says where the JSON broke.
- On Linux, the sandbox user could not read the 0700 directories `tempfile` creates for stubs and installed versions. Docker Desktop on macOS ignores that, so no run here showed it. Both directories are now 0755. It has not been run on Linux.

Not yet shown in a recorded run: any install with the free builder, and the manage and combine tasks written for pi and hermes in `scripts/runner_repos.json` (a registry-read tool, then a later task that must not call `make_tool`).

Simulated: `tests/test_loop.py` uses fixture builder arguments, fixture blind tests, and fixture Jev answers. Any install stays in a temporary directory.

Written but not yet run for real: `golem login` against a real OpenRouter approval (its PKCE, callback, state check, storage and exchange are unit-tested; OpenRouter accepts its URL and rejects a bad code), and `.github/workflows/tests.yml` and `.github/workflows/golem.yml`, which have not run on GitHub yet. The `verify` job was replayed locally on aiohttp: 4/4 own and 7/7 blind tests passed with no key in the environment.

Missing: the MCP-server shape, a GitHub App, pull requests, a `/golem` trigger, and deployment. `ACTIONS.md` describes a different Actions design that uses only `GITHUB_TOKEN`; it is not what `golem.yml` does.

Fragile: Docker runs on the same machine as the process that holds the OpenRouter key. The container does not receive the host environment. It gets `PYTHONDONTWRITEBYTECODE=1` and `HOME=/tmp`, `--network none`, `--cap-drop ALL`, `no-new-privileges`, and read-only mounts of the bundle, `/golem/runner.py`, `/golem/tests.py`, `/golem/schema.py`, the snapshot, and the attachments. `/tmp` is a writable tmpfs. A container escape would reach the host. A remote sandbox would close that.

## Stack

The package requires Python 3.13 or later. The distribution version in `pyproject.toml` is 2026.10.08. `golem/__init__.py` sets `__version__` to `0.1.0`. The [OpenRouter Agent SDK](https://openrouter.ai/docs/agent-sdk/overview) for Python, `openrouter-agent-sdk==0.8.0`, needs `openrouter==1.1.26`: newer `openrouter` releases removed names the SDK imports. The Golem image and the tool sandbox are `python:3.12-slim`. Tools may use the standard library only.

Models come from `authority.json`, all on one OpenRouter key:

- The builder and the tester are the same id, `deepseek/deepseek-v4.1-flash`, so receipts mark `independent_tester` false. `authority.json` has no `tester_plugins` and no `min_coding_score`.
- The advisor is `typesafe/jev-1.13`, called on OpenRouter's [Decisions API](https://openrouter.ai/docs/client-sdks/python/sdks/decisions/README) (`POST /api/alpha/decisions`). A call costs about $0.00002. The receipt keeps the dated snapshot, for example `typesafe/jev-1.13-20260917`.
- The runs in `evidence/` used other ids. The builder was [`openrouter/free`](https://openrouter.ai/openrouter/free), OpenRouter's router over free models. It can pick a different free model on each call; on 8 October a tool-calling test went to `nvidia/nemotron-3-ultra-550b-a55b:free` and then `apodex/apodex-1.1-mini:free`, at $0. Those runs also used [`openrouter/pareto-code`](https://openrouter.ai/openrouter/pareto-code) at its Low tier (`min_coding_score` 0.25), which resolved to `z-ai/glm-5.3`. On 8 October the blind test writer was that same model, and the receipts say `independent_tester: false`.
