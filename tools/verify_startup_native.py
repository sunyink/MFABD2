"""Manual native Maa/Agent transport test using a synthetic controller only.

Usage: python tools/verify_startup_native.py --maa-dir <isolated site-packages>
       --work-dir <scratch output>
No ADB connection, real screenshot, real input or game process is used.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def agent(identifier, mode):
    from maa.agent.agent_server import AgentServer
    from maa.custom_action import CustomAction
    from maa.controller import Controller
    from startup.common import Budget

    # Match main.py: existing custom registrations initialize v5.12.2 bindings
    # before the context-sink decorator runs.
    @AgentServer.custom_action("test_initialize_bindings")
    class Initialize(CustomAction):
        def run(self, context, argv):
            return True

    from startup.sink import guard

    original_info = Controller.info.fget

    def synthetic_adb_info(self):
        info = original_info(self)
        # The real transport is used; only the synthetic controller's routing
        # identity is changed so the production ADB guard can be exercised.
        if self.uuid == "startup-test":
            return dict(info, type="adb")
        return info

    Controller.info = property(synthetic_adb_info)
    if mode == "failure":
        guard.budget_factory = lambda report, cancelled: Budget(report, cancelled, timeout=0.5)
    assert AgentServer.start_up(identifier)
    AgentServer.join()
    AgentServer.shut_down()


def run(mode, work, maa_dir):
    import numpy as np
    from maa.agent_client import AgentClient
    from maa.controller import CustomController
    from maa.custom_recognition import CustomRecognition
    from maa.resource import Resource
    from maa.tasker import Tasker

    class Synthetic(CustomController):
        def __init__(self):
            self.commands = []
            self.frames = self.clicks = 0
            self.foreground = False
            super().__init__()

        def connect(self): return True
        def request_uuid(self): return "startup-test"
        def get_features(self): return 0
        def start_app(self, intent): raise AssertionError("Unexpected StartApp")
        def stop_app(self, intent): raise AssertionError("Unexpected StopApp")
        def swipe(self, *args): return False
        def touch_down(self, *args): return False
        def touch_move(self, *args): return False
        def touch_up(self, *args): return False
        def click_key(self, *args): return False
        def input_text(self, *args): return False
        def key_down(self, *args): return False
        def key_up(self, *args): return False

        def screencap(self):
            assert self.foreground, "Business screenshot ran before preparation"
            self.frames += 1
            return np.zeros((720, 1280, 3), dtype=np.uint8)

        def click(self, x, y):
            assert self.foreground, "Business input ran before preparation"
            self.clicks += 1
            return True

        def shell(self, cmd, timeout):
            assert 0 < timeout <= 5000
            self.commands.append(cmd)
            if mode == "failure":
                return "Permission Denial: test fixture"
            if cmd.startswith("dumpsys"):
                package = "com.neowizgames.game.browndust2" if self.foreground else "com.android.launcher"
                return f"mCurrentFocus=Window{{123 u0 {package}/.Main}}"
            if cmd.startswith("cmd package"):
                return "com.neowizgames.game.browndust2/.Main"
            if cmd.startswith("am start"):
                self.foreground = True
                return "Starting: Intent"
            raise AssertionError(cmd)

    folder = work / mode
    (folder / "resource/pipeline").mkdir(parents=True, exist_ok=True)
    nodes = {
        "test_entry": {"action": "Click", "target": [1, 1], "pre_delay": 0, "post_delay": 0, "next": ["test_next"]},
        "test_next": {"action": "Click", "target": [1, 1], "pre_delay": 0, "post_delay": 0},
    }
    if mode == "node_timeout":
        nodes["test_entry"] = {"recognition": "Custom", "custom_recognition": "test_never",
                               "timeout": 100, "rate_limit": 0, "on_error": ["test_next"]}
    (folder / "resource/pipeline/test.json").write_text(json.dumps(nodes), encoding="utf-8")
    Tasker.set_log_dir(folder / "log")
    resource, controller, tasker = Resource(), Synthetic(), Tasker()
    class Never(CustomRecognition):
        count = 0

        def analyze(self, context, argv):
            self.count += 1
            return None

    never = Never()
    assert resource.register_custom_recognition("test_never", never)
    assert resource.post_bundle(folder / "resource").wait().succeeded
    assert controller.post_connection().wait().succeeded
    assert tasker.bind(resource, controller)
    client = AgentClient()
    assert client.bind(resource)
    assert client.register_sink(resource, controller, tasker)
    assert client.set_timeout(10000)
    command = [sys.executable, "-B", "-X", "utf8=1", str(Path(__file__).resolve()),
               "--maa-dir", str(maa_dir), "--work-dir", str(work), "--agent", client.identifier, "--mode", mode]
    with (folder / "agent.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            assert client.connect()
            statuses = []
            for index in range(1 if mode == "node_timeout" else 2):
                started = time.monotonic()
                job = tasker.post_task("test_entry")
                while not job.done and time.monotonic() - started < 15:
                    time.sleep(0.02)
                assert job.done, "Native task did not complete"
                statuses.append(dict(succeeded=job.succeeded, failed=job.failed))
                if mode == "failure":
                    assert controller.frames == controller.clicks == 0
                    if index == 0:
                        first_commands = len(controller.commands)
                    else:
                        assert len(controller.commands) == first_commands, "Failure was not latched"
                        assert time.monotonic() - started < 2
                elif mode == "node_timeout":
                    assert never.count == 1, "Preparation time no longer charges the original recognition timeout"
                    assert controller.clicks == 1
                    assert time.monotonic() - started >= 2
                else:
                    assert controller.clicks == (index + 1) * 2
                    assert len(controller.commands) == 4 + index, controller.commands
            report = dict(mode=mode, frames=controller.frames, clicks=controller.clicks,
                          commands=controller.commands, statuses=statuses, misses=never.count)
            (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False))
        finally:
            client.disconnect()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()  # Only this test's own synthetic agent process.
                process.wait(timeout=5)
    assert process.returncode == 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--maa-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--agent")
    parser.add_argument("--mode", choices=("success", "failure", "node_timeout"))
    args = parser.parse_args()
    sys.path[:0] = [str(args.maa_dir), str(ROOT / "agent")]
    if args.agent:
        import maa.agent  # Select server mode before the first Library call.
    from maa.library import Library

    print("MaaFramework:", Library.version(), flush=True)
    assert Library.version() == "v5.12.2", "Use the project's pinned desktop runtime"
    if args.agent:
        agent(args.agent, args.mode)
    else:
        for mode in ("success", "failure", "node_timeout"):
            run(mode, args.work_dir, args.maa_dir)


if __name__ == "__main__":
    main()
