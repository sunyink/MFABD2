"""价目表扫描记录随日志包收回：每轮出售主控前后，把共享行情存档覆盖复制到 debug。

不开遥测也能拿回各日溢价的原始观察，用来总结每日溢价规律。共享存档只存行情
(arbitrage_store 是它唯一的使用方)，不含账号数据。文件名以 custom.log 开头，
MFAA 导出日志包时会递归收集。这是临时的数据收集：
删除本文件与 action/arbitrage_result.py 里两处 export_market_log 调用即可整块移除。
"""

import os
import shutil
from pathlib import Path

from . import mfaalog
from .persistent_store import SharedStore

LOG_NAME = "custom.log.arbitrage_market.json.log"


def debug_dir():
    return Path(__file__).resolve().parents[2] / "debug"


def export_market_log(stage, mode):
    """原样复制，不经 load()，存档不存在时也不会被初始化；任何失败只记 debug。"""
    try:
        SharedStore._init_paths()
        source = SharedStore.FILE_PATH
        if source is None or not source.exists():
            return
        directory = debug_dir()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / LOG_NAME
        temp = target.with_name(target.name + ".tmp")
        shutil.copyfile(source, temp)
        os.replace(temp, target)
    except Exception as exc:
        mfaalog.debug(f"[Arbitrage] 行情记录复制到 debug 失败({stage}/{mode}): {exc}")
