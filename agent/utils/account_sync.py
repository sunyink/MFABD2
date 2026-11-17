"""Select an account once per root Context task, without Tasker event delivery.

The client decides where the account comes from, never whichever value happens
to be present. MFAA's instance file owns the independent switch and user number,
not the task checkbox; its global options are shared across instances, so an
injected value is never read there. Android APKs intentionally use account 0.
Other clients (VS Code MaaSupport keeps global options per task) read the value
injected into the current task. Running from source (dev mode) falls back to
account 0 when nothing is injected, so a single node can be debugged; release
packages still block. Business callbacks must synchronize before accessing
PersistentStore, including cached task decisions.
"""
from collections import OrderedDict
import os
from pathlib import Path
from threading import RLock

from . import mfaalog as logger
from .instance_account_config import (
    INJECT_NODE, AccountSelection, STOP_ENTRY, parse_injected_account, read_instance_account,
)
from .persistent_store import PersistentStore

MFAA_CLIENT = "MFAAvalonia"


def account_source(android: bool, instance_id: str, client: str) -> str:
    if android:
        return "android"
    # MFAA without an instance ID stays on its own path, so the error names the real cause.
    if instance_id or client == MFAA_CLIENT:
        return "mfaa"
    return "injected"


class AccountSession:
    def __init__(self, project_root: Path, instance_id: str, *, android=False, injected=False,
                 client="", store=PersistentStore, dev_default=False):
        self.project_root = Path(project_root)
        self.instance_id = instance_id
        self.source = "android" if android else "injected" if injected else "mfaa"
        self.client = client
        self.store = store
        self.dev_default = dev_default and self.source == "injected"
        self._choices = OrderedDict()
        self._reported = set()
        self._lock = RLock()

    @property
    def source_label(self) -> str:
        if self.source == "injected":
            return f"任务注入（{self.client or '未知客户端'}）"
        return "Android 单档" if self.source == "android" else "MFAA 实例设置"

    def _select(self, context) -> AccountSelection:
        if self.source == "android":
            return AccountSelection("0", False)
        if self.source == "injected":
            node = context.get_node_object(INJECT_NODE)
            if node is None:
                return AccountSelection(reason=f"缺少节点 {INJECT_NODE}，请更新 base 资源")
            attach = node.attach
            if self.dev_default and isinstance(attach, dict) and attach.get("account_id") in (None, ""):
                # e.g. VS Code debugging from a single node, where no account is injected.
                logger.warning("[Account] 本任务没有下发存档号；源码运行（dev）按 0 号档继续，发布包仍会拦截")
                return AccountSelection("0")
            return parse_injected_account(attach)
        return read_instance_account(self.project_root, self.instance_id)

    def sync(self, context, where="", *, stop=True) -> bool:
        """stop=False: an optional feature asks; a missing account blocks the store but leaves the task running."""
        key = None
        try:
            job_id = context.get_task_job().job_id
            if type(job_id) is not int or job_id <= 0:
                raise ValueError("无有效根任务 ID")
            key = (self.instance_id if self.source == "mfaa" else self.source, job_id)
            with self._lock:
                if key not in self._choices:
                    self._choices[key] = self._select(context)
                selection = self._choices[key]
                self._choices.move_to_end(key)
                while len(self._choices) > 128:
                    expired, _ = self._choices.popitem(last=False)
                    self._reported.discard((expired, "ready"))
                    self._reported.discard((expired, "blocked"))
                if selection.valid:
                    if not self.store.is_bound(key, selection.account_id):
                        self.store.bind_account(selection.account_id, key)
                    if (key, "ready") not in self._reported:
                        # An injection carries only the number, not the switch state.
                        mode = {True: "Yes", False: "No"}.get(selection.enabled, "-")
                        logger.info(f"[Account] 实例={self.instance_id or '-'} task={job_id} "
                                    f"启用多存档={mode} 存档={selection.account_id} 来源={self.source_label}")
                        self._reported.add((key, "ready"))
                    return True
                reason = selection.reason
                self.store.block_account(key)
        except Exception as exc:
            reason = f"账号初始化失败：{exc}"
            with self._lock:
                if key is not None:
                    self._choices[key] = AccountSelection(reason=reason)
                self.store.block_account(key)
        with self._lock:
            if (key, "blocked") not in self._reported:
                logger.error(f"[Account] {reason}；已禁止本任务读写账号存档 ({where})")
                self._reported.add((key, "blocked"))
        if not stop:
            return False
        # Stop this root Context, including recognition fallback candidates.
        try:
            result = context.run_action(STOP_ENTRY)
            if result is None or not result.success:
                logger.error("[Account] 当前任务停止动作失败；账号存档仍保持封锁")
        except Exception as exc:
            logger.error(f"[Account] 无法停止当前任务；账号存档仍保持封锁：{exc}")
        return False


_session = None


def configure_account_session(project_root: Path, *, android=False, dev=False, instance_id=None, client=None):
    global _session
    identity = os.environ.get("MFA_INSTANCE_ID", "").strip() if instance_id is None else instance_id
    client = os.environ.get("PI_CLIENT_NAME", "").strip() if client is None else client
    source = account_source(android, identity, client)
    _session = AccountSession(project_root, identity, android=source == "android",
                              injected=source == "injected", client=client, dev_default=dev)
    PersistentStore.block_account()
    fallback = "（dev：未下发时用 0 号档）" if _session.dev_default else ""
    logger.info(f"[Account] 客户端={client or '-'} 选档来源={_session.source_label}{fallback}")


def sync_from_context(context, where: str = "", *, stop: bool = True) -> bool:
    if _session is None:
        # Missing identity never silently means Android or the default account;
        # an injected client without a value is blocked the same way.
        configure_account_session(Path(__file__).resolve().parents[2])
    return _session.sync(context, where, stop=stop)


def current_session():
    """The configured session (source, project root, instance id), creating it like sync_from_context does."""
    if _session is None:
        configure_account_session(Path(__file__).resolve().parents[2])
    return _session
