# Deployment sources

These are templates used by `scripts/prepare_deployment.py`. Prepare a deployment
before running Kustomize: the helper adds the selected policy ConfigMap component
and sets the image on both the service and its validation init container.

See [the deployment guide](../docs/openshift.md) for the complete workflow.

- `base/`: platform-neutral GitHub App service (one replica, shadow mode).
- `overlays/openshift/`: an edge TLS Route for `/webhook` and a namespace.
- `overlays/kind/`: no public endpoint; a restricted namespace and arbitrary UID
  for CI. This overlay also works as an internal-only Kubernetes deployment.

The generated `policy/` component is intentionally absent from source control.
It is assembled from the existing policy files, with their directory layout
preserved through ConfigMap volume `items` mappings. Do not use
`--load-restrictor LoadRestrictionsNone` or commit credentials into this tree.
