# Deploy the agent

The Git repository contains the agent code and policies. Run it as a GitHub App receiving webhooks or as a process that polls with a GitHub token. A container image is optional.

For a cluster deployment, see [OpenShift and Kubernetes deployment](openshift.md).

## Run with a GitHub token

Create repository configs and validate them with `uv run auto-merge-agent validate-config`. The token's repository access and the enabled configs together determine which repositories are scanned. For a fine-grained personal access token, grant **Pull requests: read and write**, **Contents: read and write** for merging, **Checks: read**, **Commit statuses: read and write**, and **Issues: read and write** for comments. Shadow mode only reads. A classic token needs access to those repositories and suitable `repo` scope. [GitHub documents commit status permissions](https://docs.github.com/en/rest/commits/statuses) and [check-run writes are limited to GitHub Apps](https://docs.github.com/en/rest/checks/runs), so token mode publishes an `automerge/policy` commit status instead of a check run.

```bash
cd /path/to/auto-merge-agent
uv sync --locked
export AUTOMERGE_CONFIG_ROOT="$PWD"
uv run auto-merge-agent poll --interval 900
```

Sign in with `gh auth login` first, or set `GITHUB_TOKEN` to override the local `gh` token. The command scans all enabled configs immediately and every 900 seconds. `--once` performs one scan and exits. It logs each decision; the default `AUTOMERGE_ACTIVE=false` makes no GitHub writes. Set `AUTOMERGE_ACTIVE=true` after shadow decisions match the policy. Active mode writes statuses and comments and may merge any eligible open PR in an enabled repository. A failed PR or repository is retried on the next scan. Run one polling process to avoid duplicate evaluations and merge attempts. No webhook URL, App ID, or App private key is needed.

Keep `automerge/policy` informational in branch rules. A pending or failed token status can block the normal human merge path if made required. The token's user identity performs the merge, so GitHub's branch protection and review rules apply to that user. Use a dedicated account with the narrowest repository access that meets your policy.

## Run as a GitHub App

GitHub sends events to a running process; it does not execute this repository for you. Run one service process behind a public HTTPS webhook endpoint. Its queue is in memory; startup and periodic scans recover open PRs after downtime.

## Register and install the App

1. [Register a GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app) under the account that will own it. A private App can be installed only on its owner account; choose broader visibility only if other accounts need to install it.
2. Set a Homepage URL, an active Webhook URL ending in `/webhook`, and a webhook secret. Keep SSL verification enabled. The service verifies `X-Hub-Signature-256` before processing a delivery.
3. Grant the repository permissions below. GitHub shows available webhook subscriptions after permissions are selected.
4. Subscribe to the events listed below, generate a private key under the App's credentials, and [install the App](https://docs.github.com/en/apps/using-github-apps/installing-your-own-github-app) on **only selected repositories**.

| Repository permission | Level | Used for |
| --- | --- | --- |
| Metadata | Read | Repository identity; included with GitHub Apps. |
| Pull requests | Read and write | PR data, changed files, reviews, and merge operations. |
| Contents | Read and write | GitHub's PR merge endpoint requires write access. |
| Checks | Read and write | Read CI check runs and publish `automerge/policy`. |
| Commit statuses | Read | Read status-based CI and reviewer contexts. |
| Issues | Read and write | Read and update PR timeline comments. |

Subscribe to `pull_request`, `pull_request_review`, `pull_request_review_comment`, `issue_comment`, `check_run`, `check_suite`, and `status`. The service also reconciles open PRs every 15 minutes, so a missed event does not permanently prevent evaluation. See [GitHub's permissions guide](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app) and [webhook event reference](https://docs.github.com/en/webhooks/webhook-events-and-payloads).

## Configure the process

| Environment variable | Required | Meaning |
| --- | --- | --- |
| `GITHUB_APP_ID` | Yes | Numeric App ID from its GitHub settings. |
| `GITHUB_APP_PRIVATE_KEY_FILE` or `GITHUB_APP_PRIVATE_KEY` | Yes | PEM private key path or contents. Keep outside Git. |
| `GITHUB_WEBHOOK_SECRET` | Yes for webhooks | Must match the App's webhook secret. |
| `AUTOMERGE_CONFIG_ROOT` | Recommended | Protected checkout containing `config/`, `configs/`, and `profiles/`; defaults to the working directory. |
| `AUTOMERGE_ACTIVE` | No | `false` by default. Set `true` to publish checks/comments and merge eligible PRs. |
| `OPENAI_API_KEY` | For OpenAI LLM rules | OpenAI Platform project API key. Static-only rules do not use it. |
| `MLFLOW_TRACKING_URI` | No | MLflow server for LangGraph traces. |
| `MLFLOW_EXPERIMENT` | No | MLflow experiment name; defaults to `auto-merge-agent`. |

The config checkout is the repository allowlist. After signature verification, the webhook queues a PR only when `configs/github.com/<owner>/<repo>/config.yaml` exists, validates, and has `enabled: true`. Scheduled reconciliation scans those same repo config directories and skips disabled policies. GitHub App installation permissions further limit which repositories the service can access.

The webhook service refuses to start if `GITHUB_TOKEN` is set; it must authenticate as the App installation. The `poll` command uses `GITHUB_TOKEN` or the local `gh` login. The `evaluate` command also accepts `GITHUB_TOKEN`. Provide credentials through your deployment's secret store and never commit them.

The service learns its bot login from the first comment it creates or identifies by App ID and shares it across webhook handling and later comment updates. Before then, App ID establishes ownership; a comment marker from an unrelated bot is not proof of ownership.

```bash
cd /path/to/auto-merge-agent
uv sync --locked
unset GITHUB_TOKEN
export GITHUB_APP_ID=APP_ID
export GITHUB_APP_PRIVATE_KEY_FILE=/path/to/app-private-key.pem
export GITHUB_WEBHOOK_SECRET=WEBHOOK_SECRET
export AUTOMERGE_CONFIG_ROOT="$PWD"
uv run auto-merge-agent serve --host 0.0.0.0 --port 8000
```

Put a reverse proxy or tunnel with HTTPS in front of port 8000 and configure the App's Webhook URL to its public `/webhook` route. `/health` reports whether the process is in active mode. Run one process: multiple workers would have separate in-memory queues. Pending events are lost on restart, but the startup scan re-enqueues open PRs from enabled configs. Restart after deploying code or global model-allowlist changes; use the protected config revision rather than PR branches.

For local development, GitHub cannot call `localhost`. Its [webhook testing guide](https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks/testing-webhooks) describes a forwarding proxy. For example, set the App's Webhook URL to a Smee channel, start the agent on `127.0.0.1:8000`, and forward the channel to `http://127.0.0.1:8000/webhook` with `npx smee -u CHANNEL_URL -t http://127.0.0.1:8000/webhook`.

## Roll out safely

1. Add a repo config with exact expected CI check names and an `OWNERS` file. Keep `enabled: false` until the target repo has its CI and reviewer integration.
2. Validate the agent checkout with `uv run auto-merge-agent validate-config`. Set `enabled: true`, keep `AUTOMERGE_ACTIVE=false`, and inspect decisions with the read-only `evaluate OWNER REPO PR_NUMBER` CLI. The shadow service makes no PR writes.
3. Verify webhook deliveries in the App's **Advanced → Recent deliveries** page. Use the CLI to inspect decisions; check service logs for errors or MLflow traces when configured.
4. Enable `AUTOMERGE_ACTIVE=true` for an intentionally scoped installation. The service can merge **any** open PR in an enabled repo that meets policy, including existing PRs. It posts an informational policy check and a checklist comment around each merge attempt.

If the target branch requires human approval, GitHub will reject the App's merge unless its rules grant an appropriate bypass. Put review bypass and required CI in separate rulesets so the App still has to satisfy GitHub's CI protections. The bot's `automerge/policy` check should stay informational, allowing the normal human-review path. See [GitHub rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets).

MLflow tracing is optional. When enabled, traces can include PR descriptions and diffs, so grant MLflow access accordingly. [MLflow documents automatic LangGraph tracing](https://mlflow.org/docs/latest/genai/tracing/integrations/listing/langgraph).
