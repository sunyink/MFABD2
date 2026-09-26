"""Read the account choice: MFAA's saved switch/input, or a task-local injection.

MFAA shares global options across instances, so its choice comes only from the
instance file. Clients that keep global options per task (VS Code MaaSupport)
receive the source interface's switch, injected into INJECT_NODE for each task.
"""
from dataclasses import dataclass
import json
from pathlib import Path
import re
import time


TASK_ENTRY = "Env_MultiSave_Config"
SWITCH_OPTION = "启用多存档"
ACCOUNT_OPTION = "存档名称"
ACCOUNT_INPUT = "账号多开配置"
STOP_ENTRY = "Env_AccountUnavailable_Stop"
INJECT_NODE = "Agt_MultiSave_Inject"


@dataclass(frozen=True)
class AccountSelection:
    account_id: str | None = None
    enabled: bool | None = None
    reason: str = ""

    @property
    def valid(self) -> bool:
        return self.account_id is not None and not self.reason


class _InvalidConfig(ValueError):
    pass


def _named(items, name):
    if not isinstance(items, list):
        raise _InvalidConfig("选项列表缺失或格式错误")
    matches = [item for item in items if isinstance(item, dict) and item.get("name") == name]
    if not matches:
        raise _InvalidConfig(f"缺少选项：{name}")
    return matches


def _agree(values):
    if any(value != values[0] for value in values[1:]):
        raise _InvalidConfig("重复的多存档配置互相冲突，请只保留一份设置")
    return values[0]


def _parse_switch(option):
    index = option.get("index")
    # MFAA initializes task option indexes to 0, so keep cases ordered Yes, No.
    # A bool is not a saved case index.
    if type(index) is not int or index not in (0, 1):
        raise _InvalidConfig("启用多存档开关未保存或格式错误")
    if index == 1:
        return AccountSelection("0", False)
    choices = []
    for child in _named(option.get("sub_options"), ACCOUNT_OPTION):
        data = child.get("data")
        value = data.get(ACCOUNT_INPUT) if isinstance(data, dict) else None
        if not isinstance(value, str) or re.fullmatch(r"[0-9]+", value) is None:
            raise _InvalidConfig("存档号必须是纯数字；默认档请填 0，或关闭启用多存档")
        choices.append(AccountSelection(value, True))
    return _agree(choices)


def parse_instance_account(document) -> AccountSelection:
    try:
        if not isinstance(document, dict) or not isinstance(document.get("TaskItems"), list):
            raise _InvalidConfig("实例配置缺少 TaskItems")
        tasks = [task for task in document["TaskItems"]
                 if isinstance(task, dict) and task.get("entry") == TASK_ENTRY]
        if not tasks:
            raise _InvalidConfig("找不到多存档设置任务，请重新添加；取消任务勾选不影响设置")
        choices = []
        for task in tasks:
            # Never consult default_check, task names or displayed labels.
            choices.extend(_parse_switch(option) for option in _named(task.get("option"), SWITCH_OPTION))
        return _agree(choices)
    except _InvalidConfig as exc:
        return AccountSelection(reason=str(exc))


def parse_injected_account(attach) -> AccountSelection:
    # The release interface injects nothing, so an empty value means this client
    # has neither MFAA's instance file nor the source switch. Never default to 0.
    if not isinstance(attach, dict):
        return AccountSelection(reason=f"{INJECT_NODE} 的 attach 不是对象，请更新 base 资源")
    value = attach.get("account_id")
    if value is None or value == "":
        return AccountSelection(reason="本任务没有下发存档号：当前客户端不是 MFAA，也未提供全局「启用多存档」设置")
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+", value) is None:
        return AccountSelection(reason="存档号必须是纯数字；默认档请填 0，或关闭启用多存档")
    return AccountSelection(value)


def read_instance_account(project_root: Path, instance_id: str, *, attempts=4, delay=0.1) -> AccountSelection:
    if not isinstance(instance_id, str) or not instance_id:
        return AccountSelection(reason="MFAA 未提供实例 ID，无法读取本实例存档设置")
    if instance_id in (".", "..") or re.search(r'[\\/:<>"|?*\x00-\x1f]', instance_id):
        return AccountSelection(reason="实例 ID 含非法路径字符")
    try:
        directory = (Path(project_root) / "config" / "instances").resolve()
        path = (directory / f"{instance_id}.json").resolve()
        if path.parent != directory:
            return AccountSelection(reason="实例配置路径超出配置目录")
    except (OSError, ValueError, RuntimeError):
        return AccountSelection(reason="无法定位实例配置路径")
    for attempt in range(attempts):
        try:
            before = path.stat()
            document = json.loads(path.read_text(encoding="utf-8-sig"))
            after = path.stat()
            if (before.st_ino, before.st_mtime_ns, before.st_size) != (
                    after.st_ino, after.st_mtime_ns, after.st_size):
                raise OSError("实例配置正在保存")
            return parse_instance_account(document)
        except (OSError, UnicodeError, ValueError):
            if attempt + 1 < attempts:
                time.sleep(delay)
    return AccountSelection(reason="实例配置暂不可读或保存不完整；本任务不沿用旧存档号")
