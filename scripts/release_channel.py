"""Resolve the release type, cancellation scope and MFAA fork build from the triggering event."""

import json
import os
from pathlib import Path
import re
import subprocess

MFAA_MARKER = re.compile(r"\[mfaa:([^\]\s]*)\]", re.IGNORECASE)
SHORT_COMMIT = re.compile(r"[0-9a-f]{6}")


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


def resolve_mfaa_commit(event_name: str, event: dict) -> str:
    """Return the six-character fork build to package, or "" for the requirements.txt client.

    Manual runs take the `mfaa_commit` input. Pushes (branches and tags) take a line in the
    head commit message that starts with `[mfaa:abcdef]`, so it may share the last line with a
    deploy marker. Malformed or conflicting markers fail here, before the build replaces any
    in-progress release run, instead of silently falling back to the upstream client.
    """
    if event_name == "workflow_dispatch":
        value = str((event.get("inputs") or {}).get("mfaa_commit") or "").strip()
        values = [value] if value else []
    elif event_name == "push":
        message = (event.get("head_commit") or {}).get("message") or ""
        lines = [line.strip() for line in message.splitlines()]
        values = []
        for line in lines:
            if line.lower().startswith("[mfaa:"):
                match = MFAA_MARKER.match(line)
                values.append(match[1] if match else line)
    else:
        return ""
    commits = set()
    for value in values:
        if not SHORT_COMMIT.fullmatch(value.lower()):
            raise ValueError(f"自家 MFAA 提交号必须恰好为六位十六进制字符，收到 {value!r}")
        commits.add(value.lower())
    if len(commits) > 1:
        raise ValueError(f"提交信息里有多个不同的自家 MFAA 提交号：{', '.join(sorted(commits))}")
    return commits.pop() if commits else ""


def main() -> None:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    event_name = os.environ["GITHUB_EVENT_NAME"]
    outputs = resolve(event_name, os.environ["GITHUB_REF"], event)
    marker_event = event
    if event_name == "push" and not (event.get("head_commit") or {}).get("message"):
        # Some tag pushes arrive without head_commit (v4.6.1); read the checked-out commit instead.
        message = subprocess.run(["git", "log", "-1", "--format=%B"], check=True, capture_output=True,
                                 text=True, encoding="utf-8").stdout
        marker_event = {"head_commit": {"message": message}}
    try:
        outputs["mfaa_commit"] = resolve_mfaa_commit(event_name, marker_event)
    except ValueError as error:
        print(f"::error::{error}")
        raise SystemExit(1)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        for key, value in outputs.items():
            print(f"{key}={value}")
            stream.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
