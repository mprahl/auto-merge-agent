# Deploy on OpenShift

The deployment runs the GitHub App webhook service, not token-based polling.
It uses one replica and a `Recreate` update strategy because queues and duplicate
suppression are process-local. Updates briefly interrupt webhook delivery; the
startup and 15-minute scans recover open PRs. Do not add replicas or additional
Uvicorn workers. `Recreate` avoids overlap during normal deployment updates; it
does not provide distributed fencing during node failures or manual pod deletion.

## Build and publish an image

The `Containerfile` installs dependencies from `uv.lock` in a build stage. The
runtime contains the installed application and dependencies, with no policy
checkout, credentials, Git CLI, or runtime dependency installation. It supports
an arbitrary non-root UID with a read-only root filesystem and writable `/tmp`.

The production repository is `quay.io/opendatahub/auto-merge-agent`. Build and
push with Podman (Docker works too):

```bash
export AGENT_IMAGE=quay.io/opendatahub/auto-merge-agent:sha-$(git rev-parse HEAD)
podman build -f Containerfile -t "$AGENT_IMAGE" .
podman login quay.io
podman push "$AGENT_IMAGE"
```

The **Publish image** workflow uses GitHub OIDC federation, not a stored Quay
password or a GitHub Actions secret. Before its first use, an `opendatahub` Quay
organization administrator must create the robot account
`auto-merge-agent-publisher`, grant it **Write** permission only on
`opendatahub/auto-merge-agent`, and configure its federation entry as follows:

| Field | Value |
| --- | --- |
| Robot username | `opendatahub+auto-merge-agent-publisher` |
| Issuer | `https://token.actions.githubusercontent.com` |
| Audience | `quay` |
| Subject | `repo:opendatahub-io@57720972/auto-merge-agent@1412421827:ref:refs/heads/main` |

Use the immutable OIDC subject shown in this repository's **Settings → Actions →
OIDC** page as the source of truth; the value above is the expected immutable
subject for this repository. Restricting it to `main` means pull-request runs and
other branches cannot obtain a Quay push token. The workflow requests the `quay`
audience and exchanges the GitHub OIDC token for Quay's short-lived robot token
only while publishing.

When a push to `main` changes a runtime image input (`Containerfile`,
`.dockerignore`, `pyproject.toml`, `uv.lock`, or `src/`), **Publish image** runs
the entire CI workflow and then pushes the exact tested linux/amd64 image as
`sha-<full-commit-sha>`. It does not publish from PRs or deploy to a cluster.
You can also run it manually on `main` to deliberately republish a commit.
Ordinary CI needs no Quay credentials. For other CPU architectures, build and
test separately. Prefer a digest reference for deployments; tags can be
overwritten in a registry.

## Prepare policies and Kustomize artifacts

Install Python 3.11+, uv, and Kustomize (CI uses 5.6.0). Add and review policies
using the [configuration guide](configuration.md), then validate:

```bash
uv sync --locked
uv run auto-merge-agent validate-config
uv run python scripts/prepare_deployment.py \
  --repository YOUR_ORG/YOUR_REPOSITORY \
  --image "$AGENT_IMAGE" \
  --output build/deploy
kustomize build build/deploy/overlays/openshift > build/openshift.yaml
```

Repeat `--repository owner/repo` for each repository to include. The helper requires
explicit selection: it does not automatically deploy the checked-in, enabled
`mprahl/test-automerge` example. It preserves the selected repositories' `enabled`
settings. With `--policy-root /path/to/protected/checkout`, policies can come from
a separate checkout. Use a reviewed, protected revision, not the PR being evaluated.

The helper snapshots root `OWNERS`, `config/global.yaml`, all profiles, selected
owner defaults, selected repository policies/OWNERS, and optional `context.md`.
It validates that snapshot and rejects symlinks, missing files, and bundles over
200 KB (the client-side apply annotation limit). It copies the manifests and
snapshot into a self-contained directory,
so normal Kustomize load restrictions work. It refuses to overwrite existing
output; use a new directory for each revision. No policy YAML is duplicated in
the maintained deployment sources.

ConfigMap keys are mapped back to the original directory structure under
`/etc/auto-merge-agent`. Keep Kustomize's generated name hash enabled: changes
update the pod template and restart the service, refreshing the global model
allowlist. Both containers use the same policy mount and image. The init container
runs `validate-config` before the webhook service can start.

## Provision credentials

Register/install the App using [the deployment guide](deployment.md#register-and-install-the-app).
Create the namespace, then provide the required Secret through your secret manager
or a local file that stays outside Git:

```bash
oc apply -f build/deploy/overlays/openshift/namespace.yaml
# /secure/github-app.env contains app-id=... and webhook-secret=... lines.
oc -n auto-merge-agent create secret generic auto-merge-agent-github \
  --from-env-file=/secure/github-app.env \
  --from-file=private-key.pem=/secure/app-private-key.pem
```

The exact required keys are `app-id`, `webhook-secret`, and `private-key.pem`.
Only the private key is mounted as a file; the other values use explicit Secret
environment references. The validation init container receives no credentials.
The key file is read-only and readable by the arbitrary container UID; its volume
is accessible only to the agent container. Do not supply `GITHUB_TOKEN` to this
deployment: the webhook service rejects token authentication.

For OpenAI-backed rules, optionally create `auto-merge-agent-openai` with key
`api-key`, for example `--from-file=api-key=/secure/openai-api-key`. Static-only
policies require no OpenAI Secret. For a private Quay repository, create a registry
pull Secret and link it to the service account after creating it:

```bash
oc -n auto-merge-agent apply -f build/deploy/base/serviceaccount.yaml
oc -n auto-merge-agent create secret generic quay-pull \
  --type=kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson=/secure/quay-auth.json
oc -n auto-merge-agent secrets link auto-merge-agent quay-pull --for=pull
```

No Kubernetes API permissions are needed. Service-account token mounting is
disabled. The base does not request a fixed UID or privileged SCC; OpenShift can
assign its normal namespace UID. Default resources are starting values, not
capacity guarantees; tune them against actual policy/model workloads.

## Deploy and verify shadow mode

```bash
oc apply -k build/deploy/overlays/openshift
oc -n auto-merge-agent rollout status deployment/auto-merge-agent
oc -n auto-merge-agent logs deployment/auto-merge-agent -c validate-config
oc -n auto-merge-agent logs deployment/auto-merge-agent -c agent
oc -n auto-merge-agent get route auto-merge-agent -o jsonpath='{.spec.host}'
```

Use `https://<route-host>/webhook` as the GitHub App's webhook URL. The Route uses
the cluster's default ingress certificate, redirects HTTP to HTTPS, and exposes
the `/webhook` path. Supply a custom host/certificate through your deployment
overlay if the cluster default is not publicly trusted or reachable by GitHub.
TLS terminates at the router; traffic from router to pod is HTTP. Clusters requiring
encrypted backend connections need additional server-side TLS support.

The service needs outbound access to GitHub and, when configured, the model
provider and MLflow server. There is no persistent volume or database requirement.
An optional MLflow integration uses `MLFLOW_TRACKING_URI` and `MLFLOW_EXPERIMENT`
environment entries on the agent container; supply any credentials through Secrets.

The startup/readiness/liveness probes all use `/health`. They establish HTTP process
availability, not GitHub authentication, worker progress, or downstream health.
Check the App's recent deliveries and the service's decision/error logs. `/health`
is available inside the cluster, or through `oc port-forward service/auto-merge-agent
8000:8000`; it is not deliberately exposed by the public Route.

Shadow mode is the checked-in default (`AUTOMERGE_ACTIVE=false`). Once decisions
match the intended policies, add this strategic-merge patch through `patches` in
your deployment overlay's `kustomization.yaml` and reapply:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: auto-merge-agent
spec:
  template:
    spec:
      containers:
        - name: agent
          env:
            - name: AUTOMERGE_ACTIVE
              value: "true"
```

Save it as `active.yaml` beside the overlay's `kustomization.yaml`, then append
`patches: [{path: active.yaml}]` to that file (extend existing patches if present).
Keep environment-specific customizations in your protected deployment repository
or reproducible automation; preparation does not preserve edits to earlier output.
Activation may merge existing eligible PRs in every enabled selected repository.

## Updates and rollback

Prepare a new output directory from the new reviewed policy revision and image
digest, reapply environment-specific patches, render/review it, and apply it.
Keep the previous rendered manifest for rollback. To restore it, apply the saved
manifest and wait for the rollout. Rollback cannot undo merges already performed.

Old hashed ConfigMaps are not automatically deleted by `oc apply`; retain those
needed by rollback revisions, then remove unreferenced ones deliberately or use
your GitOps tool's retention/pruning policy. Never delete a ConfigMap referenced
by a running pod or a retained rollback manifest.

After rotating credentials, restart the deployment with
`oc -n auto-merge-agent rollout restart deployment/auto-merge-agent`.
Environment values and the App private key are captured at startup, so updating
a Secret alone is insufficient. The optional OpenAI key also needs a restart.

## CI and local kind testing

Every push and pull request runs:

| Check | Coverage |
| --- | --- |
| Python 3.12 | Existing application tests, deployment helper tests, schema generation, policy validation, rule ordering |
| Manifests | Both overlays render; native resources and the OpenShift Route schema validate; policy mount paths and hash-triggered pod changes are checked |
| Workflow lint | Actionlint validates GitHub Actions syntax and expressions |
| Image | Locked dependency build; entrypoint under arbitrary UID/read-only filesystem; policy/source-checkout isolation |
| kind 1.35.0 | Restricted Pod Security admission, server-side dry-run, init validation, readiness, Service connectivity, signed/tampered webhooks, shadow mode, arbitrary UID, read-only mounts, policy replacement, invalid-config startup rejection, rollback |

Actions and kind node images are pinned by commit/digest. The Route schema comes
from a pinned `openshift/api` revision on release-4.18. Schema validation does not
exercise an OpenShift router, SCC admission, or TLS certificates: verify those in
the actual OpenShift cluster before enabling writes. kind uses disabled policies
and ephemeral credentials; it tests deployment behavior without making real GitHub
or model calls. Application unit tests cover evaluation and merge behavior.

`CI complete` is the aggregate check suitable for branch protection. Failing jobs
retain test reports, rendered manifests, or cluster logs/events; Secret values
are excluded. The image artifact is retained for one day. Publishing is automatic
for runtime-input changes on `main` (and may be run manually there), restricted
to `main`, and gated on the same full test suite.

To reproduce the cluster smoke test locally with Docker, kind 0.31.0,
Kustomize 5.6.0, kubectl, and uv installed:

```bash
docker build -f Containerfile -t auto-merge-agent:ci .
kind create cluster --name automerge
kind load docker-image auto-merge-agent:ci --name automerge
uv sync --locked
uv run python scripts/smoke_deployment.py auto-merge-agent:ci
kind delete cluster --name automerge
```

Use a disposable kind cluster: the script creates resources and deliberately
deploys an invalid policy before testing rollback. It uses port 18000 locally.
