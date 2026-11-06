"""Contract tests for scripts/release_interface.py: what each target's interface.json becomes.

Runs on the build jobs' Python 3.10 as well as 3.11, with the standard library only.
"""

import io
import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import release_interface as ri

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ("win", "macos", "linux")


def source():
    return ri.load_source(ri.SOURCE)


class TargetTest(unittest.TestCase):
    def test_target_names_are_normalised(self):
        for raw, platform in (("win", "win"), ("win-x64", "win"), ("Windows", "win"), ("macos", "macos"),
                              ("darwin", "macos"), ("osx-arm64", "macos"), ("linux-x64", "linux"),
                              ("android", "android"), ("android-arm64", "android")):
            with self.subTest(raw=raw):
                self.assertEqual(ri.release_target(raw), platform)

    def test_unknown_target_is_rejected_instead_of_guessed(self):
        # The old installer sent any unrecognised name down the Linux branch.
        for raw in ("mac", "ios", ""):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                ri.release_target(raw)


class RealSourceTest(unittest.TestCase):
    def setUp(self):
        self.source = source()
        # An empty package directory: no portable Python, so macOS resolves to system python3
        # however the developer's own install/ looks.
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.install = Path(temporary.name)

    def build(self, target, document=None, version="v0.0.0"):
        return ri.build(self.source if document is None else document,
                        ri.target_for(target, version, self.install))

    def test_source_is_not_mutated_and_desktop_targets_are_idempotent(self):
        before = deepcopy(self.source)
        for target in (*DESKTOP, "android"):
            with self.subTest(target=target):
                release = self.build(target)
                self.assertEqual(self.source, before)
                if target in DESKTOP:
                    self.assertEqual(self.build(target, release), release)

    def test_desktop_targets_differ_only_in_agent_and_pretask(self):
        # MirrorChyan's hot-update package carries the win-x86_64 interface.json to every desktop
        # platform, so this is the whole platform difference. Widening it needs 发布与CI.md §二 too.
        releases = {target: self.build(target) for target in DESKTOP}
        stripped = [{key: value for key, value in release.items() if key not in ("agent", "pretask")}
                    for release in releases.values()]
        self.assertTrue(all(item == stripped[0] for item in stripped))
        self.assertEqual({target: release["agent"]["child_exec"] for target, release in releases.items()},
                         {"win": "{PROJECT_DIR}/python/python.exe", "macos": "python3", "linux": "python3"})

    def test_desktop_targets_relocate_multi_save_and_pc_window(self):
        # The source keeps these global for per-task clients; MFAA gets the switch inside its task
        # and the PC window options on the controllers they name.
        expected = deepcopy(self.source)
        expected["global_option"].remove("启用多存档")
        for name in ri.PC_WINDOW_OPTIONS:
            expected["global_option"].remove(name)
        controllers = {item["name"]: item for item in expected["controller"]}
        controllers["PC客户端"]["option"] = ["PC窗口分辨率", "PC启动最小化"]
        controllers["PC客户端(Cursor)"]["option"] = ["PC窗口分辨率"]
        next(task for task in expected["task"] if task["entry"] == ri.MULTI_SAVE_ENTRY)["option"] = ["启用多存档"]
        del expected["option"]["存档名称"]["pipeline_override"]
        del next(case for case in expected["option"]["启用多存档"]["cases"] if case["name"] == "No")["pipeline_override"]
        for target in DESKTOP:
            with self.subTest(target=target):
                release = self.build(target)
                for key in ("global_option", "controller", "task", "option", "resource", "preset"):
                    self.assertEqual(release[key], expected[key], key)
                self.assertNotIn(ri.MULTI_SAVE_INJECT_NODE, json.dumps(release, ensure_ascii=False))

    def test_android_keeps_only_adb_and_adb_resource(self):
        filtered = self.build("android-arm64")
        self.assertEqual([item["name"] for item in filtered["controller"]], ["Adb"])
        self.assertNotIn("option", filtered["controller"][0])
        self.assertEqual([item["name"] for item in filtered["resource"]], ["ADB"])
        self.assertEqual(filtered["resource"][0]["path"], ["./resource/base", ri.ANDROID_NATIVE_LAYER])
        self.assertNotIn("mirrorchyan_rid", filtered)
        expected_tasks = [task for task in self.source["task"]
                          if task.get("entry") != ri.MULTI_SAVE_ENTRY
                          and (not task.get("controller") or "Adb" in task["controller"])]
        self.assertEqual(filtered["task"], expected_tasks)
        for name in ri.ANDROID_ACCOUNT_OPTIONS:
            self.assertNotIn(name, filtered["option"])
            self.assertNotIn(name, filtered.get("global_option", []))
        self.assertFalse(set(ri.PC_WINDOW_OPTIONS) & set(filtered["global_option"]))
        self.assertTrue(all(task["name"] != "多存档"
                            for preset in filtered["preset"] for task in preset["task"]))

    def test_android_task_and_preset_references_follow_controller_filter(self):
        document = deepcopy(self.source)
        document["task"].append({"name": "pc-only", "controller": ["PC客户端"]})
        document["preset"][0]["task"].append({"name": "pc-only"})
        filtered = self.build("android", document)
        self.assertNotIn("pc-only", [task["name"] for task in filtered["task"]])
        self.assertNotIn("pc-only", [task["name"] for task in filtered["preset"][0]["task"]])

    def test_pc_pretask_is_not_shipped_to_android(self):
        self.assertTrue(self.source["pretask"])
        self.assertEqual(self.build("android").get("pretask"), [])
        document = deepcopy(self.source)
        document["pretask"].extend([
            {"name": "shared", "exec": "test"},
            {"name": "adb-only", "exec": "test", "controller": ["Adb"]},
        ])
        self.assertEqual([task["name"] for task in self.build("android", document)["pretask"]],
                         ["shared", "adb-only"])

    def test_version_is_stamped_into_field_and_title(self):
        release = self.build("win", version="v9.9.9")
        self.assertEqual(release["version"], "v9.9.9")
        # Exactly the source's spacing around the version; it used to gain a space per run.
        self.assertEqual(release["title"], self.source["title"].replace(self.source["version"], "v9.9.9"))
        self.assertIn("MFABD2)v9.9.9 | 游戏版本：", release["title"])

    def test_title_without_a_version_slot_fails(self):
        document = deepcopy(self.source)
        document["title"] = document["title"].replace("游戏版本：", "游戏版本:")
        with self.assertRaisesRegex(ValueError, "version slot"):
            self.build("win", document)

    def test_relocation_rejects_broken_pc_window_sources(self):
        undefined = deepcopy(self.source)
        del undefined["option"]["PC启动最小化"]
        unowned = deepcopy(self.source)
        del unowned["option"]["PC窗口分辨率"]["controller"]
        foreign = deepcopy(self.source)
        foreign["option"]["PC窗口分辨率"]["controller"].append("Adb")
        lost = deepcopy(self.source)
        lost["global_option"].remove("PC窗口分辨率")
        for document in (undefined, unowned, foreign, lost):
            with self.assertRaises(ValueError):
                ri.relocate_pc_window_options(document)

    def test_relocation_rejects_ambiguous_multi_save_sources(self):
        doubled = deepcopy(self.source)
        doubled["task"].append(deepcopy(next(t for t in doubled["task"] if t["entry"] == ri.MULTI_SAVE_ENTRY)))
        owned = deepcopy(self.source)
        next(t for t in owned["task"] if t["entry"] == ri.MULTI_SAVE_ENTRY)["option"] = ["前置助手"]
        lost = deepcopy(self.source)
        lost["global_option"].remove(ri.MULTI_SAVE_SWITCH)
        undefined = deepcopy(self.source)
        del undefined["option"]["存档名称"]
        for document in (doubled, owned, lost, undefined):
            with self.assertRaises(ValueError):
                ri.relocate_multi_save_switch(document)
        bare = {"global_option": ["前置助手"], "task": [{"name": "x", "entry": "y"}]}
        ri.relocate_multi_save_switch(bare)
        self.assertEqual(bare, {"global_option": ["前置助手"], "task": [{"name": "x", "entry": "y"}]})


class AndroidNarrowingTest(unittest.TestCase):
    def build(self, document):
        return ri.build(document, ri.target_for("android", install=Path(tempfile.gettempdir())))

    def test_unrestricted_resources_are_kept(self):
        document = {"controller": [{"name": "A", "type": "Adb"}],
                    "resource": [{"name": "all", "path": ["."]}, {"name": "a", "path": ["."], "controller": ["A"]},
                                 {"name": "b", "path": ["."], "controller": ["B"]}]}
        self.assertEqual([item["name"] for item in self.build(document)["resource"]], ["all", "a"])

    def test_missing_adb_fails(self):
        with self.assertRaises(ValueError):
            self.build({"controller": [{"name": "PC", "type": "Win32"}], "resource": [{"name": "x"}]})

    def test_empty_result_fails(self):
        with self.assertRaises(ValueError):
            self.build({"controller": [{"name": "A", "type": "Adb"}],
                        "resource": [{"name": "b", "path": ["."], "controller": ["B"]}]})

    def test_resource_without_a_base_layer_fails(self):
        """An overlay-only resource loads fine and then has almost no nodes."""
        for resource in ({"name": "x"}, {"name": "x", "path": []}):
            with self.assertRaises(ValueError):
                self.build({"controller": [{"name": "A", "type": "Adb"}], "resource": [resource]})


class InstallTest(unittest.TestCase):
    """install(): one write into a package directory, then read-back checks."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "install"
        (self.output / "agent").mkdir(parents=True)
        (self.output / "agent" / ri.PRETASK_SCRIPT).write_text("# fixture", encoding="utf-8")

    def install(self, document, target, tamper=None, version="v0.0.1"):
        path = Path(self.temporary.name) / "interface.json"
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        original = ri.dumps

        def dumps(value):
            value = deepcopy(value)
            if tamper is not None:
                tamper(value)
            return original(value)

        with patch.object(ri, "dumps", side_effect=dumps), patch("sys.stdout", new_callable=io.StringIO):
            return ri.install(self.output, target, version, path)

    def test_windows_install_rewrites_pretask_paths(self):
        document = source()
        pretask = self.install(document, "win-x64")["pretask"][0]
        self.assertEqual(pretask["exec"], ri.PRETASK_EXEC)
        # 脚本不再按相对路径给出，改为 -c 载荷从 sys.executable 反推安装根。
        self.assertEqual(pretask["args"][-2:], ["-c", ri.PRETASK_LAUNCH])
        self.assertFalse([arg for arg in pretask["args"] if Path(arg.replace("\\", "/")).name == ri.PRETASK_SCRIPT])
        # exec 必须落在选定锚点下解析得到的嵌入式解释器上。
        interpreter = (ri.pretask_anchor(self.output) / pretask["exec"]).resolve()
        self.assertEqual(interpreter, (self.output / "python" / "python.exe").resolve())
        # 载荷的不变量：从解释器往上两级就是安装根，脚本必在其 agent/ 下。
        self.assertEqual(interpreter.parents[1] / "agent" / ri.PRETASK_SCRIPT,
                         (self.output / "agent" / ri.PRETASK_SCRIPT).resolve())
        # 源文件这两条路径按开发机布局写（resource/base 起跳到仓库根）。
        original = document["pretask"][0]
        self.assertEqual((ROOT / "assets/resource/base" / original["args"][-1]).resolve(), ROOT / "agent/pc_bootstrap.py")
        self.assertEqual((ROOT / "assets/resource/base" / original["exec"]).resolve(), ROOT / ".venv/Scripts/python.exe")

    def test_missing_pretask_interpreter_fails_once_python_is_unpacked(self):
        (self.output / "python").mkdir()
        with self.assertRaisesRegex(ValueError, "锚点下不存在"):
            self.install(source(), "win")
        (self.output / "python" / "python.exe").write_bytes(b"")
        self.install(source(), "win")

    def test_missing_bootstrap_script_fails(self):
        (self.output / "agent" / ri.PRETASK_SCRIPT).unlink()
        with self.assertRaisesRegex(ValueError, "pretask 必然起不来"):
            self.install(source(), "win")

    def test_renamed_bootstrap_preserves_args_and_matches_both_slashes(self):
        for script in ("../../../agent/pc_bootstrap.py", r"..\..\..\agent\pc_bootstrap.py"):
            with self.subTest(script=script):
                document = {
                    "controller": [{"name": "renamed-pc", "type": "Win32"}],
                    "pretask": [{"name": "renamed-bootstrap", "controller": ["renamed-pc"],
                                 "exec": "../../../.venv/Scripts/python.exe",
                                 "args": ["-B", "-u", script, "--custom-flag", "value"]}],
                }
                installed = self.install(document, "win-x64")
                # 一项换两项，其余 args 必须原样保留（含位置）。
                self.assertEqual(installed["pretask"], [
                    {**document["pretask"][0], "exec": ri.PRETASK_EXEC,
                     "args": ["-B", "-u", "-c", ri.PRETASK_LAUNCH, "--custom-flag", "value"]},
                ])

    def test_unrelated_pretask_venv_paths_fail_with_name(self):
        for field, value in (("exec", "../../../.venv/Scripts/python.exe"),
                             ("args", [r"..\.venv\other.py"])):
            with self.subTest(field=field):
                pretask = {"name": "unrelated-tool", "exec": "python3", "args": ["other.py"], field: value}
                with self.assertRaisesRegex(ValueError, r"pretask 'unrelated-tool'.*\.venv"):
                    self.install({"pretask": [pretask]}, "win-x64")

    def test_non_windows_drops_only_exclusively_win32_pretasks(self):
        document = {
            "controller": [{"name": "renamed-pc", "type": "Win32"},
                           {"name": "second-pc", "type": "Win32"},
                           {"name": "phone", "type": "Adb"},
                           {"name": "mac", "type": "PlayCover"}],
            "pretask": [
                {"name": "pc-only", "controller": ["renamed-pc"], "exec": ".venv/python"},
                {"name": "two-pcs", "controller": ["renamed-pc", "second-pc"], "exec": ".venv/python"},
                {"name": "shared", "exec": "python3", "args": ["shared.py"]},
                {"name": "empty", "controller": [], "exec": "python3"},
                {"name": "mixed-adb", "controller": ["renamed-pc", "phone"], "exec": "python3"},
                {"name": "mixed-other", "controller": ["renamed-pc", "mac"], "exec": "python3"},
                {"name": "other-only", "controller": ["mac"], "exec": "python3"},
            ],
        }
        for target in ("macos-arm64", "linux-x64"):
            with self.subTest(target=target):
                self.assertEqual(self.install(document, target)["pretask"], document["pretask"][2:])

    def test_readback_tampering_fails(self):
        def tamper(value):
            value["pretask"][0]["args"] = ["different.py"]

        with self.assertRaisesRegex(ValueError, "回读不符"):
            self.install({"pretask": [{"name": "shared", "exec": "python3", "args": ["shared.py"]}]},
                         "linux-x64", tamper)

    def test_macos_uses_the_portable_python_only_when_it_is_unpacked(self):
        self.assertEqual(self.install(source(), "macos")["agent"]["child_exec"], "python3")
        (self.output / "python" / "bin").mkdir(parents=True)
        (self.output / "python" / "bin" / "python3").write_bytes(b"")
        self.assertEqual(self.install(source(), "macos")["agent"]["child_exec"],
                         "{PROJECT_DIR}/python/bin/python3")

    def test_install_leaves_only_the_interface_behind(self):
        before = {path for path in self.output.rglob("*")}
        self.install(source(), "linux")
        self.assertEqual({path for path in self.output.rglob("*")} - before, {self.output / "interface.json"})


if __name__ == "__main__":
    unittest.main()
