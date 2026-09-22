"""Verify the native startup overlay using MaaFramework's actual merge semantics."""

import json
import tempfile
from pathlib import Path

from maa.resource import Resource

ROOT = Path(__file__).resolve().parents[1]


def main():
    base = json.loads((ROOT / "assets/resource/base/pipeline/StartGame.json").read_text(encoding="utf-8"))
    overlay_dir = ROOT / "assets/resource/android_native"
    overlay = json.loads((overlay_dir / "pipeline/StartGame.json").read_text(encoding="utf-8"))
    names = ["StartGame_Check_App_Alive", "StartGame_RunApp"]
    assert set(overlay) == set(names), "Keep the native startup overlay scoped to its two nodes"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "pipeline").mkdir()
        nodes = {name: base[name] for name in names}
        # Only referenced names are stubbed; the two nodes themselves are real source.
        for name in names:
            for data in [base[name], overlay[name]]:
                for field in ["next", "on_error"]:
                    for ref in data.get(field, []):
                        nodes.setdefault(ref.replace("[JumpBack]", ""), {})
        (root / "pipeline/probe.json").write_text(json.dumps(nodes), encoding="utf-8")
        resource = Resource()
        assert resource.post_bundle(str(root)).wait().succeeded
        before = {name: resource.get_node_data(name) for name in names}
        assert resource.post_bundle(str(overlay_dir)).wait().succeeded
        check = resource.get_node_data(names[0])
        launch = resource.get_node_data(names[1])
        assert before[names[0]]["action"]["type"] == "Custom"
        assert before[names[0]]["action"]["param"]["custom_action"] == "StartupCheckApp"
        assert check["action"] == {"type": "DoNothing", "param": {}}
        assert check["focus"] is None
        assert [ref["name"] for ref in check["next"]] == [names[1]]
        assert not check["on_error"]
        assert before[names[1]]["action"]["param"]["custom_action"] == "StartupRunApp"
        assert launch["action"] == {"type": "StartApp", "param": {"package": "com.neowizgames.game.browndust2"}}
        assert launch["next"] == before[names[1]]["next"]
        assert launch["timeout"] == before[names[1]]["timeout"]
        assert [ref["name"] for ref in launch["on_error"]] == ["Global_Null_Exception", "Global_Null_Panic"]
    print("Native startup overlay verified: no Shell probe/fallback; base loading chain preserved")


if __name__ == "__main__":
    main()
