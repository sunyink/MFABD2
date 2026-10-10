# -*- coding: utf-8 -*-
"""数字清单解析（BatchNumericPatch / ForceListApply / 强采放行 共用）。

语法：逗号/中文逗号/分号/空白/竖线分隔，支持区间连接符 ~ ～ -
     "1,3~5" -> [1,3,4,5]      "7;2" -> [2,7]       "1-3" -> [1,2,3]
非法 token（非数字）原样保留在 parse_tokens 里，供 BatchNumericPatch 拼节点名；
parse_number_list 只收数字，非法一律丢弃。

放在 utils 而不是 action/recognition 里：这两边都要用，而 action 与 recognition
互相有导入（pipeline_manager 引 recognition.counter），共用模块放中立位置可避免循环导入。
"""
import re

SPLIT_PATTERN = r'[，,;\s|]+'
RANGE_CONNECTORS = ['~', '～', '-']


def parse_tokens(text):
    """字符串 -> token 列表（去空，保持原顺序）。"""
    return [x for x in re.split(SPLIT_PATTERN, str(text if text is not None else '')) if x]


def parse_number_list(text):
    """把 "1,3~5" 这类输入解析成去重且升序的 int 列表；空/非法 -> []。"""
    nums = set()
    for tok in parse_tokens(text):
        conn = next((c for c in RANGE_CONNECTORS if c in tok), None)
        if conn is not None:
            parts = tok.split(conn)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                try:
                    a, b = int(parts[0].strip()), int(parts[1].strip())
                except ValueError:
                    continue
                a, b = min(a, b), max(a, b)
                nums.update(range(a, b + 1))
            continue
        try:
            nums.add(int(tok))
        except ValueError:
            continue
    return sorted(nums)
