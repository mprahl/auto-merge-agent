"""Convert the pinned upstream OpenShift Route CRD schema for kubeconform."""
import json
from pathlib import Path
import sys

import yaml

crd = yaml.safe_load(Path(sys.argv[1]).read_text())
assert crd["spec"]["group"] == "route.openshift.io"
schema = next(v for v in crd["spec"]["versions"] if v["name"] == "v1")["schema"]["openAPIV3Schema"]


def convert(value):
    if isinstance(value, list):
        return [convert(item) for item in value]
    if not isinstance(value, dict):
        return value
    value = {key: convert(item) for key, item in value.items()}
    if value.get("x-kubernetes-int-or-string"):
        value.pop("type", None)
        value["anyOf"] = [{"type": "integer"}, {"type": "string"}]
    if value.get("type") == "object" and "properties" in value:
        value.setdefault("additionalProperties", False)
    return value


output = Path(sys.argv[2])
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(convert(schema)))
