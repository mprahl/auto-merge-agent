"""Exercise a loaded image in an existing kind cluster without real credentials."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import yaml

from prepare_deployment import prepare, REPO_ROOT

NAMESPACE = "auto-merge-agent"
# Ephemeral per-run secret; generated, never stored in the repository.
WEBHOOK_SECRET = secrets.token_urlsafe(32)


def run(*args: str, input: str | None = None, timeout: int = 180) -> str:
    return subprocess.check_output(args, input=input, text=True, timeout=timeout)


def kubectl(*args: str, **kwargs) -> str:
    return run("kubectl", "-n", NAMESPACE, *args, **kwargs)


def wait_for(check, description: str, timeout: int = 90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(1)
    raise AssertionError(f"Timed out: {description}")


def pods() -> list[dict]:
    return json.loads(kubectl("get", "pods", "-l", "app.kubernetes.io/name=auto-merge-agent", "-o", "json"))["items"]


def current_pod() -> dict:
    candidates = [p for p in pods() if not p["metadata"].get("deletionTimestamp")]
    assert len(candidates) == 1, candidates
    return candidates[0]


def rollout() -> dict:
    kubectl("rollout", "status", "deployment/auto-merge-agent", "--timeout=180s", timeout=210)
    pod = current_pod()
    assert next(s for s in pod["status"]["initContainerStatuses"] if s["name"] == "validate-config")["state"]["terminated"]["exitCode"] == 0
    return pod


def apply_rendered(path: Path) -> str:
    rendered = run("kustomize", "build", str(path / "overlays/kind"))
    kubectl("apply", "--dry-run=server", "-f", "-", input=rendered)
    kubectl("apply", "-f", "-", input=rendered)
    return rendered


def check_http():
    def request(path, body=None, signature=None):
        headers = {"X-GitHub-Event": "pull_request", "Content-Type": "application/json"}
        if signature:
            headers["X-Hub-Signature-256"] = signature
        req = urllib.request.Request("http://127.0.0.1:18000" + path, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def healthy():
        try:
            return request("/health") == (200, {"ok": True, "active": False})
        except (OSError, urllib.error.URLError):
            return False

    with tempfile.TemporaryFile(mode="w+") as logs:
        process = subprocess.Popen(
            ["kubectl", "-n", NAMESPACE, "port-forward", "service/auto-merge-agent", "18000:8000"],
            stdout=logs, stderr=subprocess.STDOUT,
        )
        try:
            wait_for(healthy, "service health via port-forward")
            body = json.dumps({
                "action": "opened", "number": 1,
                "repository": {"owner": {"login": "ci-owner"}, "name": "fixture"},
            }).encode()
            signature = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
            assert request("/webhook", body)[0] == 401
            assert request("/webhook", body, "sha256=invalid")[0] == 401
            assert request("/webhook", body + b" ", signature)[0] == 401
            # A valid signature is accepted, but disabled policies never enqueue work.
            assert request("/webhook", body, signature) == (200, {"queued": 0})
        finally:
            process.terminate()
            process.wait(timeout=10)


def main():
    context = run("kubectl", "config", "current-context", timeout=30).strip()
    if not context.startswith("kind-"):
        raise SystemExit(f"Refusing to run against non-kind context: {context}")
    image = sys.argv[1] if len(sys.argv) > 1 else "auto-merge-agent:ci"
    with tempfile.TemporaryDirectory(prefix="automerge-smoke-") as tmp:
        root = Path(tmp)
        policy = root / "policies"
        shutil.copytree(REPO_ROOT / "tests/fixtures/deployment", policy)
        first = prepare(policy, root / "first", ["ci-owner/fixture"], image)
        kubectl("apply", "-f", str(first / "overlays/kind/namespace.yaml"))
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        # Ephemeral test key, never uploaded as an artifact or logged.
        secret = {
            "apiVersion": "v1", "kind": "Secret", "metadata": {"name": "auto-merge-agent-github"},
            "stringData": {
                "app-id": "12345", "webhook-secret": WEBHOOK_SECRET,
                "private-key.pem": key.private_bytes(serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
            },
        }
        kubectl("apply", "-f", "-", input=yaml.safe_dump(secret))
        original = apply_rendered(first)
        # GITHUB_WEBHOOK_SECRET is env-injected, so a rerun against an existing
        # cluster must restart the pod to pick up the newly generated secret.
        kubectl("rollout", "restart", "deployment/auto-merge-agent")
        pod = rollout()
        first_uid = pod["metadata"]["uid"]
        print("PASS: init validation, probes, Service, and restricted pod admission", flush=True)
        check_http()
        print("PASS: shadow health, signed webhook, missing/invalid/tampered signatures", flush=True)
        check = '''import os
from pathlib import Path
assert os.getuid() == 1000780000
assert not Path('/var/run/secrets/kubernetes.io/serviceaccount/token').exists()
assert Path('/etc/github-app/private-key.pem').read_text().startswith('-----BEGIN')
assert Path('/etc/auto-merge-agent/configs/github.com/ci-owner/fixture/OWNERS').is_file()
for name in ['/app/unexpected-write', '/etc/auto-merge-agent/OWNERS']:
    try:
        Path(name).write_text('unexpected')
    except OSError:
        pass
    else:
        raise AssertionError(name + ' is writable')
Path('/tmp/writable-check').write_text('ok')
'''
        kubectl("exec", pod["metadata"]["name"], "-c", "agent", "--", "python", "-c", check)
        print("PASS: arbitrary UID, read-only root/policies, writable tmp, no API token", flush=True)

        with (policy / "config/global.yaml").open("a") as f:
            f.write("\n# Exercise policy-triggered rollout\n")
        second = prepare(policy, root / "second", ["ci-owner/fixture"], image)
        updated = apply_rendered(second)
        assert rollout()["metadata"]["uid"] != first_uid
        assert first_uid not in {p["metadata"]["uid"] for p in pods()}
        check_http()
        print("PASS: policy hash triggers replacement and health recovers", flush=True)

        # Bypass the packaging validator to test the in-cluster init gate itself.
        manifests = list(yaml.safe_load_all(updated))
        cm = next(d for d in manifests if d["kind"] == "ConfigMap")
        deployment = next(d for d in manifests if d["kind"] == "Deployment")
        volume = next(v for v in deployment["spec"]["template"]["spec"]["volumes"] if v["name"] == "policy")["configMap"]
        cm["metadata"]["name"] += "-invalid"
        volume["name"] = cm["metadata"]["name"]
        key = next(item["key"] for item in volume["items"] if item["path"] == "config/global.yaml")
        cm["data"][key] = "not: [valid YAML"
        kubectl("apply", "-f", "-", input=yaml.safe_dump_all(manifests))

        def rejected():
            for candidate in pods():
                if candidate["metadata"].get("deletionTimestamp"):
                    continue
                if not any(v.get("configMap", {}).get("name") == cm["metadata"]["name"] for v in candidate["spec"]["volumes"]):
                    continue
                for status in candidate.get("status", {}).get("initContainerStatuses", []):
                    for state in [status.get("state", {}), status.get("lastState", {})]:
                        if state.get("terminated", {}).get("exitCode", 0) != 0:
                            return candidate
            return None

        bad = wait_for(rejected, "invalid policy rejected by init container")
        assert not any(s.get("state", {}).get("running") for s in bad["status"].get("containerStatuses", []))
        print("PASS: invalid policy prevents the service from starting", flush=True)
        kubectl("apply", "-f", "-", input=original)
        rollout()
        check_http()
        print("PASS: rollback to previous policy restores service", flush=True)


if __name__ == "__main__":
    main()
