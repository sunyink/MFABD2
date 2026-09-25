"""Resolve the release type and cancellation scope from the triggering event."""

import json
import os
from pathlib import Path


def resolve(event_name: str, ref: str, event: dict) -> dict[str, str]:
    inputs = (event.get("inputs") or {}) if event_name == "workflow_dispatch" else {}
    message = (event.get("head_commit") or {}).get("message") or ""
    # Preserve the existing echo "$commit_message" | tail -n1 marker rule.
    last_line = message.split("\n")[-1]

    def enabled(name: str) -> bool:
        return inputs.get(name) in (True, "true")

    if ref.startswith("refs/tags/v"):
        version_type = "release"
    elif enabled("deploy_alpha") or "[deploy-alpha]" in last_line:
        version_type = "alpha"
    elif enabled("deploy_beta") or "[deploy-beta]" in last_line:
        version_type = "beta"
    else:
        version_type = "ci"

    channel = version_type
    if version_type == "ci" and enabled("ci_as_stable"):
        channel = "release"
    # PRs use refs/pull/<number>/merge, so they cannot cancel branch pushes.
    group = f"ci-{ref}" if channel == "ci" else channel
    return {"version_type": version_type, "concurrency_group": group}


def main() -> None:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    outputs = resolve(os.environ["GITHUB_EVENT_NAME"], os.environ["GITHUB_REF"], event)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        for key, value in outputs.items():
            print(f"{key}={value}")
            stream.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
