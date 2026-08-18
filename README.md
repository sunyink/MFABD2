<!-- markdownlint-disable MD033 MD041 -->
<div align="center">
  <img alt="LOGO" src="https://github.com/sunyink/MFABD2/blob/main/ReadMe/logo.png" width="180" height="180" />


# MaaBD2 - 棕色尘埃2自动化助手

[![Stable Version](https://img.shields.io/github/v/release/sunyink/MFABD2?label=正式版&color=green&logo=github)](https://github.com/sunyink/MFABD2/releases/latest)
[![Beta Version](https://img.shields.io/github/v/tag/sunyink/MFABD2?include_prereleases&filter=*beta*&label=公测版&color=blue&logo=github)](https://github.com/sunyink/MFABD2/releases?q=beta&expanded=true)
<a href="https://mirrorchyan.com/zh/projects?rid=MFABD2" target="_blank"><img alt="mirrorc" src="https://img.shields.io/badge/Mirror%E9%85%B1-%239af3f6?logo=countingworkspro&logoColor=4f46e5"></a>

[![Build Status](https://img.shields.io/github/actions/workflow/status/sunyink/MFABD2/install.yml?label=构建状态&logo=githubactions)](https://github.com/sunyink/MFABD2/actions/workflows/install.yml)
[![License](https://img.shields.io/badge/License-MIT%2FApache--2.0-blue)](./LICENSE)
[![Stars](https://img.shields.io/github/stars/sunyink/MFABD2?label=给项目点赞&color=f39c12&logo=github)](https://github.com/sunyink/MFABD2)

[![Main 活跃度](https://img.shields.io/github/commit-activity/m/sunyink/MFABD2?label=稳定版活跃度&color=brightgreen)](https://github.com/sunyink/MFABD2/commits/main)
[![Dev 活跃度](https://img.shields.io/github/commit-activity/m/sunyink/MFABD2/develop?label=公测版活跃度&color=blue)](https://github.com/sunyink/MFABD2/tree/develop)
[![.NET Version](https://img.shields.io/badge/.NET-≥%2010-512BD4?logo=csharp)](https://dotnet.microsoft.com/download/dotnet/10.0)



</div>

基于 MaaFramework 构建的《棕色尘埃2》全流程自动化助手。解放“日常15分钟”的双手，无感替您代跑每日近50分钟的**全量**日常、周常及活动任务。拒绝枯燥重复，奖励“我全都要”，精力只留给享受游戏。

---

## 获取 & 安装

<div align="center">

### 🛠️ 版本通道

<table width="100%">
  <thead>
    <tr>
      <th width="20%" align="center">版本通道</th>
      <th width="50%" align="center">迭代逻辑与实况</th>
      <th width="30%" align="center">获取方式</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td align="center">
        <b>正式版</b><br>
        <small>(纯数字版本号)</small>
      </td>
      <td>
        <b>求稳派的选择。</b><br>
        通常在累积 100+ 次 Commits 与多轮补丁验证后才“姗姗来迟”。适合追求长期稳定、告别折腾的玩家。
      </td>
      <td align="center">
        <a href="https://github.com/sunyink/MFABD2/releases/latest">
          <img src="https://img.shields.io/github/v/release/sunyink/MFABD2?label=%E6%AD%A3%E5%BC%8F%E7%89%88&color=green&logo=github" alt="正式版">
        </a>
      </td>
    </tr>
    <tr>
      <td align="center">
        <b>公测版</b><br>
        <small>版本号含 Beta</small>
      </td>
      <td>
        <b>不断探索边界。</b><br>
        针对现场实况与素材<b>高频迭代</b>，更新粒度以日甚至小时计。强烈欢迎各位“课代表”在此抓虫并提交 Issue。
      </td>
      <td align="center">
        <a href="https://github.com/sunyink/MFABD2/releases?q=beta">
          <img src="https://img.shields.io/github/v/tag/sunyink/MFABD2?include_prereleases&filter=%2Abeta%2A&label=%E5%85%AC%E6%B5%8B%E7%89%88&color=blue&logo=github" alt="公测版">
        </a>
      </td>
    </tr>
    <tr>
      <td align="center">
        <b>内测版</b><br>
        <small>版本号含 Alpha</small>
      </td>
      <td>
        <b>新端适配的前沿。</b><br>
        目前 <b>PC 客户端（Windows 独立端）适配</b>在此通道推进——<b>开放加入、人人可试</b>。功能仍在完善，遇到问题欢迎提 Issue，作者会跟进修复。
      </td>
      <td align="center">
        <a href="https://github.com/sunyink/MFABD2/releases?q=alpha">
          <img src="https://img.shields.io/github/v/tag/sunyink/MFABD2?include_prereleases&filter=%2Aalpha%2A&label=%E5%86%85%E6%B5%8B%E7%89%88&color=orange&logo=github" alt="内测版">
        </a>
      </td>
    </tr>
  </tbody>
</table>

</div>

<details>
<summary>⚡ 五分钟快速上手（新用户看这里）</summary>
1. 下载对应平台解压 → 2. MuMu 设置 1920×1080 → 3. 游戏切简体中文 → 4. 启动 UI 运行
</details>
<br>

UI 内可直接获取更新推送、下载，由[![Mirror酱](https://img.shields.io/badge/Mirror%E9%85%B1-高速下载-%239af3f6?logo=countingworkspro&logoColor=4f46e5)](https://mirrorchyan.com/zh/projects?rid=MFABD2)提供支持。

> 通道版本差距约为几次 Bug/新功能/等验证 的轮回。

---

## 配置指引

>运行提示：Windows 用安卓模拟器；Apple Silicon Mac 可用 PlayCover 免模拟器运行（见下方「Mac 使用指引」）。请务必在良好的网络下运行，连通性波动导致的“重连转圈”易吞掉操作并引发失误。
>
>反馈指南：遇到卡点欢迎提交 [Issue](https://github.com/sunyink/MFABD2/issues/new/choose)。为了快速定位问题，请附带：出错前后几秒的录屏/截图 + 一键导出的打包日志（按钮在UI日志墙上方）![](ReadMe/UILog_Tool.png)。

**第一步 · 模拟器**

推荐使用 **MuMu 模拟器 12** 运行游戏。（需自行配置 Google 环境。省事直接用国际版，亦可参考笔记 [Google-Play-Store](https://github.com/sunyink/Google-Play-Store)）。

显示必须设置为 `平板版 1920×1080 (DPI ≈ 270 或 280)`。

<details>
<summary>多开 / ADB 讲解 （如不需要多开挂机，可安心跳过。）</summary>

多开，在UI([MFAA](https://github.com/MaaXYZ/MFAAvalonia))中，顶部新建选项卡可实现多实例。项目有存档功能，多开请注意开启并填入**多存档存档号**。

`adb.exe` 以 Windows 服务形式运行，默认服务端口 `5037`，各模拟器实例为其客户端。MuMu 多开器按编号分配 ADB 端口：`0` 号默认 `16384`，`1` 号为 `16416`，以此类推；国际版后启动的端口号在前者基础上 +1（如 `16385`）。

通常启动一个 ADB 服务即可控制多个实例，不建议多版本 ADB 并行运行。如有需要，可自定义 ADB 路径与端口加以区分。

</details>
<br>

**第二步 · 游戏内设置**

当前已支持自动配置，但需先将游戏语言切换为**简体中文**且分辨率正确，之后运行中可完成自动设定。

### 若自动配置失败，可参照下方手动调整：（均在默认基础上修改）：

| 分类 | 设置项 | 值 |
|------|--------|----|
| 操作 | 选择操作方式 | 点击地面 |
| 图像 | 分辨率 | FHD |
| 图像 | 游戏开始画面 | 主界面 |
| 通知 | 黄色圆点 | 不使用 |

> 画面质量异常时，请检查渲染方式。新机器通常 `Vulkan` 表现较好，旧机器推荐 `DirectX`。可关闭技能动画以提升运行速度。

**第三步 · 人物技能与装备**

按下图设置技能位置与装备分解选项（此设置保存在模拟器本地，不影响其他端）。配置一次即可，强化目标等级与金币消耗限额请按个人情况手动设定。

> **装备备注**：请保留至少一件 SR/UR +9 装备用于精炼。

> <img alt="技能设置" src="https://github.com/sunyink/MFABD2/blob/main/ReadMe/IT-2601.jpg" width="600px"/>


#### **关于任务复位**

箱庭地图状态识别、技能传送阵自动生成、绝大多数中断场景均已实现自动复位。

推荐在`剧情主线地图`或`传送阵上`结束游戏。

<details>
<summary>Mac 使用指引</summary>

**软件本体部署**：配置流程略复杂，可参考 [M9A 文档站](https://1999.fan/zh_cn/manual/newbie.html)（同框架，软件部署部分通用）。Mac 版 Agent 功能在根目录提供一键修复环境脚本，遇到环境问题时可使用。

**控制方式**：Apple Silicon Mac 推荐用 **PlayCover 直接控制 iOS 版，无需安卓模拟器**，详见 [PlayCover 适配指南](docs/zh_cn/PlayCover适配指南.md)。
> ⚠️ 需使用带 MaaTools 的 [hguandl fork 版 PlayCover](https://github.com/hguandl/PlayCover/releases)，**官方主线版没有 MaaTools、连不上**。
>
> PlayCover 支持由 [@KoujiMinamoto](https://github.com/KoujiMinamoto) 贡献与维护，相关问题欢迎在 issue 中 @他。

</details>

<details>
<summary>PC 客户端（Windows 独立端）使用指引 · 内测中</summary>

无需模拟器，直接控制 Windows 上的《棕色尘埃2》PC 客户端（Win32）。目前处于 **内测（Alpha 通道）**：功能仍在适配完善，**开放加入、人人可试**，遇到问题欢迎提 [Issue](https://github.com/sunyink/MFABD2/issues/new/choose)（运行平台请选「PC 客户端」），作者会跟进修复。

获取方式：下载 [内测版（Alpha）发布](https://github.com/sunyink/MFABD2/releases?q=alpha)，或在 Mirror 酱切换到 Alpha 通道。

> ⚠️ **切回提示**：内测版号在正式版基础上 **+2**（公测 +1），以保证各通道更新号单向递增。因此从内测**切回公测/正式版时，更新器会视为"降级"而不推送**——需手动重新下载目标通道，或等下一个正式版号追上后再自动衔接。

PC 客户端适配由 [@BebopSpikeSpiegel](https://github.com/BebopSpikeSpiegel) 主力推进，感谢贡献。

</details>

---

## 功能一览

帮你打理游戏里的每一天，从启动到收工，近 50 分钟日常无人值守。

🌅 **每日开局** &emsp;启动 & 更新游戏 · 自动前置配置

⚔️ **战斗刷取** &emsp;狩猎场 · 圣石洞穴 · 肉鸽塔 · PVP · 赛季活动（普通 / 挑战 / 魔兽 / 场域）

🎰 **白嫖抽卡** &emsp;每日免费 · 优惠档 · 多卡池保底保留

💰 **资源 & 奖励** &emsp;全图吸取 · 宠物派遣 · 餐馆结算 · 任务 / 活动 / 通行证 / 邮件一键清收

📅 **周常挂机** &emsp;末日之书 · 自动钓鱼 · 小屋点赞

> ⭐ **跑商套利** · 自动识别最高价，搓料理，批量进出货，可自定义商品列表
>
> 🎣 **接管钓鱼** · 检测收杆时机，全自动循环，挂机刷级


<details>
<summary><H3>📋 完整功能列表</H3></summary> 

#### 🌅启动游戏
- 启动 & 更新游戏 —— 更新游戏程序与数据版本
- 自动前置配置 —— 游戏内设置 / 技能放置 / PVP · 装备 的子菜单
- 多开存档号 —— 记录刷取进度，日常续刷、周常按需刷。

#### ⚔️ 战斗刷取
- 狩猎场 · 冒险航线 —— 选资源 / 选图 / 米饭分配
- 圣石洞穴 —— 按属性 / 均衡 / 短板
- 肉鸽塔快速战斗
- PVP 镜中之战 —— 倍率调整
- 赛季活动  —— 普通 / 挑战关卡、魔兽（自配队补刀 或 摆烂全自动）、场域小游戏

#### 🔧 装备
- 制作 / 强化 / 分解 / 精炼 — 全任务覆盖自动 / 精炼队列

#### 🎰 白嫖抽卡
- 每日免费抽 —— 速抽 / 策略抽
- 每日 90 钻优惠档（指定卡池）
- 保底保留 —— 临近必出时自动停抽

#### 💰 资源 & 奖励
- **地图资源吸取** —— 全卡带 / 每日用尽技能 / 周常存档续吸 / 卡带屏蔽
- 宠物自动派遣
- 餐馆自动结算 —— 收粉 / 升级 / 常客圣石 / 装饰币
- 一键清收 — 公会签到 / 小屋访问 / 广场祈求 / 日常任务 / 版本活动 / 通行证 / 普通邮件 & 商品邮件


#### 📅 周常
- 末日之书一轮 · 自动钓鱼 · 小屋点赞 · 浏览街机界面

### ⭐ 特色
- **跑商套利** — 自动识别当日最高价，批量进出货（可自定义商品列表） / 搓利润料理
- 接管钓鱼 — 检测收杆时机，全自动循环

#### 📥 半自动

- 爬塔刷层数 / 末日之书刷分数
- 生活技能刷级（完善中）
</details>
<br>


## 参与开发

长期是一人填战壕,**很欢迎友军**。无论你想改 Pipeline、写 Agent、补资源，还是只是反馈个带日志的 Bug——

👉 **完整参与指南见 [CONTRIBUTING.md](CONTRIBUTING.md)**：要什么 / 不要什么、提交后会经历什么、怎么搭环境、怎么进开发群，都在里面。

---

## 致谢

| 项目 / 作者 | 说明 |
|-------------|------|
| [MaaFramework](https://github.com/MaaXYZ/MaaFramework) | 自动化测试框架，本项目核心驱动 |
| [MaaPracticeBoilerplate](https://github.com/MaaXYZ/MaaPracticeBoilerplate) | 项目模板 |
| [MFAWPF](https://github.com/SweetSmellFox/MFAWPF) | Pipeline 协议通用 GUI |
| [MFAAvalonia](https://github.com/MaaXYZ/MFAAvalonia) | 基于 Avalonia 的通用 GUI |
| [MFAToolsPlus](https://github.com/SweetSmellFox/MFAToolsPlus) | 开发工具 |
| [maa-support-extension](https://github.com/neko-para/maa-support-extension) | VSCode 扩展 |
| [MaaPipelineEditor](https://github.com/kqcoxn/MaaPipelineEditor) | 可视化工具 |
| **京墨** | 跑商功能初始代码 ([7e5bb2a](https://github.com/sunyink/MFABD2/commit/7e5bb2abed984f6fd3cc254605f00e3b924cd982))，感谢付出与支持 |
| [@XiaoXKKK](https://github.com/XiaoXKKK) | 接管钓鱼初始代码 ([adb06bb](https://github.com/sunyink/MFABD2/commit/adb06bbb50f4be4150ca5b64119629af688ac8f9))，感谢付出与支持 |
| [@BebopSpikeSpiegel](https://github.com/BebopSpikeSpiegel) | PC 客户端（Windows 独立端）适配主力推进，内测通道核心贡献 |
| [@KoujiMinamoto](https://github.com/KoujiMinamoto) | PlayCover(iOS/macOS) 控制器支持与维护，让 Mac 免模拟器运行 |
| [JZPPP/MaaBD2](https://github.com/JZPPP/MaaBD2) | 早期参考项目 |

---

<div align="lift">

<table width="80%">
  <tbody>
    <tr>
      <td width="30%" align="center">
        <a href="https://afdian.com/a/MFABD2" target="_blank">
          <img width="180" src="https://pic1.afdiancdn.com/static/img/welcome/button-sponsorme.png" alt="去爱发电支持">
        </a>
      </td>
      <td width="70%">
        <b>☕ 支持项目</b><br>
        如果 MFABD2 帮您省下了宝贵的肝脏与时间，欢迎请开发者喝杯咖啡。<br>
        您的支持是项目持续迭代、修复问题的最强动力！
    </tr>
  </tbody>
</table>

</div>
