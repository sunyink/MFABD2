"""库存记账的三个开关：盘不盘、每次是否全盘、出不出报表。

选项挂在任务「⚙️ 库存记账」上（入口 Env_Ledger_Config），照多存档的做法——全局选项各实例共用，不能放那里：
- MFAA：直接读本实例配置里那个任务的选项。任务勾不勾、跑没跑都不影响；没存过的值跟 interface 的默认。
- 其他客户端：选项的覆盖只在那个任务里生效，任务跑到时由动作 LedgerConfig 把 Agt_Ledger_Inject.attach 记进内存；
  没跑过就按默认。
读不到一律按默认（盘、范围按规则、出报表），不因为配置读不成而拦任何流程。
"""

import json
from pathlib import Path
import re
import threading
import time


TASK_ENTRY = "Env_Ledger_Config"
INJECT_NODE = "Agt_Ledger_Inject"
# 键 → interface.json 里的选项名；_ON 是算「开」的那个 case 名。
OPTIONS = {"scan": "记账盘点", "full": "记账盘点范围", "report": "记账报表"}
_ON = {"scan": "Yes", "full": "每次全盘", "report": "Yes"}
DEFAULTS = {"scan": True, "full": False, "report": True}

_LOCK = threading.Lock()
_INJECTED: dict = {}      # 其他客户端：LedgerConfig 记下的选择
_LAST_GOOD: dict = {}     # MFAA：实例号 → 上一次读成的结果


def _cases(interface, name):
    """interface 里某个选项的 (case 名元组, 默认 case)；读不到返回 ((), None)。"""
    options = interface.get("option") if isinstance(interface, dict) else None
    option = options.get(name) if isinstance(options, dict) else None
    if not isinstance(option, dict):
        return (), None
    names = tuple(case.get("name") for case in option.get("cases") or [] if isinstance(case, dict))
    if not names or not all(isinstance(item, str) and item for item in names):
        return (), None
    declared = option.get("default_case")
    if isinstance(declared, list):
        declared = declared[0] if declared else None
    return names, declared if declared in names else names[0]


def _choice(saved, names, default):
    selected = saved.get("selected_case")
    if isinstance(selected, str) and selected in names:
        return selected
    index = saved.get("index")
    if type(index) is int and 0 <= index < len(names):
        return names[index]
    return default


def parse_instance(document, interface) -> dict:
    """MFAA 实例配置 → {"scan", "full", "report"}。任务或选项没存过的，跟 interface 的默认；interface 也读不到的，跟内置默认。"""
    tasks = document.get("TaskItems") if isinstance(document, dict) else None
    saved = {}
    for task in tasks if isinstance(tasks, list) else []:
        if not isinstance(task, dict) or task.get("entry") != TASK_ENTRY:
            continue
        options = task.get("option")
        for option in options if isinstance(options, list) else []:
            if isinstance(option, dict) and isinstance(option.get("name"), str):
                saved.setdefault(option["name"], option)
    result = {}
    for key, name in OPTIONS.items():
        names, default = _cases(interface, name)
        choice = _choice(saved.get(name, {}), names, default)
        result[key] = DEFAULTS[key] if choice is None else choice == _ON[key]
    return result


def parse_injected(attach) -> dict:
    """Agt_Ledger_Inject.attach → {"scan", "full", "report"}；没下发的键跟默认。"""
    attach = attach if isinstance(attach, dict) else {}
    return {key: DEFAULTS[key] if attach.get(key) in (None, "") else attach.get(key) == _ON[key] for key in OPTIONS}


def read_instance(project_root, instance_id: str, *, attempts=4, delay=0.1):
    """读本实例配置；实例号不合法、文件读不成或正在保存，返回 None。"""
    if not isinstance(instance_id, str) or not instance_id or instance_id in (".", "..") \
            or re.search(r'[\\/:<>"|?*\x00-\x1f]', instance_id):
        return None
    root = Path(project_root)
    path = root / "config" / "instances" / f"{instance_id}.json"
    for attempt in range(attempts):
        try:
            before = path.stat()
            document = json.loads(path.read_text(encoding="utf-8-sig"))
            after = path.stat()
            if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                raise OSError("实例配置正在保存")
            try:
                interface = json.loads((root / "interface.json").read_text(encoding="utf-8-sig"), strict=False)
            except (OSError, UnicodeError, ValueError):
                interface = None
            return parse_instance(document, interface)
        except (OSError, UnicodeError, ValueError):
            if attempt + 1 < attempts:
                time.sleep(delay)
    return None


def remember_injected(attach) -> dict:
    options = parse_injected(attach)
    with _LOCK:
        _INJECTED.clear()
        _INJECTED.update(options)
    return dict(options)


def ledger_options(session) -> dict:
    """当前生效的三个开关。session：utils.account_sync.current_session() 给的选档会话（来源、项目根、实例号）。"""
    if getattr(session, "source", None) == "mfaa":
        instance = session.instance_id
        options = read_instance(session.project_root, instance)
        with _LOCK:
            if options is None:
                return dict(_LAST_GOOD.get(instance, DEFAULTS))
            _LAST_GOOD[instance] = options
            return dict(options)
    with _LOCK:
        return {**DEFAULTS, **_INJECTED}
