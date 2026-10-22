#!/usr/bin/env bash
# Exclude Secrets and pod environment values from uploaded diagnostics.
set -euo pipefail
mkdir -p build/diagnostics
kubectl -n auto-merge-agent get pods,deployments,services,events -o wide > build/diagnostics/resources.txt 2>&1 || true
while IFS= read -r pod; do
  name="${pod#pod/}"
  for container in agent validate-config; do
    kubectl -n auto-merge-agent logs "$pod" -c "$container" > "build/diagnostics/$name-$container.log" 2>&1 || true
    kubectl -n auto-merge-agent logs "$pod" -c "$container" --previous > "build/diagnostics/$name-$container-previous.log" 2>&1 || true
  done
done < <(kubectl -n auto-merge-agent get pods -o name 2>/dev/null || true)
