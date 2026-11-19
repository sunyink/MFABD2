"""库存记账的三个开关：盘不盘、每次是否全盘、出不出报表。

选项挂在任务「⚙️ 库存记账」上（入口 Env_Ledger_Config），照多存档的做法——全局选项各实例共用，不能放那里：
- MFAA：直接读本实例配置里那个任务的选项。任务勾不勾、跑没跑都不影响；没存过的值跟 interface 的默认。
- 其他客户端：选项的覆盖只在那个任务里生效，任务跑到时由动作 LedgerConfig 把 Agt_Ledger_Inject.attach 记进内存；
  没跑过就按默认。
不因为配置读不成而拦任何流程：实例配置读不到，沿用上一次读成的，从没读成过才按默认（盘、范围按规则、出报表）。
已经存下的选择对着本文件的 CASES 认，不靠 interface.json——它只用来查「没存过的选项默认是哪个」，读不到就用内置默认。
所以 interface.json 在不在、放在哪（发布包在安装根；开发时在 assets/ 下，项目根没有），都不会把存下的「No」变回「开」。
"""

import json
from pathlib import Path
import re
import threading
import time


TASK_ENTRY = "Env_Ledger_Config"
INJECT_NODE = "Agt_Ledger_Inject"
# 键 → interface.json 里的选项名。CASES 是各选项的 case 名，顺序必须与 interface.json 一致：旧版 MFAA 只存序号，
# 新加的任务还会先写上序号 0（tools/fixtures/account_config/README.md）。_ON 是算「开」的那个 case 名。
# 源文件与各平台发布出去的 interface 是否仍与这里一致，由 tools/verify_ledger_point.py 守着。
OPTIONS = {"scan": "记账盘点", "full": "记账盘点范围", "report": "记账报表"}
CASES = {"scan": ("Yes", "No"), "full": ("按规则", "每次全盘"), "report": ("Yes", "No")}
_ON = {"scan": "Yes", "full": "每次全盘", "report": "Yes"}
DEFAULTS = {"scan": True, "full": False, "report": True}

_LOCK = threading.Lock()
_INJECTED: dict = {}      # 其他客户端：LedgerConfig 记下的选择
_LAST_GOOD: dict = {}     # MFAA：实例号 → 上一次读成的结果


def _saved_case(saved, key):
    """实例配置里存下的选择：认 case 名，只存了序号的认序号；没存、认不出返回 None。"""
    names = CASES[key]
    selected = saved.get("selected_case")
    if isinstance(selected, str) and selected in names:
        return selected
    index = saved.get("index")
    if type(index) is int and 0 <= index < len(names):
        return names[index]
    return None


def _default_case(interface, key):
    """interface 里这个选项默认选哪个 case（没声明、声明的不存在就取第一个，同 MFAA）；读不到、不是认识的 case 返回 None。"""
    options = interface.get("option") if isinstance(interface, dict) else None
    option = options.get(OPTIONS[key]) if isinstance(options, dict) else None
    if not isinstance(option, dict):
        return None
    names = [case.get("name") for case in option.get("cases") or [] if isinstance(case, dict)]
    declared = option.get("default_case")
    if isinstance(declared, list):
        declared = declared[0] if declared else None
    choice = declared if declared in names else names[0] if names else None
    return choice if choice in CASES[key] else None


def parse_instance(document, interface) -> dict:
    """MFAA 实例配置 → {"scan", "full", "report"}。

    存下的选择直接算数，不需要 interface；任务或选项没存过的，跟 interface 的默认；interface 也读不到的，跟内置默认。
    interface：文档，或一个取文档的函数——只在真有选项没存过时才调用。
    """
    tasks = document.get("TaskItems") if isinstance(document, dict) else None
    saved = {}
    for task in tasks if isinstance(tasks, list) else []:
        if not isinstance(task, dict) or task.get("entry") != TASK_ENTRY:
            continue
        options = task.get("option")
        for option in options if isinstance(options, list) else []:
            if isinstance(option, dict) and isinstance(option.get("name"), str):
                saved.setdefault(option["name"], option)
    loaded = []

    def defaults():
        if not loaded:
            loaded.append(interface() if callable(interface) else interface)
        return loaded[0]

    result = {}
    for key, name in OPTIONS.items():
        choice = _saved_case(saved.get(name, {}), key) or _default_case(defaults(), key)
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

    def interface():
        # 与实例配置同在 MFAA 的根下（发布包的安装根）。只查默认值用，读不到返回 None，不算实例配置读失败。
        try:
            return json.loads((root / "interface.json").read_text(encoding="utf-8-sig"), strict=False)
        except (OSError, UnicodeError, ValueError):
            return None

    for attempt in range(attempts):
        try:
            before = path.stat()
            document = json.loads(path.read_text(encoding="utf-8-sig"))
            after = path.stat()
            if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                raise OSError("实例配置正在保存")
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
