<!--
════════════════════════════════════════════════════════════════
公告草稿 · 注入目标：应用内公告 (1.公告.md)
════════════════════════════════════════════════════════════════

【格式规范】
每段内容块前须标注目标版本标记，CI 脚本按顺序拼接所有匹配
当前版本类型的块，整体注入到公告文件的 Msg-Anch 锚点。

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
  这段所有版本都会看到
  ---end---

  ---target: beta---
  这段只有 beta 版能看到
  ---end---

  → stable 收到：第 1 段
  → beta   收到：第 1 段 + 第 2 段
  → ci     收到：第 1 段

【使用方法】
1. 在本文件末尾按格式写好内容块
2. 将改动合并入主分支，下次对应版本 CI 自动注入
3. 发版后请清空内容块（保留本段注释），防止旧通知重复注入

⚠️  若版本类型标识（stable/beta/alpha/ci）发生变更，
    需同步修改 scripts/inject_announcement.py 中的 _get_tag_type()
════════════════════════════════════════════════════════════════
-->
---target: all---
> 近期发版说明:

> 正式版:UI改变因素必定不能跑时,不如直接下放公测版。 | 公测版:司职不变。 | 内测版:PC端适配过程中。
---end---

---target: beta---
> Mac系统现可使用专有控制器 playcover 与其配套资源。相关配置方法见 [PlayCover 适配指南](https://github.com/sunyink/MFABD2/blob/develop/docs/zh_cn/PlayCover%E9%80%82%E9%85%8D%E6%8C%87%E5%8D%97.md)。由[@KoujiMinamoto](https://github.com/KoujiMinamoto)强力支援。
---end---
