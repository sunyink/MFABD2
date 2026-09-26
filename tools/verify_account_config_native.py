"""Native account-boundary probe over real Agent IPC, using no game/device.

python tools/verify_account_config_native.py --maa-dir <site-packages> --work-dir <output>
Tasker event sinks are intentionally not registered: selection cannot rely on them.
"""
import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def write_config(folder, number):
    directory = folder / "application/config/instances"
    directory.mkdir(parents=True, exist_ok=True)
    document = {"TaskItems": [{"entry": "Env_MultiSave_Config", "default_check": False,
                              "option": [{"name": "启用多存档", "index": 0, "sub_options": [
                                  {"name": "存档名称", "data": {"账号多开配置": number}}]}]}]}
    (directory / "A.json").write_text(json.dumps(document), encoding="utf-8")


def serve(identifier, folder):
    from maa.agent.agent_server import AgentServer
    from maa.custom_action import CustomAction
    from maa.custom_recognition import CustomRecognition
    from utils.account_sync import AccountSession, configure_account_session
    from utils.persistent_store import PersistentStore
    from utils.runtime_environment import StoragePolicy

    PersistentStore.configure_storage(StoragePolicy(folder / "saves"))
    session = AccountSession(folder / "application", "A")
    configure_account_session(folder / "application", instance_id="A")
    # Load the real four callback entry points, without unrelated action imports.
    for name in ("cartridge_lib", "account_save_checker"):
        spec = importlib.util.spec_from_file_location(name, ROOT / "agent/action" / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

    events = []

    def record(context, name):
        events.append({"name": name, "root": context.get_task_job().job_id})
        (folder / "events.json").write_text(json.dumps(events), encoding="utf-8")

    def stop(context):
        result = context.run_action("Env_AccountUnavailable_Stop")
        assert result is not None and result.success

    @AgentServer.custom_action("test_nested")
    class Nested(CustomAction):
        def run(self, context, argv):
            record(context, "parent")
            result = context.run_task("test_child")
            assert result and result.status.succeeded
            record(context, "parent_after")
            return True

    @AgentServer.custom_action("test_record")
    class Record(CustomAction):
        def run(self, context, argv):
            record(context, argv.node_name)
            return True

    @AgentServer.custom_action("test_stop_action")
    class StopAction(CustomAction):
        def run(self, context, argv):
            record(context, "stop_action")
            stop(context)
            return False

    @AgentServer.custom_recognition("test_stop_reco")
    class StopRecognition(CustomRecognition):
        def analyze(self, context, argv):
            record(context, "stop_reco")
            stop(context)
            return None

    @AgentServer.custom_action("test_account")
    class Account(CustomAction):
        def run(self, context, argv):
            if not session.sync(context, where=argv.node_name):
                record(context, "account_blocked")
                return False
            params = json.loads(argv.custom_action_param)
            assert PersistentStore._current_account_id == params["expected"]
            assert PersistentStore.set("last_task", context.get_task_job().job_id)
            record(context, "account_" + params["expected"])
            if params.get("edit"):
                write_config(folder, "2")
                child = context.run_task("test_read_account", {
                    "test_read_account": {"custom_action_param": {"expected": "1"}}})
                assert child and child.status.succeeded
                assert PersistentStore._current_account_id == "1"
            return True

    @AgentServer.custom_recognition("test_account_reco")
    class AccountRecognition(CustomRecognition):
        def analyze(self, context, argv):
            assert not session.sync(context, where="test_account_reco")
            record(context, "account_reco_blocked")
            return None

    assert AgentServer.start_up(identifier)
    AgentServer.join()
    AgentServer.shut_down()


def run(args):
    import numpy as np
    from maa.agent_client import AgentClient
    from maa.controller import CustomController
    from maa.resource import Resource
    from maa.tasker import Tasker
    from maa.library import Library

    class Synthetic(CustomController):
        def connect(self): return True
        def request_uuid(self): return "account-config-probe"
        def get_features(self): return 0
        def screencap(self): return np.zeros((720, 1280, 3), dtype=np.uint8)
        def click(self, *args): raise AssertionError("Unexpected real-input path")
        def swipe(self, *args): return False
        def touch_down(self, *args): return False
        def touch_move(self, *args): return False
        def touch_up(self, *args): return False
        def click_key(self, *args): return False
        def input_text(self, *args): return False
        def key_down(self, *args): return False
        def key_up(self, *args): return False
        def start_app(self, *args): return False
        def stop_app(self, *args): return False

    folder = args.work_dir.resolve()
    (folder / "resource/pipeline").mkdir(parents=True, exist_ok=True)
    common = {"pre_delay": 0, "post_delay": 0, "rate_limit": 0}
    nodes = {
        "Env_AccountUnavailable_Stop": {"action": "StopTask"},
        "test_parent": {"action": "Custom", "custom_action": "test_nested"},
        "test_child": {"action": "Custom", "custom_action": "test_record"},
        "test_bad_action": {"action": "Custom", "custom_action": "test_stop_action",
                            "next": ["test_forbidden"], "on_error": ["test_forbidden"]},
        "test_bad_reco": {"recognition": "Custom", "custom_recognition": "test_stop_reco",
                          "next": ["test_forbidden"], "on_error": ["test_forbidden"]},
        "test_bad_reco_list": {"next": ["test_bad_reco", "test_forbidden"]},
        "test_forbidden": {"action": "Custom", "custom_action": "test_record"},
        "test_next_queue_task": {"action": "Custom", "custom_action": "test_record"},
        "test_read_account": {"action": "Custom", "custom_action": "test_account",
                              "custom_action_param": {"expected": "1"},
                              "on_error": ["test_forbidden"]},
        "test_invalid_account_reco": {"next": ["test_account_reco", "test_forbidden"]},
        "test_account_reco": {"recognition": "Custom", "custom_recognition": "test_account_reco"},
        "test_real_cooldown": {"recognition": "Custom", "custom_recognition": "CheckCoolDown",
                               "custom_recognition_param": {"card_name": "native_probe", "cycle_type": "g_daily"}},
        "test_real_reco_list": {"next": ["test_real_cooldown", "test_forbidden"]},
    }
    production = json.loads((ROOT / "assets/resource/base/pipeline/Dummy.json").read_text(encoding="utf-8"))
    if "Env_AccountUnavailable_Stop" in production:
        nodes["Env_AccountUnavailable_Stop"] = production["Env_AccountUnavailable_Stop"]
    for action in ("CheckCoolDown", "MarkComplete", "SwitchAccountCheckpoint"):
        nodes["test_real_" + action] = {
            "action": "Custom", "custom_action": action,
            "custom_action_param": {"card_name": "native_probe", "cycle_type": "g_daily"},
            "next": ["test_forbidden"], "on_error": ["test_forbidden"]}
    (folder / "resource/pipeline/test.json").write_text(
        json.dumps({k: {**common, **v} for k, v in nodes.items()}), encoding="utf-8")
    Tasker.set_log_dir(folder / "log")
    resource, controller, tasker = Resource(), Synthetic(), Tasker()
    assert resource.post_bundle(folder / "resource").wait().succeeded
    assert controller.post_connection().wait().succeeded
    assert tasker.bind(resource, controller)
    client = AgentClient()
    assert client.bind(resource)
    assert client.set_timeout(10000)
    command = [sys.executable, "-B", "-X", "utf8=1", str(Path(__file__).resolve()),
               "--maa-dir", str(args.maa_dir), "--work-dir", str(folder), "--agent", client.identifier]
    with (folder / "agent.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            assert client.connect()
            jobs = []
            for entry in ("test_parent", "test_bad_action", "test_next_queue_task",
                          "test_bad_reco_list", "test_next_queue_task"):
                job = tasker.post_task(entry).wait()
                jobs.append({"entry": entry, "id": job.job_id, "succeeded": job.status.succeeded})
            events = json.loads((folder / "events.json").read_text(encoding="utf-8"))
            assert [e["name"] for e in events] == [
                "parent", "test_child", "parent_after", "stop_action",
                "test_next_queue_task", "stop_reco", "test_next_queue_task"], events
            assert {e["root"] for e in events[:3]} == {jobs[0]["id"]}, events
            assert [e["root"] for e in events[3:]] == [j["id"] for j in jobs[1:]], events
            # Exercise the production selector and store over the same real IPC.
            write_config(folder, "1")
            first = tasker.post_task("test_read_account", {
                "test_read_account": {"custom_action_param": {"expected": "1", "edit": True}}}).wait()
            assert first.status.succeeded
            second = tasker.post_task("test_read_account", {
                "test_read_account": {"custom_action_param": {"expected": "2"}}}).wait()
            assert second.status.succeeded
            before = {p.name: p.read_bytes() for p in (folder / "saves").iterdir()}
            write_config(folder, "invalid")
            tasker.post_task("test_read_account").wait()
            tasker.post_task("test_invalid_account_reco").wait()
            for entry in ("test_real_reco_list", "test_real_CheckCoolDown", "test_real_MarkComplete",
                          "test_real_SwitchAccountCheckpoint"):
                tasker.post_task(entry).wait()
            assert {p.name: p.read_bytes() for p in (folder / "saves").iterdir()} == before
            write_config(folder, "0")
            assert tasker.post_task("test_read_account", {
                "test_read_account": {"custom_action_param": {"expected": "0"}}}).wait().status.succeeded
            events = json.loads((folder / "events.json").read_text(encoding="utf-8"))
            assert [e["name"] for e in events[7:]] == [
                "account_1", "account_1", "account_2", "account_blocked", "account_reco_blocked", "account_0"], events
            assert events[7]["root"] == events[8]["root"] == first.job_id
            report = {"version": Library.version(), "tasker_events_registered": False,
                      "jobs": jobs, "events": events, "forbidden_nodes_executed": 0,
                      "production_selector_checked": True, "blocked_files_unchanged": True}
            report["production_callbacks_checked"] = 4
            (folder / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(report))
        finally:
            client.disconnect()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    assert process.returncode == 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--maa-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--agent")
    args = parser.parse_args()
    sys.path[:0] = [str(args.maa_dir), str(ROOT / "agent")]
    if args.agent:
        import maa.agent
        serve(args.agent, args.work_dir)
    else:
        run(args)


if __name__ == "__main__":
    main()
