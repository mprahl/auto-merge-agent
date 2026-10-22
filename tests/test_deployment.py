from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prepare_deployment", ROOT / "scripts/prepare_deployment.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def policy(tmp_path):
    root = tmp_path / "policies"
    shutil.copytree(ROOT / "tests/fixtures/deployment", root)
    return root


def prepare(policy, tmp_path, name="deploy", repositories=None):
    return module.prepare(policy, tmp_path / name, repositories or ["ci-owner/fixture"], "quay.io/example/agent:abc123")


def test_snapshot_preserves_layout_and_excludes_unselected_repos(policy, tmp_path):
    other = policy / "configs/github.com/other/repo"
    other.mkdir(parents=True)
    (other / "config.yaml").write_text("not valid YAML: [")
    (policy / "configs/github.com/ci-owner/config.yaml").write_text("schema_version: 1\n")
    context = policy / "configs/github.com/ci-owner/fixture/context.md"
    context.write_text("Deployment guidance\n")
    output = prepare(policy, tmp_path)
    component = yaml.safe_load((output / "policy/kustomization.yaml").read_text())
    volume = yaml.safe_load((output / "policy/volume.yaml").read_text())
    items = volume["spec"]["template"]["spec"]["volumes"][0]["configMap"]["items"]
    assert len(items) == len(component["configMapGenerator"][0]["files"]) == 6
    for item in items:
        assert (output / "policy/files" / item["key"]).read_bytes() == (policy / item["path"]).read_bytes()
        assert "other" not in item["path"]
    with pytest.raises(ValueError, match="already exists"):
        prepare(policy, tmp_path)


@pytest.mark.parametrize("repository", ["../fixture", "ci-owner/..", "ci-owner/a/b", "ci-owner/", ""])
def test_invalid_repository_rejected(policy, tmp_path, repository):
    with pytest.raises(ValueError, match="Invalid repository"):
        prepare(policy, tmp_path, repositories=[repository])
    assert not (tmp_path / "deploy").exists()


def test_requires_explicit_selection(policy, tmp_path):
    with pytest.raises(ValueError, match="Select at least one"):
        module.prepare(policy, tmp_path / "deploy", [], "agent:ci")


def test_missing_owners_and_invalid_policy_rejected(policy, tmp_path):
    owners = policy / "configs/github.com/ci-owner/fixture/OWNERS"
    owners.unlink()
    with pytest.raises(ValueError, match="Missing required"):
        prepare(policy, tmp_path)
    owners.write_text("approvers: [ci-owner]\n")
    (owners.parent / "config.yaml").write_text("schema_version: 1\nenabled: true\n")
    with pytest.raises(ValueError, match="requires expected CI checks"):
        prepare(policy, tmp_path)
    assert not (tmp_path / "deploy").exists()


def test_symlink_escape_rejected(policy, tmp_path):
    global_config = policy / "config/global.yaml"
    global_config.unlink()
    external = tmp_path / "external.yaml"
    external.write_text("schema_version: 1\n")
    global_config.symlink_to(external)
    with pytest.raises(ValueError, match="escapes root"):
        prepare(policy, tmp_path)


def test_oversized_bundle_rejected(policy, tmp_path):
    (policy / "OWNERS").write_text("#" * (module.MAX_POLICY_BYTES + 1))
    with pytest.raises(ValueError, match=f"{module.MAX_POLICY_BYTES // 1000} KB"):
        prepare(policy, tmp_path)


@pytest.mark.parametrize("image", ["agent", "agent:latest", "agent:bad tag", "agent@sha256:abc", "registry:5000/agent"])
def test_unpinned_or_invalid_image_rejected(image):
    with pytest.raises(ValueError):
        module.image_transform(image)


def test_digest_image():
    digest = "sha256:" + "a" * 64
    assert module.image_transform(f"quay.io/example/agent@{digest}") == {
        "name": "auto-merge-agent", "newName": "quay.io/example/agent", "digest": digest,
    }


def test_multiple_repos_profiles_and_inheritance_survive_packaging(policy, tmp_path):
    profiles = policy / "profiles"
    profiles.mkdir()
    (profiles / "docs.yaml").write_text("schema_version: 1\nrules:\n  - name: docs\n    type: changed_paths_only\n    paths: ['docs/**']\n")
    (policy / "configs/github.com/ci-owner/config.yaml").write_text(
        "schema_version: 1\nci:\n  expected: [{name: tests}]\nrules:\n  - profile: docs\n"
    )
    repo = policy / "configs/github.com/ci-owner/fixture"
    (repo / "config.yaml").write_text("schema_version: 1\nenabled: true\n")
    shutil.copytree(repo, repo.parent / "second")
    output = prepare(policy, tmp_path, repositories=["ci-owner/fixture", "ci-owner/second"])
    volume = yaml.safe_load((output / "policy/volume.yaml").read_text())
    items = volume["spec"]["template"]["spec"]["volumes"][0]["configMap"]["items"]
    mounted = tmp_path / "mounted"
    for item in items:
        target = mounted / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(output / "policy/files" / item["key"], target)
    store = module.ConfigStore(mounted)
    assert len(store.validate_all()) == 2
    for name in ["fixture", "second"]:
        config = store.load("github.com", "ci-owner", name)
        assert config.policy.enabled
        assert config.policy.rules[0].name == "docs"
        assert config.policy.ci.expected[0].name == "tests"


def build(output, overlay):
    return [d for d in yaml.safe_load_all(subprocess.check_output(
        ["kustomize", "build", str(output / "overlays" / overlay)], text=True,
    )) if d]


@pytest.mark.skipif(shutil.which("kustomize") is None, reason="kustomize required (installed in deployment CI)")
def test_rendered_mounts_rollout_and_platform_security(policy, tmp_path):
    first = prepare(policy, tmp_path, "first")
    for overlay in ["openshift", "kind"]:
        docs = build(first, overlay)
        cm = next(d for d in docs if d["kind"] == "ConfigMap")
        deployment = next(d for d in docs if d["kind"] == "Deployment")
        pod = deployment["spec"]["template"]["spec"]
        volume = next(v for v in pod["volumes"] if v["name"] == "policy")["configMap"]
        assert volume["name"] == cm["metadata"]["name"]
        assert set(cm["data"]) == {item["key"] for item in volume["items"]}
        for item in volume["items"]:
            assert cm["data"][item["key"]] == (policy / item["path"]).read_text()
        assert deployment["spec"]["replicas"] == 1
        assert deployment["spec"]["strategy"] == {"type": "Recreate"}
        assert pod["automountServiceAccountToken"] is False
        for container in pod["containers"] + pod["initContainers"]:
            assert container["image"] == "quay.io/example/agent:abc123"
            assert container["securityContext"]["readOnlyRootFilesystem"] is True
        if overlay == "openshift":
            assert "runAsUser" not in pod["securityContext"]
            route = next(d for d in docs if d["kind"] == "Route")
            assert route["spec"]["tls"]["insecureEdgeTerminationPolicy"] == "Redirect"
            assert route["spec"]["path"] == "/webhook"
        else:
            assert not any(d["kind"] == "Route" for d in docs)
            assert pod["securityContext"]["runAsUser"] == 1000780000
    with (policy / "config/global.yaml").open("a") as f:
        f.write("\n# rollout change\n")
    second = prepare(policy, tmp_path, "second")
    get_template = lambda output: next(d for d in build(output, "kind") if d["kind"] == "Deployment")["spec"]["template"]
    assert get_template(first) != get_template(second)
