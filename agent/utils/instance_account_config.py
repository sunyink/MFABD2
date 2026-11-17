"""Read the account choice: MFAA's saved switch/input, or a task-local injection.

MFAA shares global options across instances, so its choice comes only from the
instance file. Clients that keep global options per task (VS Code MaaSupport)
receive the source interface's switch, injected into INJECT_NODE for each task.

Older MFAA clients copied every default into the instance file. Newer ones store
only values that differ from the resource default; an unset value follows the
interface default, resolved the way MFAA resolves it. Without a readable
interface an unset value is never guessed.
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
# Saved indexes and names are read against these; the interface keeps the order
# Yes, No because old clients pre-populate index 0 (tools/fixtures/account_config).
CASES = ("Yes", "No")


@dataclass(frozen=True)
class AccountSelection:
    account_id: str | None = None
    enabled: bool | None = None
    reason: str = ""

    @property
    def valid(self) -> bool:
        return self.account_id is not None and not self.reason


@dataclass(frozen=True)
class OptionDefaults:
    """Interface defaults of the two account options."""
    cases: tuple
    switch: str | None  # None: the declared default_case is not one of the cases
    account: str


class _InvalidConfig(ValueError):
    pass


def defaults_from_interface(document) -> OptionDefaults | None:
    options = document.get("option") if isinstance(document, dict) else None
    switch = options.get(SWITCH_OPTION) if isinstance(options, dict) else None
    account = options.get(ACCOUNT_OPTION) if isinstance(options, dict) else None
    if not isinstance(switch, dict) or not isinstance(account, dict):
        return None
    cases = tuple(case.get("name") if isinstance(case, dict) else None for case in switch.get("cases") or [])
    if not cases or not all(isinstance(name, str) and name for name in cases) or len(set(cases)) != len(cases):
        return None
    declared = switch.get("default_case")
    if isinstance(declared, list):
        declared = declared[0] if declared else None
    # Like MFAA: an undeclared default is the first case, a declared one must exist.
    default = declared if declared else cases[0]
    fields = [field for field in account.get("inputs") or []
              if isinstance(field, dict) and field.get("name") == ACCOUNT_INPUT]
    if len(fields) != 1:
        return None
    value = fields[0].get("default")
    return OptionDefaults(cases, default if default in cases else None, "" if value is None else str(value))


def load_defaults(project_root: Path) -> OptionDefaults | None:
    try:
        document = json.loads((Path(project_root) / "interface.json").read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        return None
    return defaults_from_interface(document)


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


def _resolve(defaults):
    # Read lazily: the interface is only needed when a value is unset.
    return defaults() if callable(defaults) else defaults


def _switch_choice(option, defaults):
    selected = option.get("selected_case")
    if selected is not None:
        if not isinstance(selected, str) or selected not in CASES:
            raise _InvalidConfig("启用多存档的已存选择已不在资源中，请重新选择")
        return selected
    index = option.get("index")
    if index is not None:
        # A bool is not a saved case index.
        if type(index) is not int or not 0 <= index < len(CASES):
            raise _InvalidConfig("启用多存档开关未保存或格式错误")
        return CASES[index]
    resolved = _resolve(defaults)
    if resolved is None:
        raise _InvalidConfig("启用多存档开关未保存或格式错误")
    if resolved.switch is None:
        raise _InvalidConfig("资源中启用多存档的默认值无效")
    return resolved.switch


def _account_number(child, defaults):
    data = child.get("data")
    value = data.get(ACCOUNT_INPUT) if isinstance(data, dict) else None
    if value is None and (data is None or isinstance(data, dict)):
        resolved = _resolve(defaults)
        value = resolved.account if resolved is not None else None
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+", value) is None:
        raise _InvalidConfig("存档号必须是纯数字；默认档请填 0，或关闭启用多存档")
    return value


def _parse_switch(option, defaults):
    choice = _switch_choice(option, defaults)
    if choice == "No":
        return AccountSelection("0", False)
    if choice != "Yes":
        raise _InvalidConfig(f"无法识别的启用多存档选项：{choice}")
    children = option.get("sub_options")
    if children is None or (isinstance(children, list) and not any(
            isinstance(item, dict) and item.get("name") == ACCOUNT_OPTION for item in children)):
        if _resolve(defaults) is not None:
            children = [{"name": ACCOUNT_OPTION}]  # nothing saved: the number follows the default
    return _agree([AccountSelection(_account_number(child, defaults), True)
                   for child in _named(children, ACCOUNT_OPTION)])


def parse_instance_account(document, defaults=None) -> AccountSelection:
    """defaults: OptionDefaults, a callable returning them (read on first need), or None."""
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
            choices.extend(_parse_switch(option, defaults) for option in _named(task.get("option"), SWITCH_OPTION))
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
    cached = []

    def defaults():
        if not cached:
            cached.append(load_defaults(project_root))
        return cached[0]

    for attempt in range(attempts):
        try:
            before = path.stat()
            document = json.loads(path.read_text(encoding="utf-8-sig"))
            after = path.stat()
            if (before.st_ino, before.st_mtime_ns, before.st_size) != (
                    after.st_ino, after.st_mtime_ns, after.st_size):
                raise OSError("实例配置正在保存")
            return parse_instance_account(document, defaults)
        except (OSError, UnicodeError, ValueError):
            if attempt + 1 < attempts:
                time.sleep(delay)
    return AccountSelection(reason="实例配置暂不可读或保存不完整；本任务不沿用旧存档号")
