# Android ARM64 构建与发布

当前接入 MaaFwApp，面向 Android 9 及以上的 ARM64 真机；运行需要 Shizuku 或 root。
游戏内语言须设为**简体中文**。当前流程按简体文本匹配；繁体游戏界面即使被 OCR 正确读出，也可能因文字不同而不命中。
已在真机确认 agent 连接和部分自定义回调；完整任务、截图点击准确性和覆盖升级仍待验收，实验构建成功不等于已支持所有任务。

## 构建

`.github/workflows/android.yml` 是安卓构建的唯一入口，推送 main 上的构建相关改动或手动触发后生成签名 Release APK。
默认只上传 Actions 产物。手动传入已有 Release tag 作为 `version_name` 并开启 `publish`，
才会将 APK、SHA256 文件和构建元数据附加到该 Release；不会创建 Release 或覆盖同名附件。
主发布入口 `install.yml` 先判定通道，再调用 `install-build.yml` 完成整套构建。
普通 CI 只取消同分支上一轮，公测、内测、正式各自跨分支取消本通道上一轮，四个通道互不取消。
手动不勾发布选项归普通 CI，勾选 `ci_as_stable` 则归正式通道（仍保留 CI 版号和构建身份）。
构建流程由独立的 `android` job 调用安卓入口，在资源检查和安卓源码检查通过后，与 `install` 平台矩阵并行构建。
安卓 job 沿用原有发版条件：手动发版、版本标签推送、alpha / beta 发版才运行，普通 push 不通过主流程触发 APK。
两端沿用同一个版本标签，并用 `source_sha` 锁定与桌面包相同的资源提交。
`release_build=true` 保留安卓工作流自己的安装编号序列；`release` job 等待桌面、安卓和更新日志全部成功，
再按安卓 job 返回的运行编号取回 APK、校验文件和构建元数据，
核对提交、版本、文件名及摘要后，与桌面 ZIP 一起上传到草稿，全部附件核验一致后才公开。安卓失败或附件不匹配时停止发布。
发布通知等待发布步骤成功。旧安卓 ZIP 构建已停用，APK 直接作为发布附件，不再套一层 ZIP；
公开发布后，Mirror 上传流程从同一 Release 同步原始签名 APK（只传正式与公测，见「Mirror酱上传」）。
APK 文件名为 `MFABD2-<项目版本>-android-arm64.apk`，与桌面 ZIP 同构；安装编号只放在同名的 `.apk.json` 里。
首次接入时，应先将安卓工作流合入默认分支，再验收主流程的手动派发与发布上传；开发分支可手动触发安卓构建验证出包。
独立的 `android.yml` 发布构建按目标版本单独分组，不被开发分支的新推送取消；同一版本已有发布构建运行时，后续请求等待。
普通开发构建仍只保留同分支的最新运行。GitHub 默认每组只保留一个等待请求，不保证重复派发的请求全部执行。

- MaaFwApp 固定正式版 **v0.2.2**，commit `de65f7a43fc6a110706f44d756a0bf82b5c296d7`。
- 桌面 MaaFramework 由 `scripts/detect_maa_version.py` 下载根
  `requirements.txt` 钉住的 MFAAvalonia、用 ctypes 调其 `libMaaFramework.so` 的 `MaaVersion()`
  读出（`# MFA_CORE_TAG=` 填了就是覆盖后的版本），与 `install-build.yml` 的 meta job 同源；
  安卓通过根 `requirements.txt` 的 `# ANDROID_AGENT_CORE_TAG=3.13.15-maafw5.14.3`
  选择已发布的 MaaAgentCoreAndroid，传给上游构建器的 `--core-tag`；**两端现在均为 5.14.3**。
  安卓原生库 tag、pip 版本、`install.py` 参数和 APK 校验期望值全部由选中的 core 派生，
  避免 Python 自带的 maafw 与 App 原生库不一致。
- Android agent core 的 tag 形如 `<CPython 版本>-maafw<框架版本>`。MaaFwApp v0.2.2
  默认 `CORE_TAG` 仍为 `3.13.15-maafw5.13.0`；本项目显式选择 5.14.3，Python 仍为
  3.13.15、NumPy 仍为 2.3.2。显式选择必须与桌面框架版本完全一致，且 Python 版本必须
  匹配构建器的 `CORE_PY`，否则检查失败；此时不得同时配置 `ANDROID_MAAFW_LINE`。
  未配置显式 core 时才使用上游默认值：差补丁号警告，差次版本默认失败，除非
  `ANDROID_MAAFW_LINE` 明确接受上游所在的线。版本检查通过不代替 APK 与真机验证。
- v0.2.2 通过临时上游目录的 `local.properties` 中 `build.releaseAbi=arm64-v8a`
  选择手机架构；`prepare` 写入该配置并保留其他本地配置。MFABD2 的 GitHub 更新源继续使用自身按安装编号
  选包的客户端，不采用上游新增的 Mirror酱优先版本选择；Mirror酱更新源使用上游已有实现。
- 打进 APK 的 pip 清单由 `android_build.py requirements` 从根 `requirements.txt` 生成，
  仓库里不再有第二份手写清单。只有平台逼迫的三项在 `android/release.json` 的
  `python_requirements` 里覆盖：numpy 与 Pillow 带 C 扩展，必须落在 Chaquopy 实际发布了
  Android wheel 的版本上；StrEnum 桌面侧没有对应项。纯 Python 包沿用桌面约束。
- `android/pi-profile.yaml` 指向 `install/` 资源和 `android-build/agent-dist/` 解释器。
- 正式包名为 `io.github.sunyink.mfabd2`，身份配置及证书指纹位于 `android/release.json`。

工作流运行现有 `install.py` 并恢复 OCR 模型，再将资源、agent 和 Android Python 打进 APK。
构建后检查 ARM64 库、agent 启动配置、Python、NumPy/Pillow 原生扩展及资源包，拒绝夹带用户存档。
产物复用项目的注释剥离脚本，再由 MaaFramework 检查剥离后的资源；源码保留注释。
UI 原版从自身 git 历史生成 versionCode；本工作流在临时上游 checkout 中改为 CI 运行序号，
以便资源代码变化时也能触发手机端重新解包。
`android/update/` 独立保存本项目的数字版本更新策略及测试，构建时复制到固定上游并替换更新客户端注册。
不修改上游通用版本比较器；上游源码变化导致接入位置不匹配时停止构建。

## 源码检查（贡献者）

`.github/workflows/android-check.yml` 是两个入口共用的检查作业。主工作流在普通提交、PR
和手动运行中执行 `Check Android`，与资源检测并行；它失败会阻止 APK 和统一发布，
不会阻止 Windows、macOS、Linux 的 `install` 平台矩阵。fork PR 的检查只需读取权限，
不需要签名密钥。独立安卓构建也必须经过相同检查，主流程发版时会再次执行。

检查输出实际检出的资源提交、Android UI 提交、agent core、Python 和 MaaFw 版本，
APK 按这些输出检出并复核提交。Android UI 的固定提交只配置在共用检查工作流中。

检查分为两套解释器：构建配置测试使用 Python 3.11；Agent、存档、接口筛选、资源覆盖、
红点算法和业务导入检查使用上游 agent core 的 Python 版本，当前为 3.13.15。
依赖由同一个 `android_build.py requirements` 生成，当前 NumPy 为 2.3.2；不会将
桌面 `numpy<2` 直接安装进该环境，也不会因缺少依赖而改用模拟模块或跳过检查。

运行环境测试在新进程中模拟 Maa 的导入和注册边界，真实加载 `action`、`recognition`、
`fishing_agent`、启动与日志监听器。Maa 替身仅提供明确的模块和接口，缺项直接失败；
注册时创建实际业务类实例，并与源码声明核对。另一个进程使用真实 MaaFw 检查接口签名、
业务导入和本机构建环境中的原生注册，不连接宿主或控制器。临时存档不会接触用户数据。

本地复跑时先检出工作流固定的 MaaFwApp 到 `android-upstream/`，使用与 CI 相同的
Python 环境。以下版本参数以 `detect_maa_version.py agent-core-tag` 的实际输出为准：

```bash
# BUILD_PYTHON 为 Python 3.11；AGENT_PYTHON 为上游版本的独立环境解释器绝对路径。
"$BUILD_PYTHON" scripts/detect_maa_version.py agent-core-tag --maafw "$DESKTOP_MAAFW" --upstream android-upstream
"$BUILD_PYTHON" scripts/android_build.py requirements --maafw "$ANDROID_MAAFW" --output android-build/requirements.txt
"$AGENT_PYTHON" -m pip install -r android-build/requirements.txt
"$AGENT_PYTHON" scripts/verify_android_agent.py real --python-version "$ANDROID_PYTHON_VERSION" --maafw-version "$ANDROID_MAAFW"
"$AGENT_PYTHON" scripts/verify_android_runtime.py
```

其余检查命令集中列在共用工作流中。源码检查使用构建机器对应的 Python 包，证明的是
安卓版本组合下的源码行为和本机导入兼容性；Android ARM64 原生库、APK 签名与内容仍在
打包后独立校验，设备运行与存档升级仍需实机验收。不能用其中一层代替另一层。

## 两套身份

构建分两种身份，它们在设备上互不覆盖，可以同时装在同一台手机上：

| 身份 | 用于 | 签名密钥 | 包名 | 应用名 | 安装编号 |
| --- | --- | --- | --- | --- | --- |
| `release` | 正式 / beta / alpha 发布 | `ANDROID_SIGNING_*` | `io.github.sunyink.mfabd2` | MFABD2 | 真实递增 |
| `ci` | 项目 CI 版本通道、push 冒烟构建 | `ANDROID_CI_*` | `io.github.sunyink.mfabd2.ci` | MFABD2 Ci | 见下 |

身份由 `android.yml` 的 `identity` 输入决定，缺省为 `ci`；`install.yml` 发布时按
`version_type` 传入——CI 通道传 `ci`，其余传 `release`。CI 身份的 APK 在文件名末尾多一段
`-ci`。**只有会进 Release 的构建消耗安装编号序列**，其余固定为 1，好让反复的验证构建互相
覆盖、不必每次先卸载；注意固定编号并不能省下序列，`run_number` 是工作流级计数器，push 照样递增。

上游把 applicationId 拼成 `BASE_APPLICATION_ID` + `.` + profile 的 `app.id`，且没有
`applicationIdSuffix`，所以 CI 包名是 `prepare_ui` 改写 `android/pi-profile.yaml` 的
`app.id` 段得到的。锚点不在就停止构建，不会用错包名配对的 label 蒙混出包。

两把钥匙都经同一条 release 签名路径，**上游在缺 keystore 时产出的是装不上的未签名 APK，
不会回落到 debug 签名**，所以 CI 身份也必须显式提供密钥。期望的证书指纹与别名都从
`build-metadata.json` 读——那份已按身份写好期望值，因此校验按模式换的是比对目标而不是跳过，
两种身份都被完整验证。

需要配置四个仓库密文：`ANDROID_SIGNING_KEYSTORE_B64` / `ANDROID_SIGNING_PASSWORD`（正式，
别名 `mfabd2`），`ANDROID_CI_KEYSTORE_B64` / `ANDROID_CI_PASSWORD`（CI，别名见
`release.json` 的 `ci_key_alias`）。两把钥匙的证书指纹分别填在 `certificate_sha256` 与
`ci_certificate_sha256`；后者为空时 `metadata` 子命令会直接报错，不会静默构建出无法校验的包。
签名前检查证书指纹，打包后检查实际 APK 签名、包名、版号与不可调试标志；密文缺失或不匹配则停止。

**正式密钥及密码不得提交到代码仓库、作为构建产物上传或重新生成替代。** 签名文件只在 runner
临时目录存在。CI 那把钥匙无保密价值，但它签的包会进公开 Release，这意味着任何拿到同一把钥匙
的人都能签出同包名的包覆盖它——这是选择让 CI 通道使用独立身份的已知代价，正式包不受影响。

旧包 `com.aliothmoon.maafw.mfabd2.experimental` 使用临时 debug 签名，新正式身份会与旧包并存。
早期实验包只用于开发测试，不提供跨包名自动迁移。稳定身份的覆盖升级仍须真机验证。

## Agent 与存档

`agent/main.py` 在启动时生成一份不可变的 `RuntimeConfig`，统一决定运行模式、是否接管
Python 环境、内核库目录和存档策略，再把存档策略传给 `PersistentStore`。
平台/宿主判断集中在 `agent/utils/runtime_environment.py`；账号切换只换文件名，不重新探测环境。
独立使用存档模块的调用者也只在首次访问时解析并保留策略。

Android 不运行项目的桌面 venv 创建或安装步骤。宿主必须提供绝对的原生库目录，
包含 `libMaaFramework.so` 和 `libMaaAgentServer.so`；错误路径会在注册前报错。

| 宿主 | 存档位置 | 依据 |
| --- | --- | --- |
| MaaFwApp | 宿主 `files/pi` 的同级 `files/mfabd2-save` | `pi` 更新时整树重建；同级目录不参与资源替换 |
| MFAAvalonia Android | 资源根 `config/MFABD2` | v2.16.1 的资源更新保留用户 `config`；此路径适配尚未出包验证 |
| 其它宿主 | 必须显式指定 `MFABD2_DATA_DIR` 绝对路径 | 不猜测 Android 的 HOME 或桌面路径 |

`MFABD2_DATA_DIR` 优先于自动目录选择；应指向宿主可写、不会随资源/Python更新被清除的目录。
显式目录不可写会报错，不回落到资源根。既有桌面的全局/便携模式选择保持不变。
账号文件名、备份恢复和不可读存档的写保护沿用现有行为。
尚未提供桌面旧存档或早期 Android 试验存档的自动迁移。
应用卸载或清除应用数据不属于“保留存档的覆盖升级”。

本地最小验证（**需要 Python 3.11+**；项目桌面 dev 环境默认的 3.10 会在
`hashlib.file_digest` 与 `TestCase.enterContext` 上直接报 AttributeError）：

```sh
python scripts/verify_android_runtime.py
python scripts/verify_android_interface_filter.py
python scripts/verify_android_overlay.py
python scripts/verify_android_build.py
python agent/recognition/test_rdd_hsv_rescue.py
python scripts/verify_android_apk.py path/to/app-release.apk

python scripts/detect_maa_version.py parse                     # 读 requirements.txt 钉住的 tag 与安卓线声明
python scripts/detect_maa_version.py agent-core-tag --maafw 5.14.3
```

`detect` 子命令要有解压好的 MFAAvalonia 才能跑，只在 CI 验证。

构建工具验证需要固定 UI 源码位于 `android-upstream`，或通过 `MFABD2_ANDROID_UPSTREAM` 指定位置。
本地检查不能替代真机上的完整任务和覆盖升级测试。

## 资源列表

MaaFwApp 的“服务器”列表对应 PI 的 `resource`。其固定版本在配置层采用第一条 `Adb`
controller 声明，实际运行使用 AndroidNativeController；该版本未按 `resource.controller`
筛选列表。安装脚本在生成 Android interface 时应用这些限制，仅保留可用的控制器和资源。
当前项目产物中只保留 `Adb` / `ADB`。桌面源码 interface 和桌面安装产物继续包含各平台声明。
安卓资源显示为“安卓原生机”，实际按 `base → android_native` 加载；任务及预设按控制器限制筛选。
安卓打包只复制 `base`、`android_native` 两个资源包，另保留 `Announcement` 公告帮助和资源根目录文件；
`pc`、`playcover` 等其他资源目录不进入产物，复用安装目录时也会清理这些残留。桌面仍复制全部资源。
`android/maaowm.json` 是该覆盖包的 MaaOWM 配置，输出为 Pipeline V1。
覆盖包只有两个启动节点：跳过不受支持的 Shell 探针，直接交给已实测成功的 StartApp；
保留 base 的加载识别链，失败进入公共兜底，不再调用 Shell 启动兜底。

## 当前版号与发布阶段

- 显示版本：有显式项目版本时原样保留，包括 `v4.3.19-beta.3.260909.gabcdef0`。未指定时与桌面发版共用 `scripts/version_rules.py` 生成：普通开发构建为 `vX.Y.Z-ci.YYMMDD.sha`，提交末行带 alpha/beta 标记时预演本次发版会拿到的 `-beta.N.YYMMDD.gsha` 版号。
- 系统内部 `versionCode`：`version_code_offset + GITHUB_RUN_NUMBER * 100 + GITHUB_RUN_ATTEMPT`。限制attempt为1至99、整体不超过2100000000，与SemVer、日期或SHA无关。
- 正式、Beta、Alpha、CI 共用这一序列，不给渠道分配数字区间。新运行的编号大于之前所有运行的编号；同一次运行的重跑只增加末两位。重跑旧运行仍占旧编号区间，并不成为最新构建；需要将旧源码重新发行时，手动新建一次工作流运行，不使用旧运行的 Re-run。
- 资源 interface.version 和 APK versionName 使用相同显示版本；versionCode 变化也触发 UI 资源重新解包。
- 所有入口都派发到同一个android.yml，使用该工作流自己的run_number；不改成继承另一个工作流编号的workflow_call。迁移/重建工作流时需调整offset，确保不会归零或倒退。
- APK 文件名为 `MFABD2-<项目版本>-android-arm64.apk`（CI 身份多一段 `-ci`），与桌面 ZIP 同构，
  旁边附带 `.apk.json` 和 `.apk.sha256`。**安装编号只在 JSON 里，不在文件名里**：JSON 包含
  `schema_version: 1`、`identity`、`version_code`、`version_name`、包名、证书指纹、APK 文件名及
  `apk_sha256`；这三份附件由同一构建生成，实际 APK 的版号及签名通过校验后才输出。发布说明结尾
  另有一行编号，那是给人看的。
- GitHub 更新先按现有 Stable/Beta 选择过滤，再读**最近 3 个**候选 Release 的 JSON，取其中编号
  最大的；只比较本机 `BuildConfig.VERSION_CODE`，不按显示版号或 SHA 排序。之所以不只看最新一个
  Release，是因为发布顺序不必然等于安装编号顺序，而"最新即最大"是一条只靠流程纪律维持的假设。
  每份 JSON 都要核对编号、包名、证书指纹、标签与文件名后才算数。下载时重新执行同一策略，并拒绝
  相同或更小的编号。
- 缺少 APK/JSON/SHA256 任一附件、旧命名（带 `-vc`）的包、以及 CI 身份的 `-ci` 包都不会作为正式
  更新候选——后者由另一把钥匙签名，装不上也不该被推荐。元数据损坏或身份不符明确报错，不回落到
  字符串比较；某个候选的 JSON 取不到时不会掩盖其余可用候选；GitHub 提供的 APK 摘要与 JSON 不一致
  时也拒绝下载。
- **CI 身份的包没有可用的内置更新**：它的包名与签名都与 Release 里的正式包对不上，GitHub 更新检查会
  报元数据无效；它也不带 Mirror酱 RID。这是有意的取舍，CI 包请手动去发布页下载。
- 例如先发布测试构建 `601`，再发布正式构建 `701`，可直接覆盖回正式；反向安装 `601` 属于降级，需要重装。同一个标签重建可以有不同编号，安装和更新依然按数字判断。显示版本继续沿用项目原文。
- 当前上游只有 Stable/Beta 更新渠道，Alpha/CI 自动发现并不完整。
- 只有正式身份的包带 Mirror酱 RID：`android_build.py prepare` 把 `assets/interface.json` 的 `mirrorchyan_rid`
  写进 PI profile 的 `update.mirrorchyanRid`（上游以它为准，压过 PI 自带字段）；APK 内的 `interface.json`
  不带 RID，`verify_android_apk.py` 也这样检查。Mirror酱按 `os=android`、`arch=arm64` 查询，检查更新无需 CDK，下载需要有效 CDK。
- 两个来源的公测频道不对等：GitHub 源接受任何预发布、按安装编号取最大，会推到内测；Mirror酱的公测频道返回正式与公测中
  版本号较大的那个，且只收正式与公测的 APK。从 GitHub 装了内测再切到 Mirror酱，会一直显示已是最新，直到出现版本号更大的公测或正式版。
- 新安装默认使用 GitHub 更新源，可在设置中选择 Mirror酱；不会覆盖用户已保存的来源偏好。
  旧版安卓包未包含 RID，选 Mirror酱会提示缺少更新配置，需先通过 GitHub 更新或手动安装新版。
- 单元测试覆盖渠道过滤、编号而非发布顺序决定胜出、扫描深度上限、不完整附件、旧命名与 `-ci` 包
  被排除、元数据不匹配、单个 sidecar 取不到时不掩盖其余候选、以及降级拒绝；在 Android CI 中运行
  `:app:testReleaseUnitTest` 的 MFABD2、Mirror酱更新测试和更新模块装配测试。真机安装和下载体验仍单独验收。
- AGP 9 默认只生成 Debug 单元测试，CI 对测试命令显式传入 `-Pandroid.onlyEnableUnitTestForTheTestedBuildType=false` 以生成 Release 测试任务；测试报告作为 `android-update-tests` 附件保存。

## Mirror酱上传

`.github/workflows/mirrorchyan_release.yml` 的安卓任务匹配 `MFABD2-*-android-arm64.apk`，
只传原始签名 APK，不包含 `.apk.json`、`.apk.sha256` 或 `-ci.apk`，也不使用桌面资源热更包。
它随公开 Release 后的自动派发运行，只处理正式与公测标签；内测标签（安卓端没有内测频道）与未填 `tag` 的手动运行都跳过。
不单独补传历史版本：已发布的旧包不带 RID，查询不了 Mirror酱，补传没有用处。

- Mirror酱的增量类型资源只收 zip，上传 APK 会在第一步被拒（HTTP 400 `file ext not supported`）；
  APK 所在的资源或平台必须由 Mirror酱官方设为全量更新。
- **Mirror酱没有删除或隐藏版本的接口。** 只删 GitHub Release 撤不掉已上传的 APK，Mirror酱会继续下发；
  安装编号只增不减，装上的用户回退只能卸载重装。出问题时尽快发更高版本顶替，或联系 Mirror酱官方下架。

## 当前验收边界

真机已确认资源列表、任务选项及部分agent回调。截图整体偏暗的上游问题仍影响颜色匹配，暂不调整公共颜色阈值。
固定签名 APK 已通过 CI 的身份及结构检查；主流程等待 APK 后统一发布的链路仍待实际发版验收。原生启动覆盖、完整任务、数字版本内置更新和覆盖升级保留存档仍需要实机验收。旧实验包迁移不在开发范围内。
Mirror酱 APK 上传、带 CDK 下载、手机覆盖升级与存档保留仍待线上及实机验收。
应用图标沿用已入库的 `ReadMe/logo.png`（180×180），后续可提供更高分辨率的品牌图标。

## 上游依据

- [MaaFwApp 接入文档](https://github.com/Aliothmoon/MaaFwApp/blob/de65f7a43fc6a110706f44d756a0bf82b5c296d7/INTEGRATION.md)
- [MaaFwApp 资源替换](https://github.com/Aliothmoon/MaaFwApp/blob/de65f7a43fc6a110706f44d756a0bf82b5c296d7/app/src/main/java/com/aliothmoon/maafw/project/PiInstaller.kt)
- [Android agent core](https://github.com/Aliothmoon/MaaAgentCoreAndroid/releases)（具体 tag 由根 `requirements.txt` 选择）
- [MFAAvalonia Android 资源替换](https://github.com/MaaXYZ/MFAAvalonia/blob/v2.16.1/MFAAvalonia.Android/AndroidAssetBootstrap.cs)
- [Android 版本名与版本码](https://developer.android.com/studio/publish/versioning)
- [Android 应用签名](https://developer.android.com/studio/publish/app-signing)
