"""Regression checks for release cancellation boundaries (no network access)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from release_channel import resolve, resolve_mfaa_commit, tag_mfaa_commit


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
                             "existing=preserved\nversion_type=beta\nconcurrency_group=beta\nmfaa_commit=\nmfaa_fallback=false\n")

    def test_script_emits_marker_and_rejects_bad_marker_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            event = root / "event.json"
            output = root / "output.txt"
            env = dict(os.environ, GITHUB_EVENT_PATH=str(event), GITHUB_OUTPUT=str(output),
                       GITHUB_EVENT_NAME="push", GITHUB_REF="refs/heads/main")
            script = str(Path(__file__).with_name("release_channel.py"))
            event.write_text(json.dumps({"head_commit": {"message": "fix: x\n\n[mfaa:9AA52B]\n[deploy-beta]"}}),
                             encoding="utf-8")
            subprocess.run([sys.executable, "-B", script], env=env, check=True, capture_output=True, text=True)
            self.assertEqual(output.read_text(encoding="utf-8"),
                             "version_type=beta\nconcurrency_group=beta\nmfaa_commit=9aa52b\nmfaa_fallback=false\n")
            output.unlink()
            event.write_text(json.dumps({"head_commit": {"message": "fix: x\n\n[mfaa:六位提交号]\n[deploy-beta]"}}),
                             encoding="utf-8")
            result = subprocess.run([sys.executable, "-B", script], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("::error::", result.stdout)
            self.assertFalse(output.exists())



class TagPushChecks(unittest.TestCase):
    """Tag pushes run against a real origin so the annotated-tag fetch itself is exercised."""

    def run_tag_push(self, commit_message, tag_message=None, with_head_commit=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin, clone = root / "origin", root / "clone"
            config = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.hooksPath=/dev/null"]

            def git(cwd, *args):
                subprocess.run(["git", "-C", str(cwd), *config, *args], check=True, capture_output=True)

            subprocess.run(["git", "init", "-q", str(origin)], check=True)
            git(origin, "commit", "-q", "--allow-empty", "-m", commit_message)
            if tag_message is None:
                git(origin, "tag", "v4.6.5")
            else:
                git(origin, "tag", "-a", "v4.6.5", "-m", tag_message)
            subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
            # Imitate actions/checkout, which can leave the pushed tag as a lightweight ref.
            git(clone, "tag", "-f", "v4.6.5", "HEAD")
            event, output = root / "event.json", root / "output.txt"
            payload = {"head_commit": {"message": commit_message}} if with_head_commit else {}
            event.write_text(json.dumps(payload), encoding="utf-8")
            env = dict(os.environ, GITHUB_EVENT_PATH=str(event), GITHUB_OUTPUT=str(output),
                       GITHUB_EVENT_NAME="push", GITHUB_REF="refs/tags/v4.6.5")
            result = subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("release_channel.py"))],
                                    env=env, cwd=clone, capture_output=True, text=True, encoding="utf-8")
            return result, output.read_text(encoding="utf-8") if output.exists() else None

    def assert_selects(self, expected, *args, warned=False, **kwargs):
        result, output = self.run_tag_push(*args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(output, "version_type=release\nconcurrency_group=release\n"
                                 f"mfaa_commit={expected}\nmfaa_fallback=true\n")
        self.assertEqual("::warning::" in result.stdout, warned, result.stdout)

    def test_annotated_tag_message_selects_fork_build(self):
        self.assert_selects("9aa52b", "Merge:'fix/x'| y", "[mfaa:9AA52B]")
        self.assert_selects("9aa52b", "Merge:'fix/x'| y\n\n[mfaa:7ff8c6]\n[deploy-beta]", "正式版\n\n[mfaa:9aa52b]")

    def test_tag_without_marker_releases_upstream_client_and_ignores_commit(self):
        for tag_message in ("", "正式版说明", None):
            with self.subTest(tag_message=tag_message):
                self.assert_selects("", "Merge:'fix/x'| y\n\n[mfaa:7ff8c6]", tag_message)
        # Some tag pushes carry no head_commit at all (v4.6.1); that changes nothing either.
        self.assert_selects("", "Merge:'fix/x'| y", "", with_head_commit=False)

    def test_bad_tag_marker_warns_and_releases_upstream_client(self):
        for tag_message in ("[mfaa:六位提交号]", "[mfaa:9aa52]", "[mfaa:9aa52b]\n[mfaa:85e032]"):
            with self.subTest(tag_message=tag_message):
                self.assert_selects("", "Merge:'fix/x'| y\n\n[mfaa:7ff8c6]", tag_message, warned=True)


def pushed(message):
    return resolve_mfaa_commit("push", {"head_commit": {"message": message}})


class MfaaCommitMarkerChecks(unittest.TestCase):
    def test_marker_line_selects_fork_build_anywhere_in_message(self):
        cases = {
            "fix: x\n\n[mfaa:9aa52b]\n[deploy-beta]": "9aa52b",
            "fix: x\n\n[mfaa:9AA52B]": "9aa52b",
            "fix: x\n\n  [mfaa:00000a]  \r\n[deploy-alpha]": "00000a",
            "fix: x\n\n[mfaa:9aa52b] [deploy-beta]": "9aa52b",
            "fix: x\n\n[MFAA:9aa52b]": "9aa52b",
            "fix: x\n\n[mfaa:9aa52b]\nbody\n[mfaa:9aa52b]": "9aa52b",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(pushed(message), expected)

    def test_no_marker_keeps_requirements_client(self):
        for message in ("", "fix: x", "fix: x\n\n[deploy-beta]", "docs: 说明 [mfaa:9aa52b] 标记的写法",
                        "fix: x\n\n#[mfaa:9aa52b]\n[deploy-beta]", "fix: x\n\nmfaa:9aa52b"):
            with self.subTest(message=message):
                self.assertEqual(pushed(message), "")

    def test_malformed_or_conflicting_markers_fail_instead_of_falling_back(self):
        for message in ("[mfaa:六位提交号]\n[deploy-beta]", "[mfaa:]", "[mfaa:9aa52]", "[mfaa:9aa52b0]",
                        "[mfaa:9aa52g]", "[mfaa: 9aa52b]", "[mfaa:9aa52b", "[mfaa:9aa52b;id]",
                        "fix: x\n\n[mfaa:9aa52b]\n[mfaa:7ff8c6]\n[deploy-beta]"):
            with self.subTest(message=message), self.assertRaises(ValueError):
                pushed(message)

    def test_tag_message_marker_never_fails(self):
        self.assertEqual(tag_mfaa_commit("[mfaa:9AA52B]"), ("9aa52b", ""))
        self.assertEqual(tag_mfaa_commit(""), ("", ""))
        self.assertEqual(tag_mfaa_commit("正式版说明"), ("", ""))
        for message in ("[mfaa:六位提交号]", "[mfaa:]", "[mfaa:9aa52b]\n[mfaa:85e032]"):
            with self.subTest(message=message):
                commit, warning = tag_mfaa_commit(message)
                self.assertEqual(commit, "")
                self.assertIn("requirements.txt", warning)
                self.assertNotIn("\n", warning)

    def test_marker_does_not_disturb_last_line_channel_rule(self):
        self.assertEqual(push("fix: x\n\n[mfaa:9aa52b]\n[deploy-beta]")["version_type"], "beta")
        self.assertEqual(push("fix: x\n\n[deploy-beta]\n[mfaa:9aa52b]")["version_type"], "ci")

    def test_manual_input_and_other_events(self):
        self.assertEqual(resolve_mfaa_commit("workflow_dispatch", {"inputs": {"mfaa_commit": " 7FF8C6 "}}), "7ff8c6")
        self.assertEqual(resolve_mfaa_commit("workflow_dispatch", {"inputs": {"mfaa_commit": ""}}), "")
        self.assertEqual(resolve_mfaa_commit("workflow_dispatch", {"inputs": {}}), "")
        for bad in ("abcde", "abcdef0", "abcdef\nversion_type=release"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                resolve_mfaa_commit("workflow_dispatch", {"inputs": {"mfaa_commit": bad}})
        # Manual runs ignore commit text; PR builds never package a fork client.
        event = {"inputs": {}, "head_commit": {"message": "[mfaa:9aa52b]"}}
        self.assertEqual(resolve_mfaa_commit("workflow_dispatch", event), "")
        self.assertEqual(resolve_mfaa_commit("pull_request", event), "")


if __name__ == "__main__":
    unittest.main()
