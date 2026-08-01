# ================================================================
# == RedDotDetector 样本采集器（v3 定标语料地基）                 ==
# ================================================================
# 作用：把每次识别(命中/未命中)的小图 + 完整识别信息落成"语料即回归集"：
#   · 小图：roi_crop / red_mask / inner，唯一命名(时间戳)累积，不覆盖；
#   · samples.jsonl.log：一行一事件(时间/节点/roi/box/conf 四项分解/红块几何/生效参数/关联图)。
#   事后整个文件夹拿走，即可离线回放：改任何参数先过旧语料，再上真机。
#
# 台账为什么叫 .jsonl.log(别改回去)：UI 的"导出日志"按扩展名白名单收集文件，.jsonl
#   不在名单内会被静默丢掉——2026-07 收到的四个用户回流包共 688 张图全部没有台账，
#   根因即此(图是 .png 在名单内，台账不在)。没有台账的图对回放器等于零价值：roi /
#   params / result / box 全在台账里。补一个 .log 后缀即可随包带出，内容仍是 JSONL。
#
# 开关(模式)：RDD_SAMPLE 环境变量(off/fail/all) > maa_option.json 的 rdd_sample > 默认 all。
#   env 穿透运行侧一切配置；fail=只采未命中；off=完全关闭(零开销)。
#
# 位置：RDD_SAMPLE_DIR > maa_option.json 的 rdd_sample_dir > <log_dir>/RedDotDetector_samples。
#   宿主进程对 log_dir 的重定向(如 VSCode 扩展指到 workspaceStorage)在 agent 侧
#   无 API 可读——MaaGlobalOption 只有 setter、没有 getter——故默认位置取
#   本体推算的 <root>/debug/(与 maa.log 同根)；重定向场景用 RDD_SAMPLE_DIR 明示。
#
# 节流去重：同 key(节点+ROI+结果)默认 1800s 最多一张(RDD_SAMPLE_INTERVAL 可调，硬闸，
#   防脉冲动画/自循环灌盘)；间隔过后若画面(roi_crop 哈希)没变仍不采——
#   且不刷新计时，画面一变下一次调用即采。
#
# 容量：目录内 PNG 超 5000 张(RDD_SAMPLE_MAX 可调)即按 mtime **淘汰最旧的**，新的顶掉旧的，
#   任何时候都继续采。不做「达标即停采」——那等于把目录冻结在几个月前，而排查现场要的
#   恰恰是最近那几张。台账只增不删(纯文本、几十 KB)，因此会留下指向已淘汰图的 files 项，
#   回放器按缺图跳过即可：参数/结果/box 都还在台账里，图没了仍有分析价值。
#
# 模块化：识别器内仅两处 hook(命中/未命中出口各一)；关掉 = RDD_SAMPLE=off，
#   拿掉 = 删本文件 + 那两处 hook。采样任何异常只 print，绝不影响识别主流程。
# ================================================================

import hashlib
import json
import os
import re
import time

import numpy as np
from PIL import Image


# 台账文件名。第一个是现行写入名；其余是历史名，只读端(回放器)须一并识别——
# 存量语料与用户手上的旧包都还是旧名，改名不能让它们作废。
MANIFEST_NAME = "samples.jsonl.log"
MANIFEST_NAMES = (MANIFEST_NAME, "samples.jsonl")

# 目录内 PNG 数量上限（RDD_SAMPLE_MAX 可调）。超限按 mtime 淘汰最旧的，**不是停采**：
# 停采等于「攒够一批就再也拿不到新数据」，而出问题时要的恰恰是最近这几张。
# 定这个数看的是文件数不是体积——实测 26 天积累 955 张仅 3.4 MB（中位数 344 B），
# 占盘完全不是问题；真正会硌人的是一个目录里堆上万个小文件：导出日志打包、清理、
# 甚至资源管理器打开都会明显变慢。5000 张按实测速率约合 4 个月。
_MAX_FILES = 5000
# 一次淘汰到上限的这个水位，而不是刚好卡在上限：否则每次 record 都要全目录扫描+排序。
# 腾出的余量 = 上限×10%，除以每次采样的图数(≤3)即两次淘汰的间隔：默认 5000 → 余量 500
# → 约 167 次采样才扫一次目录。把 RDD_SAMPLE_MAX 调到几十这种极小值时，余量会小于单次
# 图数，退化成每次 record 都扫一遍——目录本身也就那么大，可以接受，知道是这么回事即可。
_EVICT_WATERMARK = 0.9


class RddSampler:
    _MODES = ("off", "fail", "all")

    def __init__(self, default_dir_fn, option_fn=None):
        """
        default_dir_fn: () -> str，默认落盘目录（由使用方注入，避免反向依赖）。
        option_fn:      () -> dict，maa_option.json 内容（读不到给 {}）。
        """
        self._default_dir_fn = default_dir_fn
        self._option_fn = option_fn or (lambda: {})
        self._mode = None            # 首次使用时定型，进程生命周期内不变
        self._dir = None
        try:
            self._interval = float(os.environ.get("RDD_SAMPLE_INTERVAL", "1800"))
        except (TypeError, ValueError):
            self._interval = 1800.0  # 环境变量非法时兜底，避免 import 阶段崩溃
        try:
            self._max_files = int(os.environ.get("RDD_SAMPLE_MAX", str(_MAX_FILES)))
        except (TypeError, ValueError):
            self._max_files = _MAX_FILES
        self._png_count = None       # 目录内 PNG 数，首次落盘时实扫一次，之后增量维护
        self._last_ts = {}           # key -> 上次落盘时间
        self._last_hash = {}         # key -> 上次 roi_crop 内容哈希

    # ------------------------------------------------------------------
    # 对外唯一入口
    # ------------------------------------------------------------------

    def record(self, *, node, roi, result, stage=None, images=None, meta=None):
        """
        采一条样本。node=检测点名；roi=(x,y,w,h) 全局；result="hit"/"miss"；
        stage=miss 卡点；images={tag: BGR ndarray 或 bool mask}；meta=其余 JSONL 字段。
        """
        try:
            mode = self._resolve_mode()
            if mode == "off" or (mode == "fail" and result != "miss"):
                return
            key = self._key(node, roi, result)
            now = time.time()
            if now - self._last_ts.get(key, 0.0) < self._interval:
                return
            digest = self._digest((images or {}).get("roi_crop"))
            if digest and digest == self._last_hash.get(key):
                return   # 间隔过后画面仍没变→不采也不刷新计时，画面一变下一次即采

            out_dir = self._resolve_dir()
            os.makedirs(out_dir, exist_ok=True)
            ts_tag = (time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
                      + f"{int(now * 1000) % 1000:03d}")
            files = []
            for tag, img in (images or {}).items():
                name = f"{ts_tag}_{key}_{tag}.png"
                if self._save_img(os.path.join(out_dir, name), img):
                    files.append(name)

            line = {"ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                    "node": node, "result": result, "stage": stage,
                    "roi": [int(v) for v in roi]}
            line.update(meta or {})
            line["files"] = files
            with open(os.path.join(out_dir, MANIFEST_NAME), "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False, default=self._jsonable) + "\n")

            self._last_ts[key] = now
            if digest:
                self._last_hash[key] = digest
            self._evict(out_dir, added=len(files))
        except Exception as e:
            print(f"[RddSampler] 采样失败(不影响识别): {e}")

    # ------------------------------------------------------------------
    # 容量闸：FIFO 淘汰
    # ------------------------------------------------------------------

    def _evict(self, out_dir, added: int) -> None:
        """PNG 数超上限时按 mtime 删最旧的一批，留出水位。台账不动。

        为什么淘汰而不是停采：停采会让目录冻结在几个月前的样子，而排查现场问题要的
        永远是最近那几张——真出事时打开一看全是陈年旧图，采集就白做了。

        为什么台账只增不删：它是 JSONL 纯文本（实测 373 条不过几十 KB），且是回放器
        唯一的元数据来源——roi/params/result/box 全在里面，图没了台账还能看参数分布。
        代价是台账里会留下指向已淘汰图的 files 项，回放器按缺图跳过即可。

        计数是增量维护的：只有跨过上限那一次才真去扫目录，平时不做任何 IO。
        """
        try:
            if self._png_count is None:                  # 首次落盘，实扫一次建立基线
                self._png_count = self._count_png(out_dir)
            else:
                self._png_count += added
            if self._png_count <= self._max_files:
                return

            entries = []
            for e in os.scandir(out_dir):
                if e.name.endswith(".png"):
                    try:
                        entries.append((e.stat().st_mtime, e.path))
                    except OSError:
                        continue
            self._png_count = len(entries)               # 以实扫结果校正增量误差
            keep = max(1, int(self._max_files * _EVICT_WATERMARK))
            if self._png_count <= self._max_files:
                return

            entries.sort()                               # 旧 → 新
            removed = 0
            for _, path in entries[:self._png_count - keep]:
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    continue
            self._png_count -= removed
            print(f"[RddSampler] 样本达上限({self._max_files})，已淘汰最旧 {removed} 张，"
                  f"现存 {self._png_count}：{out_dir}")
        except Exception as e:
            print(f"[RddSampler] 淘汰失败(不影响识别与采样): {e}")
            self._png_count = None                       # 计数不可信了，下次重新实扫

    @staticmethod
    def _count_png(out_dir) -> int:
        return sum(1 for e in os.scandir(out_dir) if e.name.endswith(".png"))

    # ------------------------------------------------------------------
    # 配置解析
    # ------------------------------------------------------------------

    def _opt(self) -> dict:
        try:
            return self._option_fn() or {}
        except Exception:
            return {}

    def _resolve_mode(self) -> str:
        if self._mode is None:
            env = os.environ.get("RDD_SAMPLE")
            val = env if env is not None else str(self._opt().get("rdd_sample", "all"))
            val = val.strip().lower()
            self._mode = val if val in self._MODES else "all"
        return self._mode

    def _resolve_dir(self) -> str:
        if self._dir is None:
            d = (os.environ.get("RDD_SAMPLE_DIR")
                 or self._opt().get("rdd_sample_dir")
                 or self._default_dir_fn())
            self._dir = os.path.abspath(d)
        return self._dir

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    @staticmethod
    def _key(node, roi, result) -> str:
        raw = f"{node or 'node'}_{'-'.join(str(int(v)) for v in roi)}_{result}"
        return re.sub(r'[^A-Za-z0-9_.\-]', '_', raw)

    @staticmethod
    def _digest(img):
        if img is None or not isinstance(img, np.ndarray):
            return None
        # 只用于「画面是否变化」的去重比对，非安全用途（消 Ruff S324 / CWE-327 告警）
        return hashlib.md5(img.tobytes(), usedforsecurity=False).hexdigest()

    @staticmethod
    def _save_img(path, img) -> bool:
        try:
            if img is None or not isinstance(img, np.ndarray) or img.size == 0:
                return False
            if img.dtype == bool:   # bool mask → 白底黑形状
                rgb = np.full((*img.shape, 3), 255, dtype=np.uint8)
                rgb[img] = [0, 0, 0]
            else:                   # BGR → RGB
                rgb = img[..., ::-1]
            Image.fromarray(rgb).save(path)
            return True
        except Exception as e:
            print(f"[RddSampler] 图片保存失败({os.path.basename(path)}): {e}")
            return False

    @staticmethod
    def _jsonable(o):
        # np.bool_ 不是 np.integer 的子类，漏了会掉到最后的 str(o)，在 JSONL 里写成
        # "True" 字符串而不是布尔值，回放器还得单独兼容一次
        if isinstance(o, np.bool_):
            return bool(o)
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return float(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
        return str(o)
