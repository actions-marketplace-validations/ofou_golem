# GitHub Actions for Golem

Implementation note for the unwired "secret-free Actions" line in `README.md`. This repository has no workflow, no Actions secrets, no environments, and no deployments. `github.com/ofou/golem` is empty. The first commit that adds `.github/workflows/golem.yml` is the start of this path.

The README never defines "secret-free Actions." The reading used here is the one the surrounding sentences support: the run that tests a candidate has no stored credentials, and the generated tool never receives a token. The fixed adapter is the only writer. There is no human approval and no deployment.

## What the workflow does

One `workflow_dispatch` is one task about this repository. The task text is the `task` input (string, required, at most 65,535 characters). The workflow file has to be on the default branch before the first dispatch. This empty repository has no default branch until that first push.

Two jobs, in order:

1. **test.** Check out the workflow SHA. Load the registry in a fresh process and run the credential-free test, including the licence check. The test decides. Jev does not. Failure ends the run. Nothing is installed.
2. **write.** Runs only after `test` succeeds. This job is the fixed adapter. It commits the exact tool version that passed, and it commits a patch when the task result is a patch. A typed result that is not a patch produces no commit.

Generated tool code runs only in `test`. The adapter runs only in `write`.

These stay outside this workflow: `openai-agents`, `typesafe/jev-1.13`, OpenRouter, OAuth PKCE, and PostgreSQL. OAuth PKCE provisions an OpenRouter key on an existing balance. It is not Actions authentication. Putting that key, a database password, or a GitHub App private key into an Actions secret means the workflow is no longer secret-free.

## The credential

Each job receives a `GITHUB_TOKEN`: an installation token for the GitHub Actions app on this repository. It is not stored as an Actions secret. It expires when the job finishes (at most 6 hours on a GitHub-hosted runner). It can act only on this repository.

Use it as `secrets.GITHUB_TOKEN` or `github.token`. Pass it only into the adapter step, as `GH_TOKEN`. A `run` step does not receive `$GITHUB_TOKEN` unless the step sets it.

A custom GitHub App cannot be the credential for this workflow. Minting an installation token requires the app's PEM private key, stored as an Actions secret (`actions/create-github-app-token`). OIDC (`id-token: write`) gets a token for a cloud provider. It does not replace that private key. This repository's writes are to itself, which `GITHUB_TOKEN` can do.

Token limits that match the spec:

- A push made with `GITHUB_TOKEN` does not start `push` workflows. The adapter's commit does not rebuild the registry and does not wait for a person.
- `workflow_dispatch` and `repository_dispatch` do start when `GITHUB_TOKEN` sends them. Do not dispatch either from the adapter.
- On a fork `pull_request`, `GITHUB_TOKEN` is read-only and other secrets are withheld. This workflow is not a fork pull request workflow.



## Permissions

Set permissions on each job. A job `permissions` block sets every omitted scope to `none` (`metadata` stays `read`). The repository default for a new personal account is read on `contents` and `packages`. The write job's block overrides that for that job only.


| Job     | Scope             | Why                                                                                    |
| ------- | ----------------- | -------------------------------------------------------------------------------------- |
| `test`  | `contents: read`  | Checkout and read. No write, no pull requests, no checks, no deployments, no id-token. |
| `write` | `contents: write` | The adapter's `git push`. `write` includes `read`.                                     |


`contents: write` is repository contents. It is not a GitHub Deployment. Do not set `deployments`. Do not set `jobs.*.environment`. An environment can require a reviewer, and the job is not sent to a runner until that person approves. That is the human approval the README excludes.

Do not set `pull-requests: write`. Opening a pull request also needs the repository setting "Allow GitHub Actions to create and approve pull requests," which is off on a new personal repository. A pull request that `GITHUB_TOKEN` opens then waits for a user with write access to approve its `opened` / `synchronize` / `reopened` workflows. The adapter pushes instead, so that gate never applies.

Do not use `pull_request_target`. It runs default-branch code with a write token and secrets, including on a public fork.

## Workflow

Ubuntu GitHub-hosted runners already have `/usr/bin/python3`. `actions/setup-python@v5` pins CPython from the runner tool cache or a public build. The test has no pip install and no PyPI token. `python -m unittest` below is the shape of a standard-library command. This repository has no test entry point yet. Replace that line with the credential-free test when it exists. The licence check runs in the same step. It does not get its own workflow.

`actions/checkout@v6` defaults `token` to `github.token` and `persist-credentials` to `true`. The test job turns persistence off so later `git` commands in that job have no credential. The write job leaves the default so `git push` authenticates. Checkout stores that credential under `$RUNNER_TEMP` and removes it after the job.

```yaml
name: golem

on:
  workflow_dispatch:
    inputs:
      task:
        description: Task about this repository
        required: true
        type: string

jobs:
  test:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v6
        with:
          persist-credentials: false
      - uses: actions/setup-python@v5
        with:
          python-version: "3.x"
      - name: Credential-free test
        run: python -m unittest

  write:
    needs: test
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout
      - name: Fixed adapter
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: echo "adapter commits the tested version and any patch"
```

Commit as `github-actions[bot]` / `41898282+github-actions[bot]@users.noreply.github.com`.

Both jobs check out `github.sha`. The test job does not push. The write job is skipped when the test fails.

## Keeping the tool credential-free

The runner is a shell with a filesystem and a network. `contents: read` does not make it a sandbox. The README's current licence check is an AST scan, not a sandbox. The workflow keeps the GitHub credential out of the tool. It does not remove the shell.

The test job stays credential-free when all of these hold:

- The tool step is a `run` step. It does not set `GH_TOKEN`, `GITHUB_TOKEN`, or `secrets.GITHUB_TOKEN`.
- Checkout uses `persist-credentials: false`.
- The job's only permission is `contents: read`.
- No JavaScript or composite action in that job reads `github.token`. An action can read `github.token` even when the workflow never passes it in.
- The write job does not execute generated code. Its `contents: write` token and its persisted git credential belong to the adapter.
- The workflow does not check out another repository. That would need a personal access token, which is a stored secret.
- Repository settings stay as they are on this empty repo: zero Actions secrets, zero variables, zero environments, zero deployments. "Send write tokens to workflows from pull requests" and "Send secrets to workflows from pull requests" stay off.



## Sources

- [GITHUB_TOKEN](https://docs.github.com/en/actions/concepts/security/github_token)
- [Controlling permissions for GITHUB_TOKEN](https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/controlling-permissions-for-github_token)
- [Workflow syntax: permissions](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#permissions)
- [Workflow syntax: environment](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idenvironment)
- [Events that trigger workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
- [GitHub App requests from Actions](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/making-authenticated-api-requests-with-a-github-app-in-a-github-actions-workflow)
- [Generating a JWT for a GitHub App](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app)
- [OpenID Connect](https://docs.github.com/en/actions/concepts/security/openid-connect)
- [actions/checkout@v6](https://github.com/actions/checkout/blob/v6.0.0/README.md)
- [Building and testing Python](https://docs.github.com/en/actions/tutorials/build-and-test-code/python)
- [Managing GitHub Actions settings for a repository](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository)

