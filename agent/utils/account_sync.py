"""Select an account once per root Context task, without Tasker event delivery.

MFAA's instance file owns the independent switch and user number, not the task
checkbox. Android APKs intentionally use account 0. Business callbacks must
synchronize before accessing PersistentStore, including cached task decisions.
"""
from collections import OrderedDict
import os
from pathlib import Path
from threading import RLock

from . import mfaalog as logger
from .instance_account_config import AccountSelection, STOP_ENTRY, read_instance_account
from .persistent_store import PersistentStore


class AccountSession:
    def __init__(self, project_root: Path, instance_id: str, *, android=False, store=PersistentStore):
        self.project_root = Path(project_root)
        self.instance_id = instance_id
        self.android = android
        self.store = store
        self._choices = OrderedDict()
        self._reported = set()
        self._lock = RLock()

    def sync(self, context, where="") -> bool:
        key = None
        try:
            job_id = context.get_task_job().job_id
            if type(job_id) is not int or job_id <= 0:
                raise ValueError("无有效根任务 ID")
            key = ("android" if self.android else self.instance_id, job_id)
            with self._lock:
                if key not in self._choices:
                    self._choices[key] = (AccountSelection("0", False) if self.android else
                                          read_instance_account(self.project_root, self.instance_id))
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
                        source = "Android 单档" if self.android else "MFAA 实例设置"
                        mode = "Yes" if selection.enabled else "No"
                        logger.info(f"[Account] 实例={self.instance_id or '-'} task={job_id} "
                                    f"启用多存档={mode} 存档={selection.account_id} 来源={source}")
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
        # Stop this root Context, including recognition fallback candidates.
        try:
            result = context.run_action(STOP_ENTRY)
            if result is None or not result.success:
                logger.error("[Account] 当前任务停止动作失败；账号存档仍保持封锁")
        except Exception as exc:
            logger.error(f"[Account] 无法停止当前任务；账号存档仍保持封锁：{exc}")
        return False


_session = None


def configure_account_session(project_root: Path, *, android=False, instance_id=None):
    global _session
    identity = os.environ.get("MFA_INSTANCE_ID", "").strip() if instance_id is None else instance_id
    _session = AccountSession(project_root, identity, android=android)
    PersistentStore.block_account()


def sync_from_context(context, where: str = "") -> bool:
    if _session is None:
        # Missing identity never silently means Android or the default account.
        configure_account_session(Path(__file__).resolve().parents[2])
    return _session.sync(context, where)
