"""Stage a self-contained Kustomize tree from explicitly selected policy files."""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import tempfile
from pathlib import Path

import yaml

from auto_merge_agent.config import ConfigStore

REPO_ROOT = Path(__file__).resolve().parents[1]
# `kubectl apply` copies the whole ConfigMap into the 256 KiB last-applied
# annotation; stay well below it to leave room for JSON-escaping overhead.
MAX_POLICY_BYTES = 200_000


def selected_files(root: Path, repositories: list[str]) -> list[Path]:
    if not repositories:
        raise ValueError("Select at least one repository explicitly")
    paths = {Path("OWNERS"), Path("config/global.yaml")}
    paths.update(p.relative_to(root) for p in (root / "profiles").glob("*.yaml"))
    for repository in repositories:
        if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError(f"Invalid repository: {repository!r}")
        owner, repo = repository.split("/")
        if repo in {".", ".."}:
            raise ValueError(f"Invalid repository: {repository!r}")
        directory = Path("configs/github.com") / owner / repo
        paths.update([directory / "config.yaml", directory / "OWNERS"])
        for optional in [directory.parent / "config.yaml", directory / "context.md"]:
            if (root / optional).exists():
                paths.add(optional)
    for path in paths:
        source = root / path
        if not source.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Policy file escapes root: {path}")
        if any((root / parent).is_symlink() for parent in [path, *path.parents]):
            raise ValueError(f"Policy symlinks are not supported: {path}")
        if not source.is_file():
            raise ValueError(f"Missing required policy file: {path}")
    return sorted(paths)


def image_transform(image: str) -> dict[str, str]:
    if not image or any(c.isspace() for c in image):
        raise ValueError("Image must be a tagged reference or digest")
    if "@sha256:" in image:
        name, digest = image.split("@", 1)
        if not name or not re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest):
            raise ValueError("Invalid image digest")
        return {"name": "auto-merge-agent", "newName": name, "digest": digest}
    if ":" not in image.rsplit("/", 1)[-1]:
        raise ValueError("Image must have an explicit tag or sha256 digest")
    name, tag = image.rsplit(":", 1)
    if not name or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag) or tag == "latest":
        raise ValueError("Use a release/commit tag or digest, not latest")
    return {"name": "auto-merge-agent", "newName": name, "newTag": tag}


def write_yaml(path: Path, value: dict) -> None:
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def prepare(root: Path, output: Path, repositories: list[str], image: str) -> Path:
    root, output = root.resolve(), output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError(f"Output already exists; use a new directory: {output}")
    paths = selected_files(root, repositories)
    transform = image_transform(image)
    # Validate an isolated snapshot of exactly what will be deployed, not other repos.
    with tempfile.TemporaryDirectory(prefix="automerge-policy-") as tmp:
        snapshot = Path(tmp)
        total = 0
        for path in paths:
            data = (root / path).read_text(encoding="utf-8")
            total += len(data.encode("utf-8")) + 69  # file- plus SHA-256 key
            if total > MAX_POLICY_BYTES:
                raise ValueError(
                    f"Selected policies exceed the {MAX_POLICY_BYTES // 1000} KB ConfigMap budget"
                )
            target = snapshot / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(data, encoding="utf-8")
        ConfigStore(snapshot).validate_all()
        output.parent.mkdir(parents=True, exist_ok=True)
        # Build off to the side; an invalid policy never leaves a partial output.
        with tempfile.TemporaryDirectory(prefix=".automerge-stage-", dir=output.parent) as stage_dir:
            stage = Path(stage_dir) / "deploy"
            shutil.copytree(REPO_ROOT / "deploy/base", stage / "base")
            shutil.copytree(REPO_ROOT / "deploy/overlays", stage / "overlays")
            policy = stage / "policy"
            (policy / "files").mkdir(parents=True)
            files, items = [], []
            for path in paths:
                key = "file-" + hashlib.sha256(path.as_posix().encode()).hexdigest()
                shutil.copyfile(snapshot / path, policy / "files" / key)
                files.append(f"{key}=files/{key}")
                items.append({"key": key, "path": path.as_posix()})
            write_yaml(policy / "volume.yaml", {
                "apiVersion": "apps/v1", "kind": "Deployment",
                "metadata": {"name": "auto-merge-agent"},
                "spec": {"template": {"spec": {"volumes": [{
                    "name": "policy", "configMap": {
                        "name": "auto-merge-agent-policy", "items": items,
                    },
                }]}}},
            })
            write_yaml(policy / "kustomization.yaml", {
                "apiVersion": "kustomize.config.k8s.io/v1alpha1", "kind": "Component",
                "configMapGenerator": [{"name": "auto-merge-agent-policy", "files": files}],
                "patches": [{"path": "volume.yaml"}],
            })
            for overlay in (stage / "overlays").iterdir():
                path = overlay / "kustomization.yaml"
                manifest = yaml.safe_load(path.read_text())
                manifest["images"] = [transform]
                write_yaml(path, manifest)
            stage.rename(output)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", action="append", required=True, help="owner/repo; repeat to select more")
    parser.add_argument("--image", required=True, help="Quay release/commit tag or sha256 digest")
    args = parser.parse_args()
    try:
        output = prepare(args.policy_root, args.output, args.repository, args.image)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Cannot prepare deployment: {exc}\n")
    print(f"Prepared {output}; apply overlays/openshift or overlays/kind")


if __name__ == "__main__":
    main()
