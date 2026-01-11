<!-- markdownlint-disable MD033 MD041 -->
<div align="center">
  <img alt="LOGO" src="https://github.com/sunyink/MFABD2/blob/main/ReadMe/logo.png" width="180" height="180" />


# MaaBD2-棕色尘埃2自动化助手
[![License](https://img.shields.io/badge/License-MIT%2FApache--2.0-blue)](./LICENSE)
[![.NET Version](https://img.shields.io/badge/.NET-≥%2010-512BD4?logo=csharp)](https://dotnet.microsoft.com/download/dotnet/10.0)
[![月提交](https://img.shields.io/github/commit-activity/m/sunyink/MFABD2?label=开发活跃度&color=blue)](https://github.com/sunyink/MFABD2/commits/main)
[![给项目点赞](https://img.shields.io/github/stars/sunyink/MFABD2?style=social&label=给项目点赞)](https://github.com/sunyink/MFABD2)
<br>
<a href="https://mirrorchyan.com/zh/projects?rid=MFABD2" target="_blank"><img alt="mirrorc" src="https://img.shields.io/badge/Mirror%E9%85%B1-%239af3f6?logo=countingworkspro&logoColor=4f46e5"></a>
[![构建状态](https://img.shields.io/badge/构建状态-通过-success?logo=githubactions)](https://github.com/sunyink/MFABD2/actions)
[![最新版本](https://img.shields.io/github/v/release/sunyink/MFABD2?label=最新版本&logo=github&color=green)](https://github.com/sunyink/MFABD2/releases)
</div>

本项目基于 [MaaFramework](https://github.com/MaaXYZ/MaaFramework) 所提供的项目模板进行开发的棕色尘埃2自动化助手。

> **MaaFramework** 是基于图像识别技术、运用 [MAA] 开发经验去芜存菁、完全重写的新一代自动化黑盒测试框架。
> 低代码的同时仍拥有高扩展性，旨在打造一款丰富、领先、且实用的开源库，助力开发者轻松编写出更好的黑盒测试程序，并推广普及。

本项目继承 [JZPPP/MaaBD2](https://github.com/JZPPP/MaaBD2)衣钵 继续跟进游戏版本更新，主攻任务流程的优化强化。（任务流程基本重做，以前经验不再适用。）

> 向[JZPPP/MaaBD2]的开发和支持者致敬，在前人肩膀上起步事半功倍。



## 获取&部署

### ◆稳定版部署(版本号+无印)
+ 进入 **[MFABD2-Resource]发行版页** : [github.com/sunyink/MFABD2](https://github.com/sunyink/MFABD2/releases)，选择**纯数字不带单词**的版本，下载对应平台压缩包，解压即用。

### ◆公测版部署(版本号+Beta)
+ v3.* 计划后，更新主战场将移动到mirror`公测版`通道,标志为`带有beta`的版号。
+ `正式版`会更稳定，但也较大的落后于最新`公测版`。（具体约等于‘bug被发现时间’+‘修复’+‘修补补丁被验证时间’ ）
+ 进入 **[MFABD2-Resource]发行版页** : [github.com/sunyink/MFABD2](https://github.com/sunyink/MFABD2/releases)，选择带**Beta版本**，下载对应平台压缩包，解压即用。

####  `v2.1.0`之后，UI内可以直接获得更新信息。


## 使用方式 （仅支持模拟器使用）


>  功能、任务流程正在调试中，有问题可提交issues。能录or拍下视频最好，出问题前后几秒(步)带模拟器画面和MFA回显能，能帮助我快速排错。
  
  尽量在良好网络环境下运行。触发重连转圈可能吞掉那一瞬间操作导致错误，故失误概率可以是罕见、偶尔、泪目。

  1.推荐使用MuMu模拟器12运行游戏（可以直接使用国际版or自行配置Google环境），模拟器显示建议设置为`平板版 1920*1080 (DPI ≈ 270 or 280)`。
> - 多模拟器运作需要一定ADB知识。参考：adb.exe可视为windows服务，默认服务端口号`5037`，模拟器为其客户端，有各自的客户端口号。Mumu模拟器根据多开器编号决定`Adb端口号`，默认`0`号为`16384`、`1`号为`16416`。国际版与普通版可以共存。后启动的版本的端口号遵循前者规律+1，如`16385`。通常adb开启一个就可以控制多个模拟器，不建议多版本Adb并行启动，可能影响识别，可以自定义ADB路径与端口解决。
  

 2.**当前游戏内设置已支持自动设定**，但需要先切到简体中文。
    
  在此贴出游戏内设置的设定值备用：均为默认基础上，其中设置 `操作-选择操作方式-点击地面` `图像-分辨率-FHD`、`图像-游戏开始画面-主界面`、`通知-黄色圆点-不使用` 。
 > 画面质量异常请进行检查，步骤2后仍异常可尝试切换渲染方法。_一般经验，新机器`Vulkan`、旧机器`DirectX`表现较好。_

 3.任务流程已基本实现箱庭地图状态识别、技能传送阵自行生成、几乎所有自行复位(餐馆卡带例外，请避免从餐馆下线)。推荐在`剧情主线地图`、`传送阵上`结束游戏。
 > 利用025.08新出的‘快速卡带’功能，已实现采集卡带自定义选择；需要在‘快速卡带’页面，将意向关卡标黄，如此标黄卡带就会移动到最前，操作顺序决定标黄卡带的排序；每日采集只能去3个卡带，利用下拉菜单分配剧情卡带/角色卡带。

 4.一些配置如下，配置一次就可以了

 （除强化目标等级、金币消耗限额外，已支持自动设置，但建议检查。）

 装备请保留一件 SR/UR +9的装备用来精炼
 >
>   PVP战斗和分解装备的设置如图，请手动设置一次即可（技能位置与装备强化等设置为模拟器本地保存，不会影响其他端游戏；强化设置老登可以自行适度上强度；PVP设置10倍也是可以的。）

>
> 可以关闭技能动画，加快速度
> 
- 人物技能请按图中设置

 > <img alt="LOGO" src="https://github.com/sunyink/MFABD2/blob/main/ReadMe/IT-2511.jpg"  width="600px"/>
 > <img alt="LOGO" src="https://github.com/sunyink/MFABD2/blob/main/ReadMe/3.png"  width="400px"/>

---
<details>
  <summary>Mac系统使用指引</summary>
  经反馈Mac可以使用，配置过程复杂些。可以参考[M9A文档站](https://1999.fan/zh_cn/manual/newbie.html)，因为是同样的框架，软件部署部分是相通的。
</details>

## 功能一览

* [X] GUI界面 
  * [开发中] 增加一些自定义的功能、对已有功能完善。
  * [研究中] GUI界面交互

* [X] 启动游戏 
  * 能更新游戏程序
  * 能更新数据版本
  * （Google Play环境与网络环境需要自行解决）

* [X] 日常任务
  * [x] (启动自动获取)主页面下发奖励
  * [x] (可开关/按需)自动配置前置设置
  * [x] (启动自动获取)宠物派遣
  * [X] 扫荡
    * [X] 狩猎场关卡选择 
    * [X] 冒险航线策略、次数选择
    * [X] 圣石洞穴
      * [x]  属性选择
      * [x]  均衡刷取
  * [X] 采集派遣 
    * [X] 采集策略选择
      * [X] 自定义剧情卡带/角色卡带
  * [X] PVP
    * [X] PVP次数选择
  * [X] 白嫖抽卡
    * [ ] 每日90钻优惠抽：指定卡池
  * [X] 装备制作强化分解精炼
    * [X] 装备条件筛选、标记排序（精炼队列）
  * [X] 领取日常任务奖励
    * [X] 公会签到
    * [X] 餐馆结算/常客圣石领取
    * [X] 广场每日祈求
  * [X] 活动页版本活动领取（持续适配更新）
  * [X] 领取通行证奖励
  * [X] 肉鸽塔每日快速战斗 (子选项，默认关闭) 
  * [X] 领取邮件-普通与商品
* [X] 每周独立周常
    * [X] 末日之书一轮
    * [X] 小屋访问点赞
    * [X] 浏览街机界面
* [一期] 商店套利 `注1`
* [ ] 半自动操作：
  * [ ] 爬塔刷层数
  * [ ] 末日之书刷分数
  * [ ] （半自动指：手动调到页面后无限循环刷。**常闭，一次只能开一种**）
* [更多技能完善中] 生活技能刷级-砍价
* [ ] 商店套利（跑商） `注1`
* [ ] 接管钓鱼 `注2`
* [X] 活动图
  * [X] 关卡扫荡
    * [X] 难度选择（普通\挑战）
    * [X] 魔兽补刀
    * [ ] 小游戏扫荡(平时应关闭)


    

### 致谢

- [MaaFramework](https://github.com/MaaXYZ/MaaFramework) 自动化测试框架

- [MaaPracticeBoilerplate](https://github.com/MaaXYZ/MaaPracticeBoilerplate) MaaFramework项目模板

- [MFAWPF](https://github.com/SweetSmellFox/MFAWPF) Pipeline协议项目的通用GUI
- [MFAAvalonia](https://github.com/SweetSmellFox/MFAAvalonia) 基于Avalonia的MAAFramework通用GUI项目
- [MFATools](https://github.com/SweetSmellFox/MFATools) 开发工具
- [maa-support-extension](https://github.com/neko-para/maa-support-extension) VSCode扩展
- [SHA](SHA地址)跑商功能初始一整套完整代码，系**京墨**佬倾情提供，感谢他的付出与支持`←注1`
### 规范
> 如果要参与开发，可以参考文档。必须先本地测试通过再合并，请使用非快速合并-no-ff参数。

- [Pipeline协议规范](https://maafw.xyz/docs/1.1-QuickStarted)
- [帮助](/开发帮助.txt)
- [工具]制作了发布用Hook,运行`git config core.hooksPath scripts/hooks`,后直接`git commit`可以打开提交信息辅助。帮助快速享用发版系统。


## 鸣谢

本项目继承 **[JZPPP/MaaBD2](https://github.com/JZPPP/MaaBD2)** 成果，感谢植树！
本项目由 **[MaaFramework](https://github.com/MaaXYZ/MaaFramework)** 强力驱动！

