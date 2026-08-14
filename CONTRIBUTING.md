<!-- markdownlint-disable MD033 MD041 -->

# 参与 MFABD2 开发

先说句实在的：这个项目长期是我一人在填战壕，我**很欢迎友军**。所以这份文档不写"你必须按我的规矩来"，而是先告诉你——**你做的东西我会不会要、我会怎么对你、你提交之后大概会经历什么**。git 那套仪式我自己消化，不拿来挡你。

> **一句话流程**：Fork → 开个分支随便改 → 提 PR。
> **提到哪个分支不用纠结**，在 PR 里说一句你做了什么，我帮你归位。拿不准就先开个 [Issue](https://github.com/sunyink/MFABD2/issues/new/choose) 聊。

---

## 一、这个项目要什么、不要什么

新人最该先知道这块，免得白做。

**欢迎（大概率会合并）：**

- 游戏更新后**坏掉的识别/流程**修复——新卡带、改版界面、错位的模板。这是项目的命脉，永远缺人。
- 新功能的 **Pipeline 节点**或 **Agent**（自定义识别/动作）。
- **资源/截图**补充：新地图、新活动的素材。
- 文档、错别字、说明不清的地方——再小都欢迎。
- 复现清楚的 **Bug 反馈**（带日志，见下）。

**请先聊一句再动手（方向性的东西）：**

- 大改架构、改动核心流程调度、引入新依赖。
- 跨多个资源包（base / NT 等）的改动。

这类不是不要，而是**做之前开个 Issue 对一下方向**，免得你写完了我们方向不一致，白费力气。

**基本不接受：**

- 把使用求助、伸手要号、要教程当 PR/Issue 提的——那些走[使用帮助](https://github.com/sunyink/MFABD2/issues/new/choose)或软件内文档。
- 为了改而改的格式化大扫除（无功能意义的全量 reformat）。
- 破坏现有用户配置兼容性、又没说明迁移方式的改动。

**没列到的呢？**

比如你想给识别搞个模型炼丹、或别的我没预想到的方向——**清单是举例，不是围栏**。只要你把逻辑想清楚了、能踏实落地，就来。真怕做偏，先开个 Issue 聊两句；但别因为"它不在清单里"就把自己劝退。

---

## 二、你提交之后会经历什么（预期管理）

- 我会**亲自 Review 每个 PR**。这是个人维护的项目，所以——**别期待几小时内回复**。通常几天内会看，赶上忙的时候更久，但不会沉。超过一周没动静，直接在 PR 里 @ 我催，不算打扰。
- Review 时我可能会请你改几处，或者**我自己接手补完再合**——后者很常见，别觉得是你没做好，只是有些项目内部约定你不必去学。
- 合并后你的 Commit 会**原样保留**，GitHub 会自动把你列进 [Contributors](https://github.com/sunyink/MFABD2/graphs/contributors)。该是你的署名跑不掉。
- 如果最后没合并，我会说清楚为什么。不会让你的 PR 无声无息地烂在那里。

---

## 三、怎么挑活干

不知道从哪下手，按这个顺序找：

1. **[Issues](https://github.com/sunyink/MFABD2/issues)** 里带 `good first issue` / `help wanted` 标签的。
2. **[`维护点列表.md`](维护点列表.md)**——列了游戏更新时容易坏、需要盯的点，适合想长期接手某块的人。
3. 自己用着用着发现的坑——你踩到的，通常别人也踩到了。

挑中了，**在对应 Issue 下说一声"我来"**，免得撞车。

---

## 四、搭开发环境

本项目基于 [MaaFramework](https://github.com/MaaXYZ/MaaFramework)，你要碰的就两层：

- **Pipeline（JSON）**：`assets/resource/**/pipeline/*.json`——识别与流程，改这层不用写代码。
- **Agent（Python）**：`agent/`——自定义识别 / 动作，照 `agent/` 里现成的照葫芦画瓢。

**什么算"最小上手"？** 就是让你的改动在真实游戏里跑起来所需的最少东西，只有两样：①一台配好的模拟器+游戏（按 [README](README.md) 弄，人手一份）；②一个能连模拟器、把你改的节点**直接跑起来**的调试器。剩下的都是"让你更舒服"，不是"跑起来必需"。

**上手工具按角色分，有的一专、有的多能：**

| 角色 | 干什么 | 趁手的 |
|---|---|---|
| **编辑器** | 写 / 改 Pipeline、Agent | VSCode（手写 JSON）· [MPE](https://github.com/kqcoxn/MaaPipelineEditor)（拖节点） |
| **查看器** | 读懂现有节点怎么串的 | [MPE](https://github.com/kqcoxn/MaaPipelineEditor) 当"浏览器"翻 · VSCode |
| **调试器** | 连模拟器、跑 / 单点调试你的改动 | **[maa-support-extension](https://github.com/neko-para/maa-support-extension)** |

**划重点**：[maa-support-extension](https://github.com/neko-para/maa-support-extension) 一个 VSCode 插件把编辑、查看、调试全包了，能直接对着本仓库连模拟器跑任务、单点调试，反馈就在眼前。**所以真正的最小上手 = 模拟器 + 这个插件。** 这也是我手写 Pipeline 的主力路子。

**非 Windows 开发者注意**：仓库里 `assets/interface.json` 的 `agent.child_exec` 写的是 `../.venv/Scripts/python.exe`——Windows 的 venv 布局。macOS / Linux 上虚拟环境的解释器在 `bin/` 下，这个路径不存在，**Agent 会静默起不来**（进程都没创建成功，日志里什么都看不到）。本地改两处，注意**别提交**：

1. `assets/interface.json` → `"child_exec": "../.venv/bin/python3"`
2. `.vscode/mse_config.json` 里那张映射表的 **key** 同步改成一样的字符串

第 2 步不能省：maa-support-extension 是拿 `child_exec` 的**原始字面值**（不是解析后的绝对路径）去查这张表的，对不上就静默退回非调试路径——不报错，只是你的断点不生效。

只有本地要这么改。**发布包不受影响**：`install.py` 打包时按目标平台无条件覆写这两个字段（Windows 用内嵌 Python、macOS 用便携版、Linux/Android 用系统 `python3`）。

**别拿 MFAAvalonia 当测试环境。** MFAA 是发给终端用户的**构建物**（打包好的 GUI），人手一份；它不是开发调试用的。想用它验改动，得开调试模式、或手动替换包里的资源文件，很折腾。开发阶段用上面的调试器，别从 MFAA 起步。

**提交前格式化。** Pipeline JSON 有既定字段顺序，靠 prettier 保证，两种方式随你：VSCode 装 prettier 插件、保存自动格式化；或命令行 `pnpm install` 一次、之后 `pnpm prettier --write`。（配置见 `package.json`。）

**你不必跟我一样。** [MaaFramework 主页](https://github.com/MaaXYZ/MaaFramework)还挂了一堆工具（如 [MFAToolsPlus](https://github.com/SweetSmellFox/MFAToolsPlus)），挑顺手的用；用 AI（配 MCP）开发也完全可行。个人建议**两边都摸一摸**——只会手写会累，只会 AI 会飘，**都会才不受限**。

反馈 Bug / 定位问题时记得带日志（软件 UI 日志墙上方有一键导出）。更细的协议看 [MaaFramework 文档](https://maafw.com/docs/3.1-PipelineProtocol)；Mac 部署参考 [M9A 文档站](https://1999.fan/zh_cn/manual/newbie.html)。

---

## 五、代码与风格

不严苛，只要别给后人添乱：

- **先看懂，再动手**：改现有的东西前，先搞懂它**现在为什么这么写**。很多看着"多余"的节点或字段，是踩过坑留下的柱子——看到一根不明所以的柱子，先弄清它撑着什么再决定拆不拆。搞不懂就来问，我乐意讲来龙去脉。**问一句，好过重复造轮子、或拆掉承重墙。**
- **Pipeline JSON**：提交前跑过 prettier（上面那步），保持字段顺序一致。
- **Python**：跟着 `agent/` 现有风格走，日志用项目里的 `mfaalog`，别裸 `print`。
- **模块文档**：钓鱼模块有一份 [开发说明](docs/zh_cn/钓鱼模块开发说明.md)，写清了接口、参数在哪调、哪些是死代码。动这块之前先读它。
- **Commit**：本仓库带了提交辅助，执行一次 `git config core.hooksPath scripts/hooks`，之后 `git commit` 会弹出辅助界面帮你规范格式。没用也行，把改动说清楚即可。
- **资源包**：多包（base / NT 等）用 [MaaOWM](https://github.com/sunyink/MaaOWM) 管理，依序挂载、卸载时向目标包写回 Diff。

---

## 六、行为规范

就一条：**对人客气，对事直接。** 这里不分大佬萌新，愿意一起把东西做好就是同路人。

---

## 七、想找人聊 / 进开发群

<img alt="进开发群二维码" src="https://i.ibb.co/ZzXqh2sM/215216.jpg" width="240"/>

> 扫码进群。进群前请明白这是个**什么群**：

- ✅ **是**：开发协作、对方向、抓虫定位、一起填战壕的地方。
- ❌ **不是**：使用求助、伸手要号、要教程的地方——那些走软件内「使用帮助」或 [GitHub Issue](https://github.com/sunyink/MFABD2/issues/new/choose)，那边我回得更全。

为了让群保持开发氛围，**入群时会问一句**：你想参与哪一块（识别 / Pipeline / Agent / 资源 / 测试…），手上现在有什么。随便答一句真实的就行，这不是考试，只是想确认你是来一起做事的。

---

<details>
<summary>📐 好奇项目内部是怎么演进的？（贡献者<b>非</b>必读）</summary>

下面这套是我维护主链时自己跑的流程，**复杂度由我吸收，你提 PR 完全不用管这些**。放在这只是给好奇的人看。

- `main`（正式/稳定流）、`develop`（公测/活跃流）、`alpha`（临时内测）是三条发版通道，**彼此不互相合并**，各自只对应一个版本通道的发布。
- 真正的开发都在 `feat/*` 功能隔离分支上瀑布式推进：先合入测试通道验证，质量确认后再由该 feat 分支 PR 进 `main` 留下完整记录，**合并后 feat 分支即消亡**。
- 长线特性会有自己的长期分支（如 PC 支持走 `feat/NT-Support`）。
- 合并一律 `--no-ff`，禁 rebase、禁 squash——保留每条功能演进的脉络。
- 多资源包（base / NT）间存在反向合并、单独发版通道。

所以你 PR 提到哪个分支真的不重要，我会按它实际属于哪条链去归位。**你只管把东西做好。**

</details>
