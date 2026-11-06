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


def marked_commit(message: str) -> str:
    """Return the number from lines starting with `[mfaa:abcdef]`, or "" when there is none.

    The marker may share its line with a deploy marker. Malformed or conflicting markers raise,
    so a typo fails the run instead of silently packaging the upstream client.
    """
    commits = set()
    for line in message.splitlines():
        line = line.strip()
        if not line.lower().startswith("[mfaa:"):
            continue
        match = MFAA_MARKER.match(line)
        value = match[1] if match else line
        if not SHORT_COMMIT.fullmatch(value.lower()):
            raise ValueError(f"自家 MFAA 提交号必须恰好为六位十六进制字符，收到 {value!r}")
        commits.add(value.lower())
    if len(commits) > 1:
        raise ValueError(f"同一段说明里有多个不同的自家 MFAA 提交号：{', '.join(sorted(commits))}")
    return commits.pop() if commits else ""


def resolve_mfaa_commit(event_name: str, event: dict, tag_message: str = "") -> str:
    """Return the six-character fork build to package, or "" for the requirements.txt client.

    Manual runs take the `mfaa_commit` input. Pushes read the marker from the annotated tag's
    message first (official releases keep the hand-picked version in the tag name), then from
    the head commit message. All of this is settled before the build replaces any in-progress
    run of the same channel.
    """
    if event_name == "workflow_dispatch":
        value = str((event.get("inputs") or {}).get("mfaa_commit") or "").strip()
        if value and not SHORT_COMMIT.fullmatch(value.lower()):
            raise ValueError(f"自家 MFAA 提交号必须恰好为六位十六进制字符，收到 {value!r}")
        return value.lower()
    if event_name != "push":
        return ""
    return marked_commit(tag_message) or marked_commit((event.get("head_commit") or {}).get("message") or "")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True, encoding="utf-8").stdout


def tag_message(ref: str) -> str:
    """Return the message of the pushed annotated tag, or "" for a lightweight tag."""
    # actions/checkout may hold the pushed tag as a lightweight ref; fetch the real object first.
    git("fetch", "--quiet", "--no-tags", "origin", f"+{ref}:{ref}")
    if git("cat-file", "-t", ref).strip() != "tag":
        return ""
    return git("for-each-ref", "--format=%(contents)", ref)


def main() -> None:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    event_name = os.environ["GITHUB_EVENT_NAME"]
    ref = os.environ["GITHUB_REF"]
    outputs = resolve(event_name, ref, event)
    marker_event, annotation = event, ""
    if event_name == "push":
        if not (event.get("head_commit") or {}).get("message"):
            # Some tag pushes arrive without head_commit (v4.6.1); read the checked-out commit instead.
            marker_event = {"head_commit": {"message": git("log", "-1", "--format=%B")}}
        if ref.startswith("refs/tags/"):
            annotation = tag_message(ref)
    try:
        outputs["mfaa_commit"] = resolve_mfaa_commit(event_name, marker_event, annotation)
    except ValueError as error:
        print(f"::error::{error}")
        raise SystemExit(1)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        for key, value in outputs.items():
            print(f"{key}={value}")
            stream.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
