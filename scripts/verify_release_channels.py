"""Regression checks for release cancellation boundaries (no network access)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from release_channel import resolve


def push(message="", branch="main"):
    return resolve("push", f"refs/heads/{branch}", {"head_commit": {"message": message}})


def manual(branch="main", **inputs):
    return resolve("workflow_dispatch", f"refs/heads/{branch}", {"inputs": inputs})


class ReleaseChannelChecks(unittest.TestCase):
    def test_followup_push_cannot_cancel_beta_release(self):
        release = push("Merge:'chore/ArbNT'| 商店套利 Opus\n\n[deploy-beta]", "feat/NT-Suport")
        followup = push("fix:异形比例PC精炼", "feat/NT-Suport")
        self.assertEqual(release, {"version_type": "beta", "concurrency_group": "beta"})
        self.assertNotEqual(release["concurrency_group"], followup["concurrency_group"])

    def test_only_same_branch_ordinary_builds_replace_each_other(self):
        self.assertEqual(push("first"), push("second"))
        self.assertNotEqual(push(branch="main"), push(branch="feat/NT-Suport"))
        self.assertEqual(manual(), push())
        pr = resolve("pull_request", "refs/pull/510/merge", {})
        self.assertNotEqual(pr["concurrency_group"], push()["concurrency_group"])
        self.assertNotEqual(pr, resolve("pull_request", "refs/pull/511/merge", {}))

    def test_release_channels_are_shared_across_branches_and_triggers(self):
        for channel in ("alpha", "beta"):
            with self.subTest(channel=channel):
                expected = {"version_type": channel, "concurrency_group": channel}
                self.assertEqual(push(f"[deploy-{channel}]", "main"), expected)
                self.assertEqual(push(f"[deploy-{channel}]", "feat/NT-Suport"), expected)
                self.assertEqual(manual(**{f"deploy_{channel}": True}), expected)
                self.assertEqual(manual("another", **{f"deploy_{channel}": "true"}), expected)
        groups = {push()["concurrency_group"], manual(deploy_alpha=True)["concurrency_group"],
                  manual(deploy_beta=True)["concurrency_group"], manual(ci_as_stable=True)["concurrency_group"]}
        self.assertEqual(len(groups), 4)

    def test_tag_and_ci_as_stable_share_official_channel(self):
        for ref in ("refs/tags/v4.4.5", "refs/tags/v4.4.6"):
            with self.subTest(ref=ref):
                self.assertEqual(resolve("push", ref, {}),
                                 {"version_type": "release", "concurrency_group": "release"})
        self.assertEqual(manual(ci_as_stable="true"),
                         {"version_type": "ci", "concurrency_group": "release"})

    def test_existing_priority_and_false_input_values(self):
        self.assertEqual(manual(deploy_alpha="false", deploy_beta="false", ci_as_stable="false"), push())
        self.assertEqual(manual(deploy_alpha=True, deploy_beta=True, ci_as_stable=True)["version_type"], "alpha")
        self.assertEqual(manual(deploy_beta=True, ci_as_stable=True)["concurrency_group"], "beta")
        event = {"inputs": {"deploy_alpha": True}, "head_commit": {"message": "[deploy-beta]"}}
        self.assertEqual(resolve("push", "refs/heads/main", event)["version_type"], "beta")
        self.assertEqual(resolve("workflow_dispatch", "refs/tags/v4.4.5", event)["version_type"], "release")

    def test_only_the_last_line_can_select_a_release_channel(self):
        cases = {
            "docs: explain [deploy-beta]\n\nNo release requested": "ci",
            "[deploy-alpha]\n[deploy-beta]": "beta",
            "[deploy-beta]\n[deploy-alpha]": "alpha",
            "[deploy-beta] [deploy-alpha]": "alpha",
            "fix: example\n\n  [deploy-beta]  ": "beta",
            "fix: example\r\n\r\n[deploy-beta]\r": "beta",
            "[deploy-beta]\n": "ci",
            "[DEPLOY-BETA]": "ci",
            "": "ci",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(push(message)["version_type"], expected)

    def test_script_reads_event_and_emits_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            event = root / "event.json"
            output = root / "output.txt"
            event.write_text(json.dumps({"head_commit": {"message": "fix: example\n\n[deploy-beta]"}}),
                             encoding="utf-8")
            output.write_text("existing=preserved\n", encoding="utf-8")
            env = dict(os.environ, GITHUB_EVENT_PATH=str(event), GITHUB_OUTPUT=str(output),
                       GITHUB_EVENT_NAME="push", GITHUB_REF="refs/heads/main")
            subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("release_channel.py"))],
                           env=env, check=True, capture_output=True, text=True)
            self.assertEqual(output.read_text(encoding="utf-8"),
                             "existing=preserved\nversion_type=beta\nconcurrency_group=beta\n")


if __name__ == "__main__":
    unittest.main()
