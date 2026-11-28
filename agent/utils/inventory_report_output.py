"""库存记账：读凭证、出报表文件。

做账（inventory_ledger）、制表（inventory_report）、渲染（inventory_report_render）都是纯函数；
读写文件只在这里。不 import maa：Agent 盘完调它，离线工具 tools/inventory_report.py 也调它。

报表放在 <存档目录>/inventory_reports/<sha256(账号)>/：
    <游戏日>.json / .html   当日报表模型与页面；结账定稿后不再重写
    index.html              多日总览      latest.html   最近一个游戏日的页面副本
另在安装目录放一个入口页 库存报表.html（ENTRY_NAME），指向各存档的 latest.html：存档目录藏得太深，用户找不到（U60）。
"""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re

from .inventory_ledger import build_ledger, ts
from .inventory_report import VALUED, build_report
from .inventory_report_render import clock, render_day_html, render_entry_html, render_index_html
from .ledger_rules import GAP_SECONDS, QUIET

ENTRY_NAME = "库存报表.html"

# 物品表（agent/data/bag_item_names.json）里的档案 → 背包仓检的扫描类型（Agt_BagScan_Config.attach.scopes）。
PROFILE_SCOPE = {"ingredients": "A", "dishes": "A", "growth": "B", "craft": "C"}
PROFILE_CATEGORY = {"ingredients": "ingredient", "dishes": "dish", "growth": "growth", "craft": "craft"}
TICKET_KINDS = frozenset({"票券", "服装券", "其他券"})
INDEX_DAYS = 30
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_KST = timezone(timedelta(hours=9))
_UNSAFE = re.compile(r'[\\/*?:"<>|]')


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"), strict=False)
    except (OSError, ValueError):
        return None


def load_catalog(agent_dir) -> dict:
    """物品名 → 科目与扫描类型。"""
    data = _read_json(Path(agent_dir) / "data" / "bag_item_names.json") or {}
    catalog = {}
    for item in data.get("items") or ():
        category = "ticket" if item.get("kind") in TICKET_KINDS else PROFILE_CATEGORY.get(item.get("profile"), "other")
        scope = PROFILE_SCOPE.get(item.get("profile"))
        if scope is None and (catalog.get(item["cn"]) or {}).get("scope"):
            continue   # 目录外有同名的东西（亲密饮品里也有「牛奶」）：盘点读的是会被扫到的那一个，不让它盖掉
        catalog[item["cn"]] = {"category": category, "scope": scope, "kind": item.get("kind")}
    return catalog


def load_cookbook(agent_dir) -> dict:
    """配方、能在商店买到的材料、静态参考价。"""
    data = _read_json(Path(agent_dir) / "data" / "arbitrage_replenish.json") or {}
    recipes = {dish: dict(row.get("ingredients") or {}) for dish, row in (data.get("recipes") or {}).items()}
    reference = {name: row.get("peak_reference") for section in ("recipes", "materials")
                 for name, row in (data.get(section) or {}).items() if type(row.get("peak_reference")) is int}
    purchasable = {name for shop in (data.get("shops") or {}).values() for name in shop.get("items") or ()}
    return {"recipes": recipes, "purchasable": purchasable, "reference": reference}


def load_labels(agent_dir) -> dict:
    """任务入口 → 界面上的显示名；读不到就空着，页面上显示入口名。"""
    root = Path(agent_dir).parent
    for path in (root / "interface.json", root / "assets" / "interface.json"):
        data = _read_json(path)
        if isinstance(data, dict):
            return {task["entry"]: task.get("label") or task.get("name") or task["entry"]
                    for task in data.get("task") or () if isinstance(task, dict) and task.get("entry")}
    return {}


def _folder(store_dir, name: str, account: str) -> Path:
    return Path(store_dir) / name / sha256(str(account).encode("utf-8")).hexdigest()


def history_dir(store_dir, account) -> Path:
    return _folder(store_dir, "inventory_history", account)


def report_dir(store_dir, account) -> Path:
    return _folder(store_dir, "inventory_reports", account)


def save_file(store_dir, account) -> Path:
    account = str(account)
    clean = _UNSAFE.sub("_", account)   # 与 PersistentStore 给账号存档起名的规则相同
    return Path(store_dir) / ("agent_save_data.json" if account == "0" else f"agent_save_data_{clean}.json")


def load_vouchers(store_dir, account, *, since: str | None = None, hot=None) -> list:
    """该账号的凭证：冷档（按天的 jsonl）加热档（账号存档里的事件）。两边会重叠，做账时去重。

    since：只读这一天及以后的冷档。hot：调用方手里已有热档事件时直接给，免得再读一遍存档。
    """
    events = []
    folder = history_dir(store_dir, account)
    if folder.is_dir():
        for path in sorted(folder.glob("*.events.jsonl")):
            day = path.name[:10]
            if not _DAY.fullmatch(day) or (since and day < since):
                continue
            for line in path.read_bytes().splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue   # 冷档里的坏行原样留着，不影响别的
                if isinstance(row, dict):
                    events.append(row)
    if hot is None:
        data = _read_json(save_file(store_dir, account)) or {}
        hot = ((data.get("arbitrage") or {}).get("inventory") or {}).get("events") or []
    events.extend(event for event in hot if isinstance(event, dict))
    return events


def load_prices(store_dir, scans, cookbook) -> dict:
    """每次盘点适用的存货单价：盘点所在价格日（KST 日期）的出售列表最高价；
    没有当天的行情就用同月最近一天的，再没有用静态参考价，并记下来源。"""
    data = _read_json(Path(store_dir) / "agent_shared_data.json") or {}
    days = (((data.get("arbitrage") or {}).get("market") or {}).get("days") or {})
    market = {}
    for day, snapshot in days.items():
        rows = {item["name"]: item["peak_price"] for item in (snapshot or {}).get("items") or ()
                if isinstance(item, dict) and type(item.get("peak_price")) is int and item["peak_price"] > 0
                and item.get("name_confirmed", True)}
        if rows and _DAY.fullmatch(str(day)):
            market[day] = rows
    reference = (cookbook or {}).get("reference") or {}
    prices = {}
    for scan in scans:
        price_day = datetime.fromtimestamp(scan["ts"], _KST).date().isoformat()
        month = sorted((day for day in market if day[:7] == price_day[:7]),
                       key=lambda day: (day > price_day, abs(datetime.fromisoformat(day).toordinal()
                                                             - datetime.fromisoformat(price_day).toordinal())))
        row = {name: {"unit": unit, "basis": "reference", "price_day": None} for name, unit in reference.items()}
        for day in reversed(month):   # 越靠前越优先，后写的盖掉前面的
            for name, unit in market[day].items():
                row[name] = {"unit": unit, "basis": "peak_price", "price_day": day}
        prices[scan["scan_id"]] = row
    return prices


def _write(path: Path, text: str) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _snapshots(folder: Path) -> dict:
    result = {}
    if folder.is_dir():
        for path in folder.glob("*.json"):
            model = _read_json(path) if _DAY.fullmatch(path.stem) else None
            if isinstance(model, dict) and model.get("game_day") == path.stem:
                result[path.stem] = model
    return result


def _balance(model) -> dict | None:
    """一张定稿的表上的期末余额 {物品: {"quantity", "at", "unit"}}；不是定稿的表、或是没有余额块的旧表，返回 None。"""
    balance = model.get("balance") if model.get("status") == "final" else None
    rows = balance.get("items") if isinstance(balance, dict) else None
    if not isinstance(rows, dict):
        return None
    return {name: row for name, row in rows.items() if isinstance(row, dict) and type(row.get("quantity")) is int}


def _carry(saved, day) -> dict | None:
    """day 之前最近一张定稿表的期末余额：这一天的上日期末以它为准。没有定稿的表、或那张是旧表，返回 None（改从账里推）。"""
    earlier = [other for other, model in saved.items() if other < day and model.get("status") == "final"]
    return _balance(saved[max(earlier)]) if earlier else None


def write_entry(store_dir, entry_dir) -> Path | None:
    """在 entry_dir 写入口页，列出存档目录里每个存档最近一天的报表（最近更新的在前）。一份报表都没有时不写。"""
    base = Path(store_dir) / "inventory_reports"
    accounts = []
    for folder in sorted(base.iterdir()) if base.is_dir() else ():
        latest = folder / "latest.html"
        days = sorted(path.stem for path in folder.glob("*.json") if _DAY.fullmatch(path.stem))
        model = _read_json(folder / f"{days[-1]}.json") if days else None
        if not latest.is_file() or not isinstance(model, dict):
            continue
        page = latest
        # 最新一天只有与上日期末共用的那一盘（U82：跨日段并进了上一日）：那一页没有可看的变动，入口改指上一日。
        shared = (model.get("overnight") or {}).get("shared") and len(model.get("scans") or ()) == 1
        earlier = _read_json(folder / f"{days[-2]}.json") if shared and len(days) > 1 else None
        if isinstance(earlier, dict) and (folder / f"{days[-2]}.html").is_file():
            note = f"{str(model.get('game_day'))[5:]} 只有与上日期末共用的那一盘"
            model = {**earlier, "summary": f"{earlier.get('summary') or ''}（{note}）"}
            page = folder / f"{days[-2]}.html"
        alias = (model.get("account") or {}).get("alias")
        index = folder / "index.html"
        accounts.append({"name": f"存档 {alias}" if alias else "默认存档", "day": model.get("game_day") or days[-1],
                         "status": model.get("status"), "summary": model.get("summary"), "as_of": model.get("as_of"),
                         "latest": page.resolve().as_uri(), "index": index.resolve().as_uri() if index.is_file() else None,
                         "touched": latest.stat().st_mtime})
    if not accounts:
        return None
    accounts.sort(key=lambda account: -account["touched"])
    path = Path(entry_dir) / ENTRY_NAME
    _write(path, render_entry_html(accounts, base.resolve().as_uri()))
    return path


def generate_reports(store_dir, account, *, agent_dir, as_of: str | None = None, days=None, force=False, hot=None,
                     quiet=QUIET, gap=GAP_SECONDS, alias=None, entry_dir=None) -> dict:
    """重算还没结账的游戏日（days 给了就只算这些天，force 连已定稿的也重算），并更新总览与 latest。

    结转：每一天的上日期末取它之前最近一张定稿表的期末余额，对不上的记成期初调整；定稿的表运行时不再重写。
    离线强制重算某一天会改掉它的期末余额，之后的日子要一起重算（--from/--to），否则那几天的上日期末还是旧的。

    quiet、gap：做账划段用的免记名单与连续阈值，调用方从节点 Agt_Ledger_Config 取来传入（ledger_rules.settings），
    与运行时判盘不盘的是同一份；不传就是内置值。

    entry_dir：给了就顺手在那里更新入口页（安装目录）；写不进去只是没有入口，报表照出。
    返回 {"days": 写了哪几天, "folder": 报表目录, "latest": 最近一天页面的路径, "summary": 最近一天的摘要,
          "entry": 入口页路径或 None}。
    """
    folder = report_dir(store_dir, account)
    as_of = as_of or datetime.now(timezone.utc).isoformat(timespec="seconds")
    saved = _snapshots(folder)
    # 定稿的表是固定值：运行时不再重写，报表格式的版本变了也不重算（U75）。
    settled = sorted(day for day, model in saved.items() if model.get("status") == "final")
    frozen = settled[-1] if settled and not force and not days else None   # 这一天及更早的不碰
    # 下一日的期初以最后一张定稿表的期末余额为准，所以只读它那天起的冷档就够。
    # 那张还是没有余额块的旧表时，这一回把冷档读全，从账里把余额推出来；之后定稿的表都带着，就不用再读全了。
    partial = frozen is not None and _balance(saved[frozen]) is not None
    vouchers = load_vouchers(store_dir, account, since=frozen if partial else None, hot=hot)
    catalog, cookbook = load_catalog(agent_dir), load_cookbook(agent_dir)
    ledger = build_ledger(vouchers, catalog=catalog, quiet=quiet, gap=gap)
    prices = load_prices(store_dir, ledger["scans"], cookbook)
    if not any(name in row for row in prices.values() for name, entry in ledger["items"].items()
               if entry["category"] in VALUED):
        prices = None   # 一样有价的存货都没有，就只出数量
    labels = load_labels(agent_dir)
    present = sorted({scan["day"] for scan in ledger["scans"]})
    # 最后一个定稿日及更早的日子不碰：定稿的不重写；没出过表的，凭证也多半不全（只读了近期的、或只剩热档里那几条）。
    wanted = [day for day in present if (day in days if days else force or day not in settled)
              and not (frozen and day <= frozen)]
    written = []
    for day in wanted:   # 从早到晚：这一回刚定稿的表，就是下一天的上日期末
        model = build_report(ledger, game_day=day, as_of=as_of, prices=prices, cookbook=cookbook, labels=labels,
                             kst=clock(day), history_before=any(other < day for other in saved), prior=_carry(saved, day),
                             account={"hash": folder.name, "alias": alias or (None if str(account) == "0" else str(account))})
        if model is None:
            continue
        folder.mkdir(parents=True, exist_ok=True)
        # 先页面后快照：快照在就算数（定稿的不再重写），页面必须先于它落盘，免得中途断掉留下一张期中的页面改不回来。
        _write(folder / f"{day}.html", render_day_html(model))
        _write(folder / f"{day}.json", json.dumps(model, ensure_ascii=False, separators=(",", ":")))
        saved[day] = model
        written.append(day)
    result = {"days": written, "folder": folder, "latest": None, "summary": None, "entry": None}
    if saved and written:
        recent = sorted(saved)[-INDEX_DAYS:]
        _write(folder / "index.html", render_index_html([saved[day] for day in recent]))
        _write(folder / "latest.html", (folder / f"{recent[-1]}.html").read_text(encoding="utf-8"))
        result.update(latest=folder / "latest.html", summary=saved[recent[-1]].get("summary"))
        if entry_dir is not None:
            try:
                result["entry"] = write_entry(store_dir, entry_dir)
            except OSError:
                pass   # 安装目录不可写（装在只读位置）：没有入口而已
    return result


def game_days_between(first: str, last: str) -> list:
    start, end = datetime.fromisoformat(first).date(), datetime.fromisoformat(last).date()
    return [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]


__all__ = ["ENTRY_NAME", "generate_reports", "write_entry", "load_vouchers", "load_catalog", "load_cookbook", "load_prices", "report_dir",
           "history_dir", "save_file", "game_days_between", "ts"]
