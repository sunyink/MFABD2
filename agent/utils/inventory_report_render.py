"""库存记账：把报表模型画成单文件 HTML。

只吃 inventory_report.build_report 的结果。样式与数据全部内嵌，不引用任何外部资源；
只有一段内联脚本，让表头与首列在手机任意缩放下贴住屏幕边，不跑脚本时退回原生吸附。
"""

from datetime import datetime, timedelta, timezone
import html
import json
import re

from .inventory_report import GOLD, GROUPS, KIND_LABEL, REASONS, VALUED

KST = timezone(timedelta(hours=9))
FLAG_LABEL = {"abnormal_end": "未正常结束", "unsettled": "未当场结清", "cross_day": "跨日", "partial_scan": "有一次盘点中断"}
STATUS_LABEL = {"interim": "期中（未结账）", "final": "期末（已结账）"}
GROUP_TITLE = {"operating": "经营活动", "valuation": "估值调整", "outside": "未入账"}
# 期初调整的那一笔跨过的结账盘点为什么没有它的读数（inventory_ledger 物品区间的 reason）。
ADJUST_REASONS = {"unknown": "没读准它", "unread": "没读到顶栏", "out_of_scope": "不扫这一科目", "missed": "没盘完",
                  "suspect_zero": "把它判成了 0、前后读数又相同，按漏读处理"}
BASIS_LABEL = {"counted": "盘点差", "booked": "已记账", "derived": "推算"}

CSS = r"""
:root{
  --bg:#f3f5f2; --card:#ffffff; --fg:#1a1f1c; --mut:#5f6964; --line:#d7ddd8; --rule:#c3cbc6; --head:#eaefeb;
  --acc:#2b5d8a; --neg:#9a3d2e; --dim:#9aa39d;
  --c-daily:#2a78d6; --c-collect:#1baf7a; --c-shop:#eb6834; --c-outside:#8a938e; --c-mixed:#c98500;
  --warn-bg:#fff6e0; --warn-fg:#6b4a00; --crit:#b4352f; --ok:#1d6b3a;
  --font:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei","Noto Sans CJK SC","Noto Sans SC",sans-serif;
  --serif:"Songti SC","STSong","SimSun","Noto Serif CJK SC","Noto Serif SC",Georgia,serif;
}
@media (prefers-color-scheme: dark){ :root:not([data-theme="light"]){
  --bg:#15181a; --card:#1d2124; --fg:#e6eae7; --mut:#9aa49e; --line:#2d3439; --rule:#3a434a; --head:#22282c;
  --acc:#7fb0dc; --neg:#e3907f; --dim:#6b756f;
  --c-daily:#3987e5; --c-collect:#199e70; --c-shop:#d95926; --c-outside:#7b847f; --c-mixed:#eda100;
  --warn-bg:#3a2e12; --warn-fg:#f3d98a; --crit:#e46b65; --ok:#5fbf85; color-scheme:dark } }
:root[data-theme="dark"]{
  --bg:#15181a; --card:#1d2124; --fg:#e6eae7; --mut:#9aa49e; --line:#2d3439; --rule:#3a434a; --head:#22282c;
  --acc:#7fb0dc; --neg:#e3907f; --dim:#6b756f;
  --c-daily:#3987e5; --c-collect:#199e70; --c-shop:#d95926; --c-outside:#7b847f; --c-mixed:#eda100;
  --warn-bg:#3a2e12; --warn-fg:#f3d98a; --crit:#e46b65; --ok:#5fbf85; color-scheme:dark }
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 var(--font);padding-block:16px 40px;padding-inline:16px}
.wrap{max-width:1100px;margin:0 auto;display:grid;grid-template-columns:minmax(0,1fr);gap:18px}
.wrap>*{min-width:0}
h1,h2{margin:0;text-wrap:balance}
h1{font:600 22px/1.3 var(--serif)}
h2{font:600 17px/1.3 var(--serif);padding-bottom:6px;border-bottom:2px solid var(--rule);display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
h2 small{font:12px/1.4 var(--font);color:var(--mut);font-weight:400}
a{color:var(--acc)}
.mut{color:var(--mut)} .dim{color:var(--dim)} .small{font-size:12px}
.num{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
.neg{color:var(--neg)} .zero{color:var(--dim)}
.q{display:block} .amt{display:block;font-size:11px;color:var(--mut);margin-top:1px}
.badge{display:inline-block;font-size:11px;line-height:1;padding:4px 7px;border-radius:4px;border:1px solid var(--line);color:var(--mut);vertical-align:middle;white-space:nowrap}
.badge.interim{border-color:var(--c-mixed);color:var(--warn-fg);background:var(--warn-bg)}
.tag{display:inline-block;font-size:11px;line-height:1;padding:3px 6px;border-radius:3px;color:#fff;white-space:nowrap}
.tag.daily{background:var(--c-daily)} .tag.collect{background:var(--c-collect)} .tag.shop{background:var(--c-shop)}
.tag.outside{background:var(--c-outside)} .tag.mixed{background:var(--c-mixed);color:#1a1400}
header.sheet{display:grid;gap:4px}
header.sheet .meta{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:12px;color:var(--mut)}
header.sheet .meta b{color:var(--fg);font-weight:500}
.alerts{display:grid;gap:6px}
.alert{background:var(--warn-bg);color:var(--warn-fg);border-left:3px solid var(--c-mixed);padding:8px 10px;border-radius:0 4px 4px 0;font-size:13px}
.alert.info{background:var(--head);color:var(--fg);border-left-color:var(--c-outside)}
.alert.error{border-left-color:var(--crit);color:var(--crit);font-weight:600}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:10px 12px;min-width:0}
.kpi .k{font-size:12px;color:var(--mut)} .kpi .v{font-size:20px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1.25;margin-top:2px}
.kpi .s{font-size:12px;color:var(--mut);margin-top:2px}
.tl{display:flex;gap:2px;height:30px;border-radius:4px;overflow:hidden;min-width:0}
.tl .seg{min-width:0;display:flex;align-items:center;justify-content:center;font-size:11px;color:#fff;overflow:hidden;white-space:nowrap;padding:0 4px}
.tl .seg.daily{background:var(--c-daily)} .tl .seg.collect{background:var(--c-collect)} .tl .seg.shop{background:var(--c-shop)}
.tl .seg.outside{background:repeating-linear-gradient(135deg,var(--c-outside) 0 4px,transparent 4px 8px);color:var(--fg)}
.tl .seg.mixed{background:repeating-linear-gradient(135deg,var(--c-mixed) 0 4px,var(--warn-bg) 4px 8px);color:var(--fg)}
.tl .seg.compressed{flex:0 0 72px}
.ticks{display:flex;justify-content:space-between;font-size:11px;color:var(--mut);margin-top:3px}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12px;color:var(--mut);margin-top:6px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;vertical-align:-1px;margin-right:4px}
.tw{width:-webkit-fit-content;width:fit-content;min-width:100%;border:1px solid var(--line);border-radius:6px;background:var(--card)}
table{border-collapse:separate;border-spacing:0;width:100%;min-width:0;font-size:13px}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top;text-align:left}
thead th{background:var(--head);color:var(--mut);font-weight:600;font-size:12px;white-space:normal;position:sticky;top:env(safe-area-inset-top,0px);z-index:2;box-shadow:0 1px 0 var(--line)}
thead th .sub{display:block;font-weight:400;font-size:11px;color:var(--dim)}
tbody tr:last-child td{border-bottom:0}
tbody tr.sub td{color:var(--mut)}
tbody tr.sum td{font-weight:600;border-top:2px solid var(--rule);background:var(--head)}
tbody tr.grp td{background:var(--head);color:var(--mut);font-size:12px;font-weight:600;letter-spacing:.02em}
tbody tr.val td{background:var(--head);font-size:12px;color:var(--mut)}
th.sticky,td.sticky{position:sticky;left:0;background:var(--card);z-index:1;box-shadow:inset -1px 0 0 var(--line);min-width:7em}
thead th.sticky{background:var(--head);z-index:3}
tbody tr.grp td.sticky,tbody tr.sum td.sticky,tbody tr.val td.sticky{background:var(--head)}
td.span{background:repeating-linear-gradient(135deg,transparent 0 6px,var(--head) 6px 12px)}
td.gap{color:var(--dim);text-align:center;font-size:11px}
td.note{color:var(--mut);font-size:12px;white-space:normal;min-width:8em;max-width:22em}
td.bad{color:var(--crit);font-weight:600}
.col-daily{box-shadow:inset 0 -2px 0 var(--c-daily)} .col-collect{box-shadow:inset 0 -2px 0 var(--c-collect)} .col-shop{box-shadow:inset 0 -2px 0 var(--c-shop)}
.col-outside{box-shadow:inset 0 -2px 0 var(--c-outside)} .col-mixed{box-shadow:inset 0 -2px 0 var(--c-mixed)}
details{border:1px solid var(--line);border-radius:6px;background:var(--card)}
details>summary{cursor:pointer;padding:9px 12px;font-weight:600;list-style:none;display:flex;gap:8px;align-items:center}
details>summary::-webkit-details-marker{display:none}
details>summary::before{content:"▸";color:var(--mut);font-size:12px}
details[open]>summary::before{content:"▾"}
details>summary small{font-weight:400;color:var(--mut)}
details>.body{padding:0 12px 12px;display:grid;gap:10px}
details .tw{border-radius:4px}
details.plain{border:0;background:transparent} details.plain>summary{padding:6px 0}
.segs{display:grid;gap:8px}
.segc{border:1px solid var(--line);border-left-width:4px;border-radius:4px;padding:8px 10px;background:var(--card);display:grid;gap:4px}
.segc.daily{border-left-color:var(--c-daily)} .segc.collect{border-left-color:var(--c-collect)} .segc.shop{border-left-color:var(--c-shop)}
.segc.outside{border-left-color:var(--c-outside)} .segc.mixed{border-left-color:var(--c-mixed)}
.segc .hd{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center}
.segc ul{margin:0;padding-left:18px;font-size:12px;color:var(--mut)}
.chips{display:flex;flex-wrap:wrap;gap:4px}
.chip{font-size:11px;border:1px solid var(--line);border-radius:10px;padding:1px 7px;color:var(--mut)}
.chip.warn{border-color:var(--c-mixed);color:var(--warn-fg)}
.chip.ok{border-color:var(--ok);color:var(--ok)}
.checks{display:grid;gap:4px;font-size:13px}
.checks div::before{content:"✓ ";color:var(--ok);font-weight:700}
.checks div.bad::before{content:"✗ ";color:var(--crit)}
footer{font-size:12px;color:var(--mut);border-top:1px solid var(--line);padding-top:10px;display:grid;gap:3px}
details.rules>summary{font-family:var(--serif)}
.rule{display:grid;gap:2px;font-size:13px}
.rule b{font-weight:600}
.rule p{margin:0;color:var(--mut);max-width:62em}
sup.fn{font-size:10px;color:var(--acc);margin-left:2px}
.fns{margin:6px 0 0;padding-left:2.2em;font-size:12px;color:var(--mut);display:grid;gap:2px;max-width:62em}
.fns li::marker{color:var(--acc)}
.fns b{color:var(--fg);font-weight:500;margin-right:6px}
.cap{display:flex;flex-wrap:wrap;gap:2px 16px;font-size:12px;color:var(--mut);margin:-4px 0 8px}
.cap b{color:var(--fg);font-weight:600;margin-right:4px}
.spark{display:flex;flex-wrap:wrap;gap:10px}
.spark figure{margin:0;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:8px 10px;font-size:12px;color:var(--mut)}
.spark svg{display:block;margin-top:4px}
.vv thead th,.vv th.sticky,.vv td.sticky{position:relative;top:auto;left:auto}
.vv thead th{transform:translateY(var(--sy,0px))}
.vv td.sticky{transform:translateX(var(--sx,0px))}
.vv thead th.sticky{transform:translate(var(--sx,0px),var(--sy,0px))}
@media (max-width:480px){ .kpi .v{font-size:18px} th,td{padding:6px 7px} }
"""

# 手机上页面比屏幕宽时，原生 sticky 只认被撑大的布局视口；按 visualViewport 把表头往下推、首列往右推。
JS = r"""
(function () {
  var vv = window.visualViewport, d = document;
  if (!vv || !window.requestAnimationFrame) return;
  var tables = [].slice.call(d.querySelectorAll('.tw > table'));
  if (!tables.length) return;
  d.documentElement.classList.add('vv');
  var queued = false;
  function update() {
    queued = false;
    var sx = window.scrollX, sy = window.scrollY, top = vv.pageTop, left = vv.pageLeft;
    tables.forEach(function (t) {
      var r = t.getBoundingClientRect();
      if (!r.height) return;
      var head = t.tHead ? t.tHead.getBoundingClientRect().height : 0;
      var cell = t.rows[0] && t.rows[0].cells[0];
      var first = cell ? cell.getBoundingClientRect().width : 0;
      var dy = Math.max(0, Math.min(top - (r.top + sy), r.height - head * 2));
      var dx = Math.max(0, Math.min(left - (r.left + sx), r.width - first));
      t.style.setProperty('--sy', dy + 'px');
      t.style.setProperty('--sx', dx + 'px');
    });
  }
  function queue() { if (!queued) { queued = true; window.requestAnimationFrame(update); } }
  vv.addEventListener('scroll', queue);
  vv.addEventListener('resize', queue);
  window.addEventListener('scroll', queue, {passive: true});
  window.addEventListener('resize', queue);
  d.addEventListener('toggle', queue, true);
  queue();
})();
"""

RULES = (
    ("（一）编制基础", "本表只用四类事实编制：背包盘点读数、顶栏货币读数、流程走到各个位置时留的标记、商店逐笔记下的补买、出售与料理制作。"
                    "报表在生成时现场计算，重新生成不改动任何记录。"),
    ("（二）会计期间与结账", "游戏日从 09:00 KST 起算。相邻两次盘点之间是一段，归到它<b>开始</b>那次盘点所在的游戏日："
                       "一日的期初是当天第一次盘点，期末是下一个游戏日第一次盘点的读数，隔夜那一段记在上一日。"
                       "下一个游戏日的盘点到来之前只能记账，按期中列报，期末暂取当天最后一次盘点；到来之后结账定稿，按期末列报。"),
    ("（三）单位与写法", "数量以「个」计，金额以金币计；满一万以「万」计，保留一位小数。负数以括号表示并着红色。"
                    "「–」为零，「·」为这一段两头的盘点没读到它，「不扫」为这一列的盘点不盘这一科目。"),
    ("（四）科目", "分货币票券、存货、养成材料三类。金币按面值；钻石、天赋神药、票券与养成材料不估值，只报数量。"
                "没有负债，资产合计即所有者权益。"),
    ("（五）存货计价", "存货按<b>当月出售列表最高价</b>计价，月内一般不变；没读到行情时用内置的参考价，并在单价格里标出。"
                  "数量变动按变动后的单价计入各列；单价本身变了，对原有存货的重估差额单列「公允价值变动」，不归任何一列。"),
    ("（六）各列的归属", "每一段按流程留下的标记归到日常、吸取或商店；没有任何任务的一段记为期间外，通常是手动操作。"
                    "同一天同一类的各段把变化相加成一列。一段里混进了两类任务、或夹着没接记账的任务，整段单列「混合」，不拆。"
                    "任务没有当场结清（收尾的盘点隔了很久才来）时，变化仍记在它那一列，列头注明，其中混有期间外。"),
    ("（七）扫描范围与跨区间合并", "每类任务只盘它会动的科目。某次盘点不盘、或没读准某样东西时，它前后的几段合成一笔，"
                           "列在「跨区间」一栏并注明原因；当天最后一次读到它的盘点即为它的期末。"
                           "结账的那次盘点没读到某样东西时，上一日已定稿、不再改：这一天的期初取当天第一次读到的数，"
                           "与上次读数的差单列在「<b>期初调整</b>」表里（平时没有这张表），上日期末 + 期初调整 = 本日期初，"
                           "不计入本日各列与损益。"),
    ("（八）推算与余额项", "料理制作份数取制作时记下的读数；材料消耗按配方展开。常规采购不逐笔记，是余额项，"
                     "等于盘点净额减去已记账与推算部分；不可购买的材料余额项应为零。推算结果只在报表里，不写回存档。"),
    ("（九）自检", "每张表都核对恒等式，结果列在页末。任何一条不成立时进通知并标红，表照样画出，便于排查。"),
    ("（十）吸取分卡产出", "放技能时读屏幕左侧的获得列表，每进一次卡记一笔。各张卡列它自己读到的数，不按盘点缩放；"
                     "盘点与各卡读数之和的差单列「卡带未知」，可正可负（走路碰到的、没读到的、吸完却没有记录的卡，说不清是哪张卡的）。"
                     "同一张卡一天里进了几次合成一行。盘点不扫的东西只列读数，不计入。"),
)


def esc(text) -> str:
    return html.escape(str(text), quote=True)


def clock(game_day: str):
    """把 ISO 时刻写成页面上的 KST 时刻；不在游戏日当天的带上「次日」或日期。"""
    def show(text) -> str:
        try:
            moment = datetime.fromisoformat(str(text)).astimezone(KST)
        except ValueError:
            return str(text)
        day = moment.date().isoformat()
        time = f"{moment.hour:02d}:{moment.minute:02d}"   # 不用 strftime：Windows 上格式串里带中文会编码失败
        if day == game_day:
            return time
        following = (datetime.fromisoformat(game_day) + timedelta(days=1)).date().isoformat()
        return f"次日 {time}" if day == following else f"{moment.month:02d}-{moment.day:02d} {time}"
    return show


def fmt(value, *, sign=False) -> str:
    """数量：千分位；负数带括号并着色；0 写成 –；没有读数写成 ·。"""
    if value is None:
        return '<span class="dim">·</span>'
    if value == 0:
        return '<span class="zero">–</span>'
    if value < 0:
        return f'<span class="neg">({abs(value):,})</span>'
    return f"+{value:,}" if sign else f"{value:,}"


def wan(value, *, sign=False) -> str:
    """金额的紧凑写法：满一万用「万」带一位小数。"""
    if value is None:
        return "·"
    size = abs(value)
    body = f"{size / 10_000:,.1f}万" if size >= 10_000 else f"{size:,}"
    if value < 0:
        return f"({body})"
    return f"+{body}" if sign and value > 0 else body


def amount(value, *, sign=False) -> str:
    if value is None:
        return '<span class="dim">·</span>'
    if value == 0:
        return '<span class="zero">–</span>'
    text = wan(value, sign=sign)
    return f'<span class="neg">{text}</span>' if value < 0 else text


def cap(*parts) -> str:
    return '<div class="cap">' + "".join(f"<span><b>{esc(key)}</b>{value}</span>" for key, value in parts) + "</div>"


def cell(quantity, price=None, *, sign=False) -> str:
    """数量金额式：上行数量，有单价时下行金额。"""
    if quantity is None or price is None:
        return fmt(quantity, sign=sign)
    return f'<span class="q">{fmt(quantity, sign=sign)}</span><span class="amt">{amount(quantity * price, sign=sign)}</span>'


def _unit(row):
    return row["unit"] if row else None


def card_name(key: str) -> str:
    """周期记录里的卡带键名（Story_10_霍尔蒙克斯）→ 页面上的名字（剧情 10 霍尔蒙克斯）。认不出格式的原样返回。"""
    kind, _, rest = str(key).partition("_")
    number, _, name = rest.partition("_")
    label = {"Story": "剧情", "Event": "活动", "Character": "角色"}.get(kind)
    return f"{label} {number} {name}" if label and number.isdigit() and name else str(key)


def _header(model, show):
    status = model["status"]
    account = model["account"]
    last = model["scans"][-1]
    return (
        '<header class="sheet">'
        f'<h1>库存日报 <span class="badge {status}">{STATUS_LABEL[status]}</span></h1><div class="meta">'
        f'<span>编制单位 <b>{esc(account.get("alias") or "默认存档")}</b> <span class="dim">{esc((account.get("hash") or "")[:8])}</span></span>'
        f'<span>游戏日 <b>{esc(model["game_day"])}</b> <span class="dim">（09:00 KST 起）</span></span>'
        f'<span>编制时刻 <b>{esc(show(model["as_of"]))} KST</b></span>'
        f'<span>最后一次盘点 <b>{esc(show(last["at"]))}</b></span>'
        '<span>单位 <b>个 / 金币</b>，详见适用准则</span></div></header>')


def _kpis(model):
    kpis, valued = model["kpis"], model["valuation"] is not None
    parts = [f"{label} {fmt(kpis[key], sign=True)}" for label, key in (("商店", "shop_cash"), ("期间外", "outside_cash"))
             if kpis[key] is not None]
    out = ['<section class="kpis">',
           f'<div class="kpi"><div class="k">金币净变动</div><div class="v">{fmt(kpis["gold_delta"], sign=True)}</div>'
           f'<div class="s">{" · ".join(parts) or "顶栏金币的盘点差"}</div></div>']
    if valued:
        out.append(f'<div class="kpi"><div class="k">本日损益（现金 + 存货估值）</div><div class="v">{amount(kpis["profit"], sign=True)}</div>'
                   f'<div class="s">其中存货估值 {amount(kpis["stock_delta"], sign=True)}</div></div>')
    top = " · ".join(f"{esc(name)} +{quantity:,}" for name, quantity in kpis["collect_top"])
    worth = f' ≈{wan(kpis["collect_value"])}' if valued and kpis["collect_value"] else ""
    out.append(f'<div class="kpi"><div class="k">吸取产出</div><div class="v">{kpis["collect_items"]} 项{worth}</div>'
               f'<div class="s">{top or "当天没有吸取的变化"}</div></div>')
    out.append(f'<div class="kpi"><div class="k">盘点</div><div class="v">{kpis["scans"]} 次</div>'
               f'<div class="s">共 {kpis["scan_seconds"] // 60} 分 {kpis["scan_seconds"] % 60:02d} 秒</div></div></section>')
    return "".join(out)


def _timeline(model, show):
    segments = model["segments"]
    if not segments:
        return ""
    out = ["<section><h2>时间轴 <small>宽度按时长；斜纹 = 不属于任何一类任务</small></h2>",
           cap(("期间", f'{esc(show(segments[0]["start"]))} → {esc(show(segments[-1]["end"]))} 盘点'), ("时刻", "KST")),
           '<div class="tl">']
    for segment in segments:
        squeezed = " compressed" if segment["minutes"] > 180 else ""   # 隔夜那样的长段压成固定宽度，免得把别的挤没
        label = KIND_LABEL[segment["kind"]]
        out.append(f'<div class="seg {segment["kind"]}{squeezed}" style="flex:{max(segment["minutes"], 1)} 1 0" '
                   f'title="{esc(label)} {esc(show(segment["start"]))}→{esc(show(segment["end"]))}（{segment["minutes"]} 分）">{esc(label)}</div>')
    out.append('</div><div class="ticks">' + "".join(f"<span>{esc(show(moment))}</span>" for moment in dict.fromkeys(
        [segments[0]["start"]] + [segment["end"] for segment in segments])) + "</div>")
    out.append('<div class="legend">' + "".join(
        f'<span><i style="background:var(--c-{key})"></i>{esc(KIND_LABEL[key])}</span>'
        for key in dict.fromkeys(segment["kind"] for segment in segments)) + "</div></section>")
    return "".join(out)


def _stock_table(model, show):
    cols, valued = model["cols"], model["valuation"] is not None
    with_spans = any(item["spans"] for item in model["items"])
    first, last = model["scans"][0], model["scans"][-1]
    width = len(cols) + 3 + valued + with_spans
    out = ["<section><h2>一、进销存表 <small>逐物品：期初 + 各列 = 期末"
           + ("；存货行上行数量、下行金额" if valued else "") + "</small></h2>",
           cap(("期间", f'{esc(model["game_day"])} 游戏日，{esc(show(first["at"]))} → {esc(show(last["at"]))} 盘点'),
               ("单位", "数量：个" + ("；金额：金币，满万以万计" if valued else ""))),
           '<div class="tw"><table><thead><tr><th class="sticky">物品</th>']
    if valued:
        out.append('<th class="num">单价<span class="sub">月度最高价</span></th>')
    out.append(f'<th class="num">期初<span class="sub">{esc(show(first["at"]))}</span></th>')
    for col in cols:
        notes = [f'{col["runs"]} 段'] + [FLAG_LABEL[flag] for flag in col["flags"] if flag in ("abnormal_end", "unsettled")]
        if col["key"] in ("daily", "collect", "shop"):
            notes.append("盘 " + ("+".join(col["scopes"]) or "–"))
        out.append(f'<th class="num col-{col["key"]}">{esc(col["label"])}<span class="sub">{esc(" · ".join(notes))}</span></th>')
    if with_spans:
        out.append('<th class="num">跨区间<span class="sub">几段合成一笔</span></th>')
    out.append(f'<th class="num">期末<span class="sub">{esc(show(last["at"]))}</span></th></tr></thead><tbody>')
    notes = []
    for key, title in GROUPS:
        rows = [item for item in model["items"] if item["group"] == key]
        if not rows:
            continue
        out.append(f'<tr class="grp"><td class="sticky" colspan="{width}">{esc(title)}</td></tr>')
        for item in sorted(rows, key=lambda row: (-abs(row["total"] * (_unit(row["price"]) or 1)), row["name"])):
            unit, unit_open = _unit(item["price"]), _unit(item["price_open"])
            out.append(f'<tr><td class="sticky">{esc(item["name"])}</td>')
            if valued:
                if unit is None:
                    out.append('<td class="num"><span class="dim">·</span></td>')
                else:
                    basis = '<span class="amt">参考价</span>' if item["price"].get("basis") == "reference" else ""
                    older = f'<span class="amt">期初 {unit_open:,}</span>' if unit_open not in (None, unit) else ""
                    out.append(f'<td class="num"><span class="q">{unit:,}</span>{older}{basis}</td>')
            early = f'<span class="amt">{esc(item["opening_at"])}</span>' if item["opening_at"] != first["id"] else ""
            out.append(f'<td class="num">{cell(item["opening"], unit_open)}{early}</td>')
            for col in cols:
                if col["key"] in item["deltas"]:
                    out.append(f'<td class="num">{cell(item["deltas"][col["key"]], unit, sign=True)}</td>')
                elif col["key"] in item["gaps"]:
                    out.append('<td class="gap">不扫</td>')
                else:
                    out.append('<td class="num"><span class="dim">·</span></td>')
            if with_spans:
                if item["spans"]:
                    marks = []
                    for span in item["spans"]:
                        merged = "、".join(KIND_LABEL[kind] for kind in span["cols"])
                        notes.append((item["name"], f'{merged}合成一笔（{span["from"]}→{span["to"]}）：'
                                      f'{REASONS.get(span["reason"], "这一笔跨到了下一个游戏日")}'))
                        marks.append(f'<sup class="fn">{len(notes)}</sup>')
                    total = sum(span["delta"] for span in item["spans"])
                    out.append(f'<td class="num span">{cell(total, unit, sign=True)}{"".join(marks)}</td>')
                else:
                    out.append('<td class="num"><span class="zero">–</span></td>')
            late = f'<span class="amt">截至 {esc(item["closing_at"])}</span>' if item["closing_at"] != last["id"] else ""
            out.append(f'<td class="num"><b>{cell(item["closing"], unit)}</b>{late}</td></tr>')
        if valued and key in VALUED:
            value = model["valuation"][key]
            flows = sum(value["flows"].values()) + value["spans"]
            out.append(f'<tr class="val"><td class="sticky" colspan="{width}">估值小计（含未变动的）· 期初 {wan(value["opening"])}'
                       f' · 各列 {wan(flows, sign=True)} · 单价调整 {wan(value["reval"], sign=True)} · 期末 {wan(value["closing"])}</td></tr>')
    unchanged = model["unchanged"]
    if not model["items"]:
        out.append(f'<tr class="sub"><td class="sticky" colspan="{width}">当天还没有可比较的两次盘点，或没有任何变动</td></tr>')
    out.append("</tbody></table></div>")
    if notes:
        out.append('<ol class="fns">' + "".join(f"<li><b>{esc(name)}</b>{esc(text)}</li>" for name, text in notes) + "</ol>")
    held = [row for row in unchanged if row["quantity"]]
    if unchanged:
        worth = sum(row["value"] or 0 for row in unchanged)
        none = len(unchanged) - len(held)
        out.append(f'<details class="plain"><summary>未变动的 {len(held)} 项 <small>两头读数相同'
                   + (f"，估值 {wan(worth)}" if valued and worth else "")
                   + (f"；另有 {none} 项一直是 0，不列" if none else "") + '</small></summary><div class="body"><div class="small mut">'
                   + esc("、".join(f'{row["name"]} {row["quantity"]:,}' for row in held)) + "</div></div></details>")
    if model["unread"]:
        out.append(f'<details class="plain"><summary>当天没读到的 {len(model["unread"])} 项 <small>以前盘到过，当天的盘点都没读到</small></summary>'
                   f'<div class="body"><div class="small mut">{esc("、".join(model["unread"]))}</div></div></details>')
    out.append("</section>")
    return "".join(out)


def _adjust_table(model, show):
    """期初调整表：只在有物品的本日期初与上日期末对不上时出现，只列对不上的那几样（U67-1、U69）。"""
    rows = model.get("adjustments") or []
    if not rows:
        return ""
    closer = model["scans"][0]
    out = ["<section><h2>期初调整 <small>本日期初与上日期末对不上的物品</small></h2>",
           cap(("为什么", f'上一日结账的那次盘点（{esc(show(closer["at"]))}）没读到它们，本日期初取当天第一次读到的数；'
                          "这段差额分不出发生在哪一天，不计入本日各列与损益"),
               ("恒等式", "上日期末 + 期初调整 = 本日期初")),
           '<div class="tw"><table><thead><tr><th class="sticky">物品</th><th class="num">上日期末<span class="sub">上次读到的数</span></th>'
           '<th class="num">期初调整</th><th class="num">本日期初<span class="sub">当天第一次读到的数</span></th></tr></thead><tbody>']
    for row in rows:
        before, unit = _unit(row.get("price_before")), _unit(row.get("price"))
        change = (f'<span class="q">{fmt(row["delta"], sign=True)}</span><span class="amt">{amount(row["value"], sign=True)}</span>'
                  if row.get("value") is not None else fmt(row["delta"], sign=True))
        if None not in (before, unit) and before != unit:   # 金额差里含单价变化，数量减了金额也可能增
            change += f'<span class="amt">含单价 {before:,} → {unit:,}</span>'
        why = ADJUST_REASONS.get(row["reason"], "没读到它")
        out.append(f'<tr><td class="sticky">{esc(row["name"])}</td>'
                   f'<td class="num">{cell(row["before"], before)}<span class="amt">{esc(show(row["before_time"]))}</span></td>'
                   f'<td class="num">{change}<span class="amt">结账那一盘{esc(why)}</span></td>'
                   f'<td class="num"><b>{cell(row["opening"], unit)}</b><span class="amt">{esc(show(row["opening_time"]))}</span></td></tr>')
    out.append("</tbody></table></div></section>")
    return "".join(out)


def _shop_table(model):
    shop = model["shop"]
    if not shop:
        return ""
    money = shop["gold"]
    notes = ["出售按成交时核对过的金币差"]
    if money.get("carried"):
        notes.append(f"其中 {money['carried']:,} 是金币没核上、回读库存确认全部成交，按开窗时的报价记")
    if money.get("unknown_sales"):
        notes.append(f"{'、'.join(money['unknown_sales'])} 的出售没核到金币，没有计入，混在余额项里")
    if money["unconfirmed_sales"]:
        notes.append(f'另有 {money["unconfirmed_sales"]} 笔出售未确认，未计入')
    regular = money.get("regular", True)
    bad = "" if regular else " bad"
    out = ["<section><h2>二、商店账实核对 <small>逐笔记下的 对 两头盘点</small></h2>",
           cap(("期间", "当天的商店各段"), ("单位", "个（数量表）；金币一行为金币")),
           '<div class="tw"><table><thead><tr><th class="sticky">物品</th><th class="num">盘点净额</th>'
           '<th class="num">已记账<span class="sub">补买买入</span></th><th class="num">已记账<span class="sub">出售</span></th>'
           '<th class="num">制作 ±<span class="sub">成品记账，材料按配方</span></th>'
           '<th class="num">余额项<span class="sub">= 常规采购（推算）</span></th><th>依据</th></tr></thead><tbody>',
           '<tr class="grp"><td class="sticky" colspan="7">金币</td></tr>',
           f'<tr><td class="sticky">金币</td><td class="num">{fmt(money["net"], sign=True)}</td>'
           f'<td class="num">{fmt(money["replenish"], sign=True)}</td><td class="num">{fmt(money["sales"], sign=True)}</td>'
           f'<td class="num"><span class="dim">·</span></td><td class="num{bad}">{fmt(money["residual"], sign=True)}</td>'
           f'<td class="note{bad}">{esc(("不是常规采购：" if not regular else "") + "；".join(notes))}</td></tr>']
    if shop["rows"]:
        out.append('<tr class="grp"><td class="sticky" colspan="7">料理 / 食材</td></tr>')
    for row in shop["rows"]:
        bad = ' bad' if row["mismatch"] else ""
        out.append(f'<tr><td class="sticky">{esc(row["name"])}</td><td class="num">{fmt(row["net"], sign=True)}</td>'
                   f'<td class="num">{fmt(row["replenish"] or 0, sign=True)}</td><td class="num">{fmt(row["sales"] or 0, sign=True)}</td>'
                   f'<td class="num">{fmt(row["cooking"] or 0, sign=True)}</td><td class="num{bad}">{fmt(row["residual"], sign=True)}</td>'
                   f'<td class="note{bad}">{esc(("推算失配：" if row["mismatch"] else "") + row["basis"])}</td></tr>')
    out.append('</tbody></table></div><p class="small mut">补买、出售、料理制作是逐笔记下的事实；材料消耗按配方展开。'
               "常规采购不逐笔记，是余额项——不可购买的材料余额项应为 0，这是推算的自检。</p></section>")
    return "".join(out)


def _assets_table(model, show):
    assets, first, last = model["assets"], model["scans"][0], model["scans"][-1]
    out = ["<section><h2>三、资产表 <small>时点家底。没有负债，所有者权益 ≡ 资产</small></h2>",
           cap(("时点", f'期初 {esc(show(first["at"]))} · 期末 {esc(show(last["at"]))} KST'),
               ("单位", "货币票券与养成材料为个；存货与合计为金币，满万以万计")),
           '<div class="tw"><table><thead><tr><th class="sticky">科目</th><th class="num">期初</th><th class="num">期末</th>'
           '<th>口径</th></tr></thead><tbody><tr class="grp"><td class="sticky" colspan="4">货币票券</td></tr>']
    for row in assets["currency"]:
        note = "面值" if row["name"] == GOLD else "不估值，只报数量"
        early = f'<span class="amt">{esc(row["opening_at"])}</span>' if row.get("opening_at") not in (None, first["id"]) else ""
        late = f'<span class="amt">截至 {esc(row["closing_at"])}</span>' if row.get("closing_at") not in (None, last["id"]) else ""
        out.append(f'<tr><td class="sticky">{esc(row["name"])}</td><td class="num">{fmt(row["opening"])}{early}</td>'
                   f'<td class="num"><b>{fmt(row["closing"])}</b>{late}</td><td class="note">{note}</td></tr>')
    for group in assets["groups"]:
        value = group["value"]
        if value:
            out.append(f'<tr><td class="sticky">{esc(group["label"])} {group["read"]} 项</td><td class="num">{wan(value["opening"])}</td>'
                       f'<td class="num"><b>{wan(value["closing"])}</b></td><td class="note">按月度最高价</td></tr>')
        else:
            out.append(f'<tr><td class="sticky">{esc(group["label"])}</td><td class="num">{group["read"]} 项有读数</td>'
                       f'<td class="num">{group["read"]} 项有读数</td><td class="note">不估值，数量见表一</td></tr>')
    total = assets["total"]
    label = "资产合计 = 所有者权益" if total["valued"] else "金币（存货未估值）"
    out.append(f'<tr class="sum"><td class="sticky">{label}</td><td class="num">{wan(total["opening"])}</td>'
               f'<td class="num">{wan(total["closing"])}</td><td class="note">金币面值 + 存货估值；不估值的科目不进合计</td></tr>'
               "</tbody></table></div></section>")
    return "".join(out)


def _statement_table(model):
    statement, valued = model["statement"], model["valuation"] is not None
    out = ["<section><h2>四、收益与现金流量表 <small>左列口袋里的，中列仓库里按单价折的，右列两者之和</small></h2>",
           cap(("期间", f'{esc(model["game_day"])} 游戏日'), ("单位", "金币，满万以万计（金额表）")),
           '<div class="tw"><table><thead><tr><th class="sticky">项目</th><th class="num">现金<span class="sub">金币</span></th>'
           '<th class="num">存货估值变动</th><th class="num">损益<span class="sub">= 现金 + 存货</span></th><th>依据</th></tr></thead><tbody>']
    carried, adjustment = statement.get("carried"), statement.get("adjustment")
    if carried and adjustment:
        out.append(f'<tr><td class="sticky">上日期末</td><td class="num">{fmt(carried["cash"])}</td>'
                   f'<td class="num">{wan(carried["stock"])}</td><td class="num">{wan(carried["total"])}</td>'
                   '<td class="note">各物品上次读到的数</td></tr>'
                   f'<tr class="sub"><td class="sticky">期初调整</td><td class="num">{fmt(adjustment["cash"], sign=True)}</td>'
                   f'<td class="num">{amount(adjustment["stock"], sign=True)}</td><td class="num">{amount(adjustment["total"], sign=True)}</td>'
                   '<td class="note">上一日结账那次盘点没读到的，补上读数后的差；不计入本日合计</td></tr>')
    out.append(f'<tr><td class="sticky">期初</td><td class="num">{fmt(statement["opening"]["cash"])}</td>'
               f'<td class="num">{wan(statement["opening"]["stock"])}</td><td class="num">{wan(statement["opening"]["total"])}</td>'
               f'<td class="note">{"调整后，" if adjustment else ""}当天第一次盘点</td></tr>')
    current = None
    for row in statement["rows"]:
        if row["group"] != current:
            current = row["group"]
            out.append(f'<tr class="grp"><td class="sticky" colspan="5">{GROUP_TITLE[current]}</td></tr>')
        tag = f'<span class="tag {row["col"]}">{esc(KIND_LABEL[row["col"]])}</span> ' if row["col"] and not row["part"] else ""
        profit = None if row["cash"] is None or row["part"] else row["cash"] + (row["stock"] or 0)
        stock = '<td class="num"><span class="dim">·</span></td>' if row["part"] or not valued \
            else f'<td class="num">{amount(row["stock"], sign=True)}</td>'
        out.append(f'<tr{" class=sub" if row["part"] else ""}><td class="sticky">{tag}{esc(row["label"])}</td>'
                   f'<td class="num">{fmt(row["cash"], sign=True)}</td>{stock}'
                   f'<td class="num">{amount(profit, sign=True)}</td><td class="note">{BASIS_LABEL[row["basis"]]}'
                   + ("，已含在下一行" if row["part"] else "") + "</td></tr>")
    totals = statement["totals"]
    out.append(f'<tr class="sum"><td class="sticky">本日合计</td><td class="num">{fmt(totals["cash"], sign=True)}</td>'
               f'<td class="num">{amount(totals["stock"], sign=True)}</td><td class="num">{amount(totals["profit"], sign=True)}</td>'
               '<td class="note">现金列 = 金币盘点差</td></tr>'
               f'<tr class="sum"><td class="sticky">期末</td><td class="num">{fmt(statement["closing"]["cash"])}</td>'
               f'<td class="num">{wan(statement["closing"]["stock"])}</td><td class="num">{wan(statement["closing"]["total"])}</td>'
               '<td class="note">= 期初 + 本日合计 = 表三合计</td></tr>')
    if statement["other_currencies"]:
        others = "、".join(f"{name} {'(' + format(-value, ',') + ')' if value < 0 else '+' + format(value, ',')}"
                          for name, value in statement["other_currencies"].items())
        out.append(f'<tr class="sub"><td class="sticky" colspan="5">其他货币与票券不计入合计：{esc(others)}</td></tr>')
    out.append("</tbody></table></div>")
    if not valued:
        out.append('<p class="small mut">存货还没有单价，本表只有现金一列。</p>')
    out.append("</section>")
    return "".join(out)


def _products(items, read_only=()) -> str:
    """一格里的产出：「金币 +3,000、兽肉 +240」；只有读数的东西跟在后面、着淡色。"""
    text = "、".join(f'{esc(item["name"])} {fmt(item["quantity"], sign=True)}' for item in items)
    seen = "、".join(f'{esc(item["name"])} {item["quantity"]:,}' for item in read_only)
    if seen:
        text += ("；" if text else "") + f'<span class="dim">读到 {seen}（盘点不扫，未计入）</span>'
    return text or '<span class="zero">–</span>'


def _collect_section(model):
    """五、吸取：有掉落记录时按卡带列产出（盘点总数按读数分摊），没有时只列吸完的卡。"""
    collect = model["collect"]
    if not collect["runs"]:
        return ""
    state = f'<span class="chip warn">{collect["abnormal"]} 段未正常结束</span>' if collect["abnormal"] \
        else '<span class="chip ok">正常结束</span>'
    by_card = collect.get("by_card")
    if not by_card or not by_card["recorded"]:
        chips = "".join(f'<span class="chip">{esc(card_name(card))}</span>' for card in collect["cards"] or ())
        return (f'<section><h2>五、吸取 <small>{collect["runs"]} 段</small></h2><div class="chips">{state}{chips}</div>'
                '<p class="small mut">没有记录吸取掉落（「记录吸取掉落」关着），不拆分卡产出；产出见表一「吸取」列。'
                + ("" if collect["cards"] is not None else "没走到正常结束点的那一段，卡带不记名。") + "</p></section>")
    valued = model["valuation"] is not None
    out = [f'<section><h2>五、吸取 · 分卡产出 <small>{collect["runs"]} 段</small></h2><div class="chips">{state}</div>',
           cap(("期间", f'{esc(model["game_day"])} 游戏日的吸取各段'),
               ("单位", "个" + ("；折合为金币，满万以万计" if valued else ""))),
           '<div class="tw"><table><thead><tr><th class="sticky">卡带</th><th>进入</th>'
           '<th>产出<span class="sub">各卡读到的数；与盘点的差在「卡带未知」</span></th>'
           + ('<th class="num">折合金币<span class="sub">金币面值 + 存货估值</span></th>' if valued else "")
           + "</tr></thead><tbody>"]
    for row in by_card["rows"]:
        name = card_name(row["card"]) if row["card"] else "未识别卡带"
        if not row["recorded"]:
            out.append(f'<tr class="sub"><td class="sticky">{esc(name)}</td><td>吸完</td>'
                       f'<td class="note">未记录掉落</td>' + ('<td class="num"><span class="dim">·</span></td>' if valued else "")
                       + "</tr>")
            continue
        entered = f'{row["visits"]} 次' + (" · 吸完" if row["complete"] or row["finished"] else " · 没吸完")
        notes = [f'{row["points"]} 个点']
        if row["unread"]:
            notes.append(f'{row["unread"]} 条没读成')
        if row["stuck"]:
            notes.append(f'{row["stuck"]} 个点列表卡住')
        worth = ""
        if valued:
            extra = f'<span class="amt">{len(row["unpriced"])} 项没有单价</span>' if row["unpriced"] else ""
            worth = f'<td class="num">{amount(row["value"])}{extra}</td>'
        out.append(f'<tr><td class="sticky">{esc(name)}</td><td>{esc(entered)}<span class="amt">{esc("，".join(notes))}</span></td>'
                   f'<td class="note">{_products(row["items"], row["read_only"])}</td>{worth}</tr>')
    rest = by_card["unallocated"]
    if rest["items"]:
        worth = f'<td class="num">{amount(rest["value"])}</td>' if valued else ""
        out.append(f'<tr class="val"><td class="sticky">卡带未知</td><td></td><td class="note">{_products(rest["items"])}</td>'
                   f"{worth}</tr>")
    out.append("</tbody></table></div>")
    runs, recorded, covered = by_card["runs"], by_card["recorded"], by_card["covered"]
    basis = ("各卡是它自己读到的数，不按盘点缩放；盘点与读数的差列在「卡带未知」：正数是盘点比读到的多（走路碰到的、没读到的），"
             "负数是读到的比盘点多。")
    if covered < runs:
        basis += (f"{runs} 段吸取里有 {runs - covered} 段" + ("没有掉落记录或" if recorded < runs else "")
                  + "有吸完的卡没记录，那几张卡的产出也在「卡带未知」里。")
    out.append(f'<p class="small mut">{basis}各卡加上卡带未知等于表一「吸取」列（自检 C8）。'
               "同一张卡一天里进了几次合成一行；读数来自放技能时左侧的获得列表，只用来分摊，总数以盘点为准。</p></section>")
    return "".join(out)


def _details(model, show):
    out = ['<details><summary>各段与任务明细 <small>每一段里跑过什么、怎么结束</small></summary><div class="body"><div class="segs">']
    for segment in model["segments"]:
        kind = segment["kind"]
        chips = "".join(f'<span class="chip warn">{esc(FLAG_LABEL.get(flag, flag))}</span>' for flag in segment["flags"])
        if kind in ("collect", "shop") and "abnormal_end" not in segment["flags"]:
            chips += '<span class="chip ok">正常结束</span>'
        out.append(f'<div class="segc {kind}"><div class="hd"><span class="tag {kind}">{esc(KIND_LABEL[kind])}</span>'
                   f'<b>{esc(show(segment["start"]))} → {esc(show(segment["end"]))}</b>'
                   f'<span class="mut small">{segment["minutes"]} 分 · 盘点 {esc(segment["from"])}→{esc(segment["to"])}</span>'
                   f'<span class="chips">{chips}</span></div>')
        if segment["tasks"]:
            out.append("<ul>" + "".join(
                f'<li>{esc(task["label"])} <span class="dim">{esc(show(task["start"]) if task["start"] else "?")}–'
                f'{esc(show(task["end"]) if task["end"] else "?")}</span></li>' for task in segment["tasks"]) + "</ul>")
        if segment["cards"]:
            out.append(f'<div class="small mut">卡带：{esc("、".join(card_name(card) for card in segment["cards"]))}</div>')
        out.append("</div>")
    if not model["segments"]:
        out.append('<div class="small mut">当天只有一次盘点，还没有成段。</div>')
    out.append('</div></div></details><details><summary>盘点记录 <small>'
               f'{len(model["scans"])} 次</small></summary><div class="body"><div class="tw"><table><thead><tr>'
               '<th class="sticky">#</th><th>时刻</th><th>位置</th><th>范围</th><th class="num">秒</th><th class="num">页</th>'
               '<th class="num">读到</th><th class="num">判 0</th><th>未知</th><th class="num">金币</th></tr></thead><tbody>')
    for scan in model["scans"]:
        unknown = esc("、".join(scan["unknown"])) or '<span class="zero">–</span>'
        out.append(f'<tr><td class="sticky">{esc(scan["id"])}</td><td>{esc(show(scan["at"]))}</td><td>{esc(scan["label"] or "")}</td>'
                   f'<td>{esc("+".join(scan["scope"]))}{"" if scan["complete"] else "（没盘完）"}'
                   f'{"（存货退出重扫过）" if scan.get("retried") else ""}</td>'
                   f'<td class="num">{scan["seconds"]}</td><td class="num">{scan["pages"]}</td><td class="num">{scan["read"]}</td>'
                   f'<td class="num">{scan["absent"]}</td><td>{unknown}</td><td class="num">{fmt(scan["gold"])}</td></tr>')
    out.append("</tbody></table></div></div></details>")
    return "".join(out)


def _page(title, body, *, script=True) -> str:
    body = re.sub(r'(<tr class="[a-z]+">)<td class="sticky" colspan="(\d+)">(.*?)</td></tr>',
                  lambda match: f'{match.group(1)}<td class="sticky">{match.group(3)}</td>'
                                f'<td colspan="{int(match.group(2)) - 1}"></td></tr>', body)
    return ('<!doctype html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            f'<meta name="color-scheme" content="light dark">\n<title>{esc(title)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n'
            f'<div class="wrap">\n{body}\n</div>\n' + (f"<script>{JS}</script>\n" if script else "") + "</body>\n</html>\n")


def render_day_html(model) -> str:
    """某一个游戏日的日报。"""
    show = clock(model["game_day"])
    parts = [_header(model, show)]
    if model["notices"]:
        parts.append('<section class="alerts">' + "".join(
            f'<div class="alert {notice["level"]}">{"" if notice["level"] == "info" else "⚠ "}{esc(notice["text"])}</div>'
            for notice in model["notices"]) + "</section>")
    parts.append('<details class="rules"><summary>适用准则 <small>编制基础与统计口径</small></summary><div class="body">'
                 + "".join(f'<div class="rule"><b>{esc(title)}</b><p>{text}</p></div>' for title, text in RULES)
                 + "</div></details>")
    parts += [_kpis(model), _timeline(model, show), _adjust_table(model, show), _stock_table(model, show), _shop_table(model),
              _assets_table(model, show), _statement_table(model), _collect_section(model)]
    parts.append(_details(model, show))
    parts.append('<section><h2>自检</h2><div class="checks">' + "".join(
        f'<div class="{"" if check["ok"] else "bad"}">{esc(check["name"])} <span class="mut">· {esc(check["detail"])}</span></div>'
        for check in model["checks"]) + "</div></section>")
    parts.append(f'<footer><span>做账规则第 {model["rule_version"]} 版 · 归日按每段开始那次盘点所在的游戏日 · '
                 '数据来源：账号存档与库存历史 · 本页由凭证现场生成，重算不改数据</span>'
                 f'<span>摘要：{esc(model["summary"])}</span><span><a href="index.html">多日总览</a></span></footer>')
    return _page(f'库存日报 {model["game_day"]}', "\n".join(part for part in parts if part))


def _spark(values) -> str:
    points = [value for value in values if value is not None]
    if len(points) < 2:
        return ""
    low, high = min(points), max(points)
    spread = (high - low) or 1
    step = 120 / (len(points) - 1)
    path = " ".join(f"{index * step:.1f},{26 - (value - low) / spread * 24:.1f}" for index, value in enumerate(points))
    return f'<svg width="120" height="28" viewBox="0 0 120 28" role="img"><polyline fill="none" stroke="currentColor" stroke-width="1.5" points="{path}"/></svg>'


def render_index_html(models) -> str:
    """多日总览：从各日的报表模型聚合，不重读凭证。"""
    days = sorted(models, key=lambda model: model["game_day"], reverse=True)
    rows = []
    for model in days:
        kpis = model["kpis"]
        rows.append(f'<tr><td class="sticky"><a href="{esc(model["game_day"])}.html">{esc(model["game_day"])}</a></td>'
                    f'<td>{STATUS_LABEL[model["status"]]}</td><td class="num">{fmt(kpis["gold_delta"], sign=True)}</td>'
                    f'<td class="num">{fmt(kpis["shop_cash"], sign=True)}</td><td class="num">{amount(kpis["profit"], sign=True)}</td>'
                    f'<td class="num">{kpis["collect_items"]}</td><td class="num">{kpis["scans"]}</td>'
                    f'<td class="num">{kpis["warnings"] or "–"}</td></tr>')
    oldest = list(reversed(days))
    charts = [("金币余额", [model["statement"]["closing"]["cash"] for model in oldest]),
              ("商店现金净额", [model["kpis"]["shop_cash"] for model in oldest]),
              ("吸取变动项数", [model["kpis"]["collect_items"] for model in oldest])]
    figures = "".join(f"<figure>{esc(title)}{_spark(values)}</figure>" for title, values in charts if _spark(values))
    month = days[0]["game_day"][:7] if days else ""
    monthly = [model["kpis"] for model in days if model["game_day"].startswith(month)]
    body = ('<header class="sheet"><h1>库存总览</h1><div class="meta">'
            f'<span>近 <b>{len(days)}</b> 个有盘点的游戏日</span><span>本月（{esc(month)}）金币净变动 '
            f'<b>{fmt(sum(kpis["gold_delta"] or 0 for kpis in monthly), sign=True)}</b>，商店现金净额 '
            f'<b>{fmt(sum(kpis["shop_cash"] or 0 for kpis in monthly), sign=True)}</b></span></div></header>'
            + (f'<section class="spark">{figures}</section>' if figures else "")
            + '<section><div class="tw"><table><thead><tr><th class="sticky">游戏日</th><th>状态</th><th class="num">金币净变动</th>'
              '<th class="num">商店现金净额</th><th class="num">损益</th><th class="num">吸取项数</th><th class="num">盘点</th>'
              '<th class="num">提醒</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table></div></section>")
    return _page("库存总览", body)


def render_entry_html(accounts, folder: str) -> str:
    """安装目录里的入口页：各存档最近一天的报表。只有一个存档时直接跳过去，跳不过去就点链接。

    accounts：[{name, day, status, summary, as_of, latest, index}]，latest / index 是 file:// 地址；folder 是报表目录的地址。
    """
    rows = []
    for account in accounts:
        when = clock(account["day"])(account["as_of"]) if account.get("as_of") else ""
        links = [f'<a href="{esc(account["latest"])}">{esc(account["day"])} 日报</a>']
        if account.get("index"):
            links.append(f'<a href="{esc(account["index"])}">多日总览</a>')
        rows.append(f'<tr><td class="sticky">{esc(account["name"])}</td><td>{" · ".join(links)}</td>'
                    f'<td>{STATUS_LABEL.get(account.get("status"), "")}</td><td>{esc(when)}</td>'
                    f'<td class="note">{esc(account.get("summary") or "")}</td></tr>')
    jump = ""
    if len(accounts) == 1:
        target = json.dumps(accounts[0]["latest"]).replace("</", "<\\/")
        jump = f"<script>location.replace({target});</script>"
    body = ('<header class="sheet"><h1>库存报表</h1><div class="meta"><span>每次记账盘点后自动更新</span>'
            f'<span>报表都在 <a href="{esc(folder)}">存档目录的 inventory_reports</a> 里，这一页只是入口</span></div></header>'
            '<section><div class="tw"><table><thead><tr><th class="sticky">存档</th><th>报表</th><th>状态</th>'
            '<th>更新（KST）</th><th>摘要</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table></div></section>" + jump)
    return _page("库存报表", body, script=False)
