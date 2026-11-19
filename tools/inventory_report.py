"""离线出库存报表：读存档目录里某个账号的凭证，重算并写出日报与总览。不需要 maa、不连游戏。

    python -B tools/inventory_report.py <存档目录>                      列出目录里有哪些账号
    python -B tools/inventory_report.py <存档目录> --account 0          重算该账号所有还读得到凭证的游戏日
    python -B tools/inventory_report.py <存档目录> --account 0 --day 2026-11-14
    python -B tools/inventory_report.py <存档目录> --account 0 --from 2026-11-01 --to 2026-11-07

<存档目录> 就是放 agent_save_data*.json 的那个目录；账号是多存档的存档名，没开多存档时是 0。
报表写在 <存档目录>/inventory_reports/<账号的 sha256>/，与 Agent 运行时写的是同一处。
冷档只留 62 天：更早的日子没有凭证，重算不出来，已有的快照原样留着。
"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from utils import inventory_report_output as output  # noqa: E402
from utils import ledger_rules  # noqa: E402

LEDGER_NODES = ROOT / "assets" / "resource" / "base" / "pipeline" / "Ledger.json"


def ledger_settings() -> dict:
    """做账划段用的免记名单与连续阈值：和运行时一样取自节点 Agt_Ledger_Config.attach，这里直接读资源文件。

    读不到用内置值。运行时若被别的资源包或选项覆盖过这个节点，离线这里看不到——现在没有谁覆盖它。
    """
    try:
        attach = json.loads(LEDGER_NODES.read_text(encoding="utf-8"))["Agt_Ledger_Config"]["attach"]
    except (OSError, ValueError, KeyError, TypeError):
        attach = None
    return ledger_rules.settings(attach)


def accounts(store: Path) -> list:
    found = []
    for path in sorted(store.glob("agent_save_data*.json")):
        name = path.stem[len("agent_save_data"):].lstrip("_") or "0"
        found.append((name, path.name))
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="离线重算库存报表")
    parser.add_argument("store", type=Path, help="存档目录（放 agent_save_data*.json 的那个）")
    parser.add_argument("--account", help="账号：多存档的存档名，没开多存档时是 0")
    parser.add_argument("--day", help="只算这一个游戏日（UTC 日期）")
    parser.add_argument("--from", dest="first", help="区间起（游戏日）")
    parser.add_argument("--to", dest="last", help="区间止（游戏日）")
    options = parser.parse_args(argv)
    if not options.store.is_dir():
        print(f"目录不存在：{options.store}")
        return 2
    if options.account is None:
        rows = accounts(options.store)
        print("这个目录里的账号存档：" if rows else "这个目录里没有账号存档。")
        for name, file in rows:
            print(f"  --account {name}    （{file}）")
        return 0
    days = None
    if options.day:
        days = [options.day]
    elif options.first or options.last:
        if not (options.first and options.last):
            print("--from 与 --to 要一起给")
            return 2
        days = output.game_days_between(options.first, options.last)
    settings = ledger_settings()
    result = output.generate_reports(options.store, options.account, agent_dir=ROOT / "agent", days=days, force=True,
                                     quiet=settings["quiet"], gap=settings["gap"])
    if not result["days"]:
        print("没有可出的报表：这个账号在所选范围里没有记账盘点。")
        return 1
    print(f"写了 {len(result['days'])} 天：{result['days'][0]} … {result['days'][-1]}")
    print(f"目录：{result['folder']}")
    print(f"最近一天：{result['summary']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
