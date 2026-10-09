# Golem

Golem is an agent that works inside one repository. When a task needs an operation it does not have, it writes a Python tool, proves that tool in a sandbox, and installs that exact version. Later sessions in the same repository call the installed tool.

What a tool may read, import, and spend is fixed by `authority.json`. Golem prints that file's sha256 before and after every run and does not modify it. Whole-line `//` comments are part of the hashed bytes. Installed tools accumulate. The licence stays the same.

## Requirements

- Python 3.13 or later
- Docker. Generated code runs only in the sandbox.
- An OpenRouter account. Golem spends credits on that account.

## Install

```bash
uv tool install git+https://github.com/ofou/golem
golem login
```

`pip install` from the same URL works the same way. The wheel includes the default licence.

To run from a container:

```bash
docker build -t golem:local https://github.com/ofou/golem.git#main
scripts/golem-docker login
scripts/golem-docker PATH run "TASK"
```

The image is `python:3.12-slim` and the entrypoint is `python -m golem`. `scripts/golem-docker` mounts the repository and the host Docker socket, so sandbox containers run beside Golem. That socket gives the Golem container control of the host Docker. `GOLEM_IMAGE` defaults to `golem:local`.

## Commands

```bash
golem login [--port N] [--no-browser]
golem --repo PATH run "TASK" [--attach FILE ...]
golem --repo PATH registry
golem --repo PATH verify [--attach FILE ...]
golem --repo PATH rollback NAME VERSION
golem licence
golem credits
```

`--repo` and `--licence` go before the subcommand. `--repo` defaults to the current directory. If `--licence` is omitted, Golem uses `<repo>/.golem/authority.json` when that file exists, otherwise the packaged licence.

`login` uses OpenRouter's OAuth PKCE flow and stores the key at `${XDG_CONFIG_HOME:-~/.config}/golem/openrouter.key` with mode `0600`. The key is not printed. `OPENROUTER_API_KEY`, when set, takes precedence. Set a spending limit on the key at [openrouter.ai](https://openrouter.ai); the login flow has no limit field. Golem's per-task cap still applies. `--headless` prints a code to paste. The Docker wrapper uses that mode.

`verify` re-runs every installed tool's tests in the sandbox and checks the files against the receipt. It does not need an API key. `--attach` supplies files the tests read under `/inputs`. A matching install prints `OK`. An edited file fails with `CHANGED since they were tested`.

A run writes its answer to `.golem/runs/<run_id>/result.md`.

## How a tool is made

A session starts with four kernel tools: `list_files`, `read_file`, `make_tool`, and `install_tool`. Every other tool the agent calls, it built.

1. **Proposal.** `make_tool` requires a `task_quote` copied from the task. After whitespace is collapsed and case is folded, the quote must be at least 8 characters. Attempts are counted on that quote. A new name does not reset the count. An installed name is refused unless the proposal revises it.
2. **Licence.** A request for access, an import, a subprocess, an environment read, or a write that the licence does not grant is refused. The lint names the request. The sandbox enforces it.
3. **Advisor.** `typesafe/jev-1.13`, called through OpenRouter's Decisions API, may send a proposal back once. It does not approve an install. If the advisor is unavailable, the build continues.
4. **Blind tests.** A tester model writes `test_blind.py` from the interface, the task, and the repository. It does not see the implementation. On failure, the builder receives the test name and the exception type.
5. **Sandbox.** Golem runs the builder's tests, the blind tests, and the same tests against two stubs that must fail them. The builder's suite must contain at least 3 tests, and at least 80% of the tests must fail on both stubs.
6. **Install.** `install_tool` checks that the files still hash to what was tested, writes `.golem/registry/NAME/VERSION/`, and points `.golem/registry/active.json` at that version. The first version of a name is `0.1.0`. Each later version bumps the minor number.
7. **Next session.** A new session starts only after an install, while sessions and budget remain. It loads installed tools from disk and calls them in the sandbox.

The sandbox is:

```text
docker run --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534
```

Memory, CPU, process, and time limits come from the licence. The host environment is not forwarded. The only writable path is a `/tmp` tmpfs. Repository-read tools also see a read-only snapshot at `/repo`. Registry-read tools also see the registry at `/registry`.

## Tool contract

A tool is a standard-library Python function, `run(args: dict) -> dict`, with JSON schemas for its input and output. It runs once per call and keeps no state. Output is a typed result or a unified diff. A tool does not apply a patch, and it does not receive network access, credentials, or a shell.

| Access | What it can read |
| --- | --- |
| Pure | Its arguments, and files attached to the task at `/inputs` |
| Repository-read | Also a read-only snapshot of the repository at `/repo` |
| Registry-read | Also the registry at `/registry`: manifests, receipts, and usage. No tool code. |

The repository snapshot omits `.git`, `.golem`, virtualenvs, dependency and build directories, secret filenames, symlinks, and files larger than 1 MB. `.env.example`, `.env.sample`, `.env.template`, and `.env.dist` are kept.

## Limits

From `authority.json`, enforced by the kernel:

| Cap | Value |
| --- | --- |
| Spend per task | $0.20 |
| Sessions per task | 3 |
| Steps per session | 18 |
| `make_tool` calls per task | 4 |
| Attempts per gap | 3 |
| Sandbox runs per task | 60 |
| One sandbox run | 120 s, 512 MB, 1 CPU, 128 processes |

The spend cap is checked between model steps. Models are set in the same file. The builder and the tester are both `deepseek/deepseek-v4.1-flash`, so receipts mark `independent_tester` false. The advisor is `typesafe/jev-1.13`.

## GitHub Actions

[`.github/workflows/golem.yml`](.github/workflows/golem.yml) runs a task against a public repository. Add the repository secret `OPENROUTER_API_KEY`, then:

```bash
gh workflow run golem.yml -f target=OWNER/REPO -f ref=SHA -f task="..."
```

An optional `task2` runs as a new process on the same registry. A second job holds no secret and re-runs every installed tool's tests. Only accounts with write access can start the workflow.

## Development

```bash
uv sync --no-editable
python -m unittest discover -s tests
```

[`.github/workflows/tests.yml`](.github/workflows/tests.yml) runs on Python 3.13: it checks `golem licence` against the sha256 of `authority.json`, then runs ruff, pyright, and pytest. Sandbox tests need Docker and `python:3.12-slim`.

Recorded runs, including failed attempts, are in [`evidence/`](evidence/).
