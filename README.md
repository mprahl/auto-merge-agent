# Auto Merge Agent

**Automatic merges for low-risk PRs, controlled by each repository's policy.**

This Python agent combines static rules, CI, a current-head CodeRabbit review, and optional LLM judgments. Run it as a GitHub App with webhooks or as a polling process with a GitHub token. It submits the evaluated head SHA with each merge request, so a later push prevents that merge.

> **Start in shadow mode.** The agent evaluates PRs and logs decisions without posting checks, statuses, comments, or merges. Turn on writes after the decisions match your policy.

## Quick start: run locally with a GitHub token

This path needs no GitHub App, webhook, public URL, or server. The `poll` command reads policies from this checkout and scans open PRs in enabled repositories. It runs in the foreground; keep the terminal open for continuous polling, or use `--once` for a single scan.

### 1. Install and sign in

Install Python 3.11+, [uv](https://docs.astral.sh/uv/), and the [GitHub CLI](https://cli.github.com/). From this repository's root:

```bash
uv sync
gh auth login
gh auth status
```

If `gh` is already signed in, skip `gh auth login`. The poll command uses `GITHUB_TOKEN` when set; otherwise it calls `gh auth token`. The token must be able to read the target repository. For active mode, use an account that can merge there. A fine-grained token needs access to the selected repositories plus **Pull requests: read and write**, **Contents: read and write**, **Checks: read**, **Commit statuses: read and write**, and **Issues: read and write**. A classic `gh` token needs suitable `repo` scope. See [token permissions and branch rules](docs/deployment.md#run-with-a-github-token) for details. Never put a token in a policy file.

### 2. Add one repository policy

Choose a repository with an open PR, GitHub Actions CI, and CodeRabbit review enabled. Find the exact CI job name on the PR's **Checks** tab. Substitute its owner, repo, trusted PR author login, and CI job name in the commands below. The trusted author is the PR's author, not necessarily the person running the agent.

```bash
export TARGET_OWNER=acme
export TARGET_REPO=widget
export TRUSTED_AUTHOR=maintainer
export CI_CHECK=tests
mkdir -p "configs/github.com/$TARGET_OWNER/$TARGET_REPO"
cat > "configs/github.com/$TARGET_OWNER/$TARGET_REPO/OWNERS" <<EOF
approvers: [$TRUSTED_AUTHOR]
EOF
cat > "configs/github.com/$TARGET_OWNER/$TARGET_REPO/config.yaml" <<EOF
schema_version: 1
enabled: true
ci:
  expected:
    - name: "$CI_CHECK"
      source: github-actions
rules:
  - name: docs-only
    type: changed_paths_only
    paths: [README.md, "docs/**"]
EOF
```

This starting policy considers only PRs authored by `TRUSTED_AUTHOR` that change `README.md` or files under `docs/`. It still requires passing CI and a completed CodeRabbit assessment of the current PR head. Other PRs are evaluated but do not qualify for automerge. The static rule does not need an OpenAI API key. If your CI is a commit status rather than a GitHub Actions check run, remove `source: github-actions` or set it to that status creator's login. See [configuration](docs/configuration.md) for more rules and limits.

### 3. Validate and inspect decisions

```bash
uv run auto-merge-agent validate-config
uv run auto-merge-agent poll --once
```

Run these commands from this repository's root. Validation checks the policy files. The one-time scan lists open PRs and logs a decision for each one. The default `AUTOMERGE_ACTIVE=false` means it makes no GitHub changes. For a full JSON decision on one PR, run:

```bash
GITHUB_TOKEN="$(gh auth token)" uv run auto-merge-agent evaluate "$TARGET_OWNER" "$TARGET_REPO" 123
```

Replace `123` with the PR number. If `GITHUB_TOKEN` is already set, `uv run auto-merge-agent evaluate ...` uses it directly. A `waiting` result often means CI or the current-head CodeRabbit review has not arrived. A `human_review` result means a gate or rule did not pass; read its `reasons` before changing the policy.

### 4. Poll continuously, then enable writes

```bash
uv run auto-merge-agent poll --interval 900
```

The first scan starts immediately; later scans start after each 900-second interval. Stop with Ctrl+C. The command retries failed repositories and PRs on the next scan. Keep it in shadow mode until its logged decisions match what you expect.

When ready, start one active process:

```bash
AUTOMERGE_ACTIVE=true uv run auto-merge-agent poll --interval 900
```

Active mode can merge **any eligible open PR** in every enabled repository config in this checkout, including PRs opened before the process started. It posts an `automerge/policy` commit status, may comment, and requests a merge guarded by the evaluated head SHA. Keep that status informational in branch rules; if it is required, its pending or failed state can block the normal human merge path. [GitHub allows token users to write commit statuses, while check-run writes are reserved for Apps](https://docs.github.com/en/rest/checks/runs). Start only one poll process for a given config checkout.

For another config location, put `--config-root /path/to/policies` before `poll`. See [deployment](docs/deployment.md#run-with-a-github-token) for token permissions, active mode, and branch protection details.

### If the first scan does not behave as expected

| Symptom | Check |
| --- | --- |
| No repositories or PR decisions appear | Run from this checkout's root, check the `configs/github.com/<owner>/<repo>/config.yaml` path and `enabled: true`, and confirm the repository has open PRs. |
| GitHub returns 401 or 403 | Check `gh auth status` or the `GITHUB_TOKEN` override, repository access, and [token permissions](docs/deployment.md#run-with-a-github-token). |
| Decision waits for CI | Match `ci.expected.name` to the exact check name. Use `source: github-actions` for GitHub Actions check runs; remove it when unsure which source is reported. |
| Decision waits for review | Confirm CodeRabbit finished reviewing the PR's latest head commit. A new push makes the earlier review stale. |
| Eligible PR does not merge in active mode | Read the logged error and check the token user's merge access and the repository's branch rules. GitHub can reject a merge even after the agent's policy passes. |

## How a PR qualifies

The agent checks every PR in this order:

1. **Mandatory gates:** The PR is open, ready, mergeable, and its author is allowed by a matching rule (using the repository-wide trusted-author list by default). Its file list is complete, its size is within the repo limit, and no blocking label or human change request is present. Configured CI checks pass, at least one real CI check succeeds, and CodeRabbit has reviewed the latest head SHA.
2. **One eligibility rule:** Top-level rules are alternatives. A static rule can allow a Go dependency bump without an LLM. An `all` group can require a path rule, a complexity rule, and a custom safety rule together. If a static rule passes, the LLM is skipped.
3. **Final recheck:** Before merging, the agent reruns the policy and confirms the head and base have not changed. Missing evidence, an unrecognized review, or a model error prevents an automatic merge.

In active mode, the App shows its decision in an `automerge/policy` check. Token polling uses a commit status with the same context:

| Result | App check / token status | What happens |
| --- | --- | --- |
| Waiting for evidence | In progress / pending | Reevaluate when CI or review arrives. |
| Eligible | Success / success | Post a checklist of passed gates and attempt the SHA-guarded merge. |
| Human review needed | Action required / failure | Explain why; optionally mention configured maintainers. |

Keep `automerge/policy` informational so PRs needing human review can still use the repository's normal path.

## Try a read-only evaluation

You need Python 3.11+, [uv](https://docs.astral.sh/uv/), a repo policy in this checkout, and a GitHub token with read access. If you use the GitHub CLI:

```bash
uv sync --extra test
uv run auto-merge-agent validate-config
uv run pytest -q
GITHUB_TOKEN="$(gh auth token)" uv run auto-merge-agent evaluate OWNER REPO PR_NUMBER
```

The CLI prints the decision as JSON. `--execute` enables writes and can merge the PR.

## Configure a repository

Policies live in this agent's checkout:

```text
config/global.yaml                         # Defaults and allowed model IDs
profiles/<profile>.yaml                    # Reusable eligibility rules
configs/github.com/<owner>/config.yaml     # Optional owner defaults
configs/github.com/<owner>/<repo>/config.yaml
configs/github.com/<owner>/<repo>/OWNERS   # Default trusted PR authors
configs/github.com/<owner>/<repo>/context.md # Optional LLM guidance
OWNERS                                     # Ownership of this agent repo
```

Repo settings override owner settings; nested maps merge and lists replace inherited lists. The service evaluates only repositories with an enabled, valid config.

LLM rules automatically include the policy directory's `context.md` and the target repo's root `AGENTS.md` (or `CLAUDE.md` if `AGENTS.md` is absent). Set `llm_context_files: [docs/merge-policy.md]` in `config.yaml` for additional target-repo files. Target-repo context comes from the PR base commit. See [configuration](docs/configuration.md#eligibility-rules) for details.

### Example: dependency bumps and small fixes

**1. Allow a model** in `config/global.yaml`:

```yaml
schema_version: 1
allowed_models: [gpt-6-sol]
defaults: {}
```

**2. Configure the repository.** For `github.com/acme/widget`, put `approvers: [maintainer]` in `configs/github.com/acme/widget/OWNERS`, then create `configs/github.com/acme/widget/config.yaml`:

```yaml
schema_version: 1
enabled: true
ci:
  expected:
    - name: tests
      source: github-actions
rules:
  - profile: go-dependencies
  - name: small-maintenance
    all:
      - name: maintenance-paths
        type: changed_paths_only
        paths: ["*.go", go.mod, go.sum, README.md, "docs/**"]
      - name: maintenance-complexity
        type: llm_complexity
        model: gpt-6-sol
        max: low
        max_changed_lines: 100
      - name: maintenance-safety
        type: llm_custom
        model: gpt-6-sol
        prompt: >-
          Require human review for public API changes, new services, or
          unclear security effects. If uncertain, return false.
```

Replace `maintainer` and `tests` with the repository's GitHub login and CI check. The `go-dependencies` profile expands to a static rule for modified `go.mod` and `go.sum` files. Both alternatives require a trusted author, passing CI, and a current-head CodeRabbit review. If CodeRabbit also posts a status, that status must pass. A PR changing only `go.mod` and `go.sum` uses the static rule and skips the LLM, even though `small-maintenance` also matches. Other matching changes must pass both LLM criteria.

See [configuration](docs/configuration.md) for inheritance, every policy field, and decision semantics. Its [policy recipes](docs/configuration.md#policy-recipes) show Python dependency updates from Dependabot, small changes outside an auth package, and low-complexity changes that preserve the public API. The checked-in policy under `configs/` is another example.

JSON Schemas for [global](schemas/global.schema.json) and [org/repo](schemas/repository.schema.json) YAML provide editor completion. `validate-config` checks cross-file requirements such as model allowlisting and expected CI checks. Regenerate schemas after changing config models with `uv run python scripts/generate_schemas.py`.

## Run the GitHub App

1. Register a GitHub App and install it on selected repositories.
2. Configure its credentials and webhook secret, then run one service process.
3. Inspect decisions with `AUTOMERGE_ACTIVE=false` (the default). Set it to `true` when ready to let the App post checks, comment, and merge.

The service accepts webhooks at `/webhook`, exposes `/health`, and scans enabled repos at startup and every 15 minutes to recover work missed during downtime.

See [deployment](docs/deployment.md) for permissions, events, environment variables, local webhook forwarding, and rollout steps. See [architecture](docs/architecture.md) for the evaluation flow, extension points, and MLflow tracing.

For OpenShift, use the [container and Kustomize deployment guide](docs/openshift.md).
CI validates both overlays and runs deployment lifecycle tests in kind without real GitHub or Quay credentials.

## Project scope

The current implementation supports GitHub and CodeRabbit. GitLab and additional reviewer integrations are extension points. The webhook service uses one worker and an in-memory queue with retries; pending events do not survive a restart. Polling retries failed PRs on the next scan. GitHub branch rules still decide which merges are permitted.
