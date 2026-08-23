<!--
════════════════════════════════════════════════════════════════
Release 头部草稿 · 注入目标：GitHub Release Notes 顶部
════════════════════════════════════════════════════════════════

【格式规范】
每段内容块前须标注目标版本标记，CI 脚本按顺序拼接所有匹配
当前版本类型的块，插入到 Release Notes 正文最顶部。

标记格式：
  ---target: <版本类型> [, <版本类型>]---
  内容…
  ---end---

版本类型：
  all    · 所有版本（stable / beta / alpha / ci）
  stable · 正式版
  beta   · 公测版
  alpha  · 内测版
  ci     · 开发版

【叠加示例】
  ---target: all---
  这段所有版本的 Release Notes 都会显示
  ---end---

  ---target: stable---
  这段只出现在正式版 Release Notes 里
  ---end---

  → stable 收到：第 1 段 + 第 2 段
  → beta   收到：第 1 段
  → ci     收到：第 1 段

【使用方法】
1. 在本文件末尾（注释之后）按格式写好内容块
2. 将改动合并入主分支，下次对应版本 CI 自动读取并插入 Release Notes 头部
3. 发版后请清空内容块（保留本段注释），防止旧内容重复出现

⚠️  若版本类型标识（stable/beta/alpha/ci）发生变更，
    需同步修改 scripts/changelog_generator.py 中的 _get_tag_type()
════════════════════════════════════════════════════════════════
-->
---target: all---
> 159 Moons of Grace , And miles to go with you.

>原资源过旧，直接下发公测。激进好过死定。
---end---
