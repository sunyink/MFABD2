# MFABD2

基于 MaaFramework 的《BrownDust2》自动化工具。Pipeline JSON 描述流程，
Python Agent 提供自定义识别/动作，MFAAvalonia 提供 GUI。

## 仓库布局

| 路径 | 内容 |
| --- | --- |
| `assets/interface.json` | Pipeline Interface (PI)，UI 选项与 `pipeline_override` |
| `assets/resource/base/` | 主资源包：pipeline JSON、模板图、OCR 模型 |
| `assets/resource/pc/` | PC 端差异化覆盖包 |
| `assets/resource/Announcement/` | 软件内公告 Markdown |
| `assets/MaaCommonAssets/` | **submodule**，不要直接改 |
| `agent/` | Python Agent：`action/` `recognition/` `utils/` |
| `scripts/` | 版本分析、changelog 生成、公告注入 |
| `tools/` | 维护脚本：`migrate_pipeline_manager.py`（迁移）、`lint_pipeline.py`（静态体检）、`bounded_safety_net.py`（无界循环普查与有界化） |

## 运行

`agent/main.py` **不能直接运行**——它需要 MaaFramework 通过命令行末位参数传入
`socket_id`，缺失时会直接退出。要跑就从 GUI 启动。

运行模式由 `requirements.txt` 是否存在自动判定：

- **dev**（有 `requirements.txt`）：`main.py` 会自动接管虚拟环境（`utils/venv_ops.py`），
  且**不注入** DLL 路径，用 pip 安装的 MaaFw 自带 DLL。
- **release**（无）：强制把 `runtimes/<rid>/native` 注入 `MAAFW_BINARY_PATH`。

改 `venv_ops.py` 里的 MaaFw 版本时，要与 `agent/` 代码所依赖的版本手动对齐——
两者不一致会出现「代码是新的、DLL 是旧的」。

当前版本锚点（写文档/排查时以实际文件为准，别照抄这里的数字）：

| 锚点 | 位置 | 现值 |
| --- | --- | --- |
| MaaFw（内核 + pip） | `agent/utils/venv_ops.py` 的 `DEV_MAAFW_VERSION` | `5.11.1` |
| MaaFw 兼容区间 | 同上 `FALLBACK_MAAFW_SPEC` | `>=5.11,<6.1` |
| MFAAvalonia | `requirements.txt` 的 `MFAA_TAG` | `v2.14.0-beta.2` |
| Python | 同上 `PREFERRED_PYTHON_VERSION` | `3.10` |

运行时日志在 `debug/maafw.log`，开头三行会打印实际内核版本——**排查前先对一眼**，
`debug/maa.log` 是旧命名的历史档（停在 v5.9.2），别拿它推当前行为。

## 格式化（改 JSON/YAML 前必读）

`.prettierrc.mjs` 装了两个插件，**手写缩进和字段顺序几乎必然不合规**：

```bash
pnpm prettier --write <file>
```

生效的关键选项：

- `multilineArraysWrapThreshold: 1` → **数组每个元素独占一行**
- `@nekosu/prettier-plugin-maafw-sort` → **重排 key 顺序**。默认
  `maafwPipelinePatterns` = `/pipeline/.*\.jsonc?`，即 `base/pipeline/`
  与 `pc/pipeline/` 下所有节点 JSON 都按协议字段序重排；`maafwInterfacePatterns`
  另用一套规则处理 `interface.json`。（`base/default_pipeline.json` 不在
  `pipeline/` 目录内，**不受排序影响**。）
- 自定义 `stripBlankLines` → 删除对象/数组内部空行
- JSON 4 空格、`printWidth: 120`、`bracketSpacing: false`；YAML 2 空格

**`.pre-commit-config.yaml` 里的 hook 在任何地方都不会自动运行。** pre-commit 框架未安装
（`scripts/hooks/` 里没有 `pre-commit` 钩子）；配置里的 `ci:` 段指向 pre-commit.ci，
但全历史没有一条 `Auto update by pre-commit` 提交，该 App 并未启用。

所以 prettier、oxipng（PNG 压缩）、markdownlint 全靠手动执行。
**没有人会替你修格式**——不跑就是不合规的文件直接进仓库。

## 提交

标题前缀由 `scripts/changelog_generator.py` 解析成 changelog 分类，**写错会污染发布日志**。
冒号半角全角都认（`[：:]`）：

- **Tier 1**（功能类）：`feat` / `fix` / `impr`（功能增强，项目自定义）/ `perf` / `refactor` / `revert`
- **Tier 2**（维护类）：`docs` / `style` / `test` / `chore` / `ci` / `build`

解析不出前缀的提交落入 `other` 组。注意 **`security` 不在任何分层里**，用了会掉进 other。

**scope 会提升分层**：`chore(fix): xxx` 这种「Tier 2 类型 + Tier 1 scope」会被归进 **Tier 1 的
`fix` 组**，而不是 chore（`changelog_generator.py:77-79`）。想让维护类改动出现在功能日志里可以
用这招，但别误用——scope 只认 `\w+`。

正文标记（`detect_commit_highlights`）：

- `BREAKING CHANGE:` / `BREAKING-CHANGE:` → 标注 ⚠️ 破坏性变更。
  **优先用这个而不是 `feat!:`**——检测 `!:` 的正则带 `^` 锚点且不跨行，body 多行时会漏检。
- `HIGHLIGHT:` → 标为 💡 亮点功能
- `Co-authored-by: 名字` → 列为协作者

`.github/workflows/install.yml` 检测发版标记。**前两个只取提交信息的最后一行**
（`tail -n1`），位置放错就不触发；第三个是全文匹配，写在哪儿都算：

- `[deploy-beta]` → beta 发布（`tail -n1`；历史用过 246 次，是常规发版手段）
- `[deploy-alpha]` → alpha 发布（`tail -n1`）
- `[deploy-sync]` → 交由 `dispatcher.yml` 分发（**全文 `contains`**，见 `install.yml` L53）

> 这三个标记会真的触发构建与发布。**除用户明确要求，不要写入提交信息。**

## Git 钩子

`core.hookspath` 已指向 **`scripts/hooks/`**（两个钩子都已入库，对所有 clone 生效）。
根目录 `.git/hooks/prepare-commit-msg` 是另一份旧副本，因 hookspath 重定向而**永不执行**——
别改那份。

- `prepare-commit-msg`：**不带 `-m`** 时把消息覆写成交互模板；`git commit -m` 会跳过
  （`$2` 为 `message`/`commit` 时直接 `exit 0`）。合并提交不拦截。
- `commit-msg`：**任何提交方式都会跑**（`-m` / `-F` / 编辑器都拦）。它对模板占位句做
  **纯字符串 `grep`**，命中就拒绝提交。用 `-m` 写真实描述不受影响。
  ⚠️ 副作用：**连"引用"这句话都会被拦**——写一条解释该钩子的提交信息时，
  别把占位句原样抄进去，中间断开或用省略号。

## 分支

Fork → 开分支 → PR；分支名不需纠结，PR 里说明改了什么即可（见 `CONTRIBUTING.md`）。
以下改动**先开 Issue 对方向**再动手：

- 大改架构、核心流程调度、引入新依赖
- 跨多个资源包（`base` / `pc` / NT 等）的改动
- 破坏现有用户配置兼容性

不做无功能意义的全量 reformat。

## 硬约束

- **坐标基准 1280×720**（宽×高，横屏；即"720p"指短边）。所有 ROI、点击点、模板图都以此为准。
  实测 850 个 `roi`/`target`/`begin`/`end`：`x+w` 的 p99 = 1279、`y+h` 的 p95 = 715。
- **Pipeline 节点协议用 V1 扁平格式**：`recognition` / `action` 是字符串，参数平铺在节点顶层。
  不要写 V2 的 `{type, param}` 嵌套。（`interface.json` 是独立概念，用 PI V2。）
- 节点命名用 **`模块_动作` 下划线分段、段内 PascalCase**，例如
  `Arbitrage_Bargaining_Msgbox_Ck`、`Activities_FreeClothing_Ty2`。现存 1551 个节点中
  99.4% 带模块前缀，新增节点沿用所在文件的模块名；测试节点用小写 `test_*`。
- 协议默认 `rate_limit=1000ms`、`pre_delay`/`post_delay=200ms`——省略字段会引入隐式等待，
  不需要延迟时要显式写 `0`。
- **循环节点必须有界**。`timeout` 只对「next 列表整轮无命中」计时——识别一直命中就永不触发；
  而 `max_hit` 默认 UINT_MAX。于是「命中即点、点了画面不变」的节点是永动机（实测有单日
  914 次点同一坐标、连烧 90 分钟的案例）。所以**自环节点、以及被 `[JumpBack]` 指向的节点，
  一律要显式写 `max_hit`**。取值按「这个节点在最长的一次正常运行里合法命中几次」预算，
  不是按「卡住时想让它几次停」——触界只是让该节点退场由 `on_error` 接手，给大只是止损慢，
  给小会打断本来能走通的流程。跨节点定值口径：**被依赖者的额度不低于依赖它的节点**
  （清理器 ≥ 触发器，否则触发还在点、清理先退场会留下清不掉的弹窗；识别源 ≥ 消费者）。
  用 `python tools/lint_pipeline.py --only bounds` 普查，`tools/bounded_safety_net.py` 批量补。
