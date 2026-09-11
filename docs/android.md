# Android ARM64 构建与发布

当前接入 MaaFwApp，面向 Android 9 及以上的 ARM64 真机；运行需要 Shizuku 或 root。
游戏内语言须设为**简体中文**。当前流程按简体文本匹配；繁体游戏界面即使被 OCR 正确读出，也可能因文字不同而不命中。
已在真机确认 agent 连接和部分自定义回调；完整任务、截图点击准确性和覆盖升级仍待验收，实验构建成功不等于已支持所有任务。

## 构建

`.github/workflows/android.yml` 是安卓构建的唯一入口，推送开发分支或手动触发后生成签名 Release APK。
默认只上传 Actions 产物。手动传入已有 Release tag 作为 `version_name` 并开启 `publish`，
才会将 APK、SHA256 文件和构建元数据附加到该 Release；不会创建 Release 或覆盖同名附件。
主发布流程先调用这一入口，沿用同一个版本标签，并用 `source_sha` 锁定与桌面包相同的资源提交。
`release_build=true` 保留安卓工作流自己的安装编号序列；主流程等待这次构建成功，取回 APK、校验文件和构建元数据，
核对提交、版本、文件名及摘要后，与桌面 ZIP 一起上传到新建的 Release。安卓失败或附件不匹配时停止发布。
发布通知等待发布步骤成功。旧安卓 ZIP 构建与镜像上传入口已停用，APK 直接作为发布附件，不再套一层 ZIP。
APK 文件名沿用 `MFABD2-<项目版本>-android-arm64` 前缀，额外保留 `-vc<versionCode>` 供安装和内置更新识别构建先后。
首次接入时，应先将安卓工作流合入默认分支，再验收主流程的手动派发与发布上传；开发分支的推送构建可先验证出包。
发布构建按目标版本单独分组，不被开发分支的新推送取消；同一版本已有发布构建运行时，后续请求等待。
普通开发构建仍只保留同分支的最新运行。GitHub 默认每组只保留一个等待请求，不保证重复派发的请求全部执行。

- MaaFwApp 固定 commit `f4f6f220e21e3a1b7b0cf5df4bdbe0ec04c668f7`。
- MaaFramework 原生库与 Python binding 均为 5.12.3。
- Android agent core 为 `3.13.15-maafw5.12.3`，包含 NumPy 2.3.2。
- Pillow 固定 11.0.0，使用 Chaquopy 的 Android wheel；其共享库路径已加入 profile。
- `android/pi-profile.yaml` 指向 `install/` 资源和 `android-build/agent-dist/` 解释器。
- 正式包名为 `io.github.sunyink.mfabd2`，身份配置及证书指纹位于 `android/release.json`。

工作流运行现有 `install.py` 并恢复 OCR 模型，再将资源、agent 和 Android Python 打进 APK。
构建后检查 ARM64 库、agent 启动配置、Python、NumPy/Pillow 原生扩展及资源包，拒绝夹带用户存档。
产物复用项目的注释剥离脚本，再由 MaaFramework 检查剥离后的资源；源码保留注释。
UI 原版从自身 git 历史生成 versionCode；本工作流在临时上游 checkout 中改为 CI 运行序号，
以便资源代码变化时也能触发手机端重新解包。
`android/update/` 独立保存本项目的数字版本更新策略及测试，构建时复制到固定上游并替换更新客户端注册。
不修改上游通用版本比较器；上游源码变化导致接入位置不匹配时停止构建。

构建使用仓库密文 `ANDROID_SIGNING_KEYSTORE_B64` 和 `ANDROID_SIGNING_PASSWORD`，别名为 `mfabd2`。
签名前检查证书指纹，打包后检查实际 APK 签名、包名、版号与不可调试标志；密文缺失或不匹配则停止。
密钥及密码不得提交到代码仓库、作为构建产物上传或重新生成替代。签名文件只在 runner 临时目录存在。

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

本地最小验证：

```sh
python tools/verify_android_runtime.py
python tools/verify_android_resources.py
python tools/verify_android_overlay.py
python tools/verify_android_build.py
python agent/recognition/test_rdd_hsv_rescue.py
python tools/verify_android_apk.py path/to/app-release.apk
```

构建工具验证需要固定 UI 源码位于 `android-upstream`，或通过 `MFABD2_ANDROID_UPSTREAM` 指定位置。
本地检查不能替代真机上的完整任务和覆盖升级测试。

## 资源列表

MaaFwApp 的“服务器”列表对应 PI 的 `resource`。其固定版本在配置层采用第一条 `Adb`
controller 声明，实际运行使用 AndroidNativeController；该版本未按 `resource.controller`
筛选列表。安装脚本在生成 Android interface 时应用这些限制，仅保留可用的控制器和资源。
当前项目产物中只保留 `Adb` / `ADB`。桌面源码 interface 和桌面安装产物继续包含各平台声明。
安卓资源显示为“安卓原生机”，实际按 `base → android_native` 加载；任务及预设按控制器限制筛选。
`android/maaowm.json` 是该覆盖包的 MaaOWM 配置，输出为 Pipeline V1。
覆盖包只有两个启动节点：跳过不受支持的 Shell 探针，直接交给已实测成功的 StartApp；
保留 base 的加载识别链，失败进入公共兜底，不再调用 Shell 启动兜底。

## 当前版号与发布阶段

- 显示版本：有显式项目版本时原样保留，包括 `v4.3.19-beta.260909.abcdef`。普通开发构建沿用项目现有规则生成 `vX.Y.Z-ci.YYMMDD.sha`，提交末行的alpha/beta标记也按现行规则处理。
- 系统内部 `versionCode`：`version_code_offset + GITHUB_RUN_NUMBER * 100 + GITHUB_RUN_ATTEMPT`。限制attempt为1至99、整体不超过2100000000，与SemVer、日期或SHA无关。
- 正式、Beta、Alpha、CI 共用这一序列，不给渠道分配数字区间。新运行的编号大于之前所有运行的编号；同一次运行的重跑只增加末两位。重跑旧运行仍占旧编号区间，并不成为最新构建；需要将旧源码重新发行时，手动新建一次工作流运行，不使用旧运行的 Re-run。
- 资源 interface.version 和 APK versionName 使用相同显示版本；versionCode 变化也触发 UI 资源重新解包。
- 所有入口都派发到同一个android.yml，使用该工作流自己的run_number；不改成继承另一个工作流编号的workflow_call。迁移/重建工作流时需调整offset，确保不会归零或倒退。
- APK 文件名为 `MFABD2-<项目版本>-android-arm64-vc<versionCode>.apk`，旁边附带 `.apk.json` 和 `.apk.sha256`。JSON 包含 `schema_version: 1`、`version_code`、`version_name`、包名、证书指纹、APK 文件名及 `apk_sha256`；这三份附件由同一构建生成，实际 APK 的版号及签名通过校验后才输出。
- GitHub 更新先按现有 Stable/Beta 选择过滤，再从附件名选出最大的数字编号；只比较本机 `BuildConfig.VERSION_CODE`，不按显示版号或 SHA 排序。仅对最终候选读取一份 JSON，核对其编号、包名、证书指纹、标签、文件名和摘要，避免逐个下载所有历史版本的 JSON。下载时重新执行同一策略，并拒绝相同或更小的编号。
- 缺少 APK/JSON/SHA256 任一附件、旧命名的占位包不会作为更新候选。元数据损坏或身份不符明确报错，不回落到字符串比较；GitHub 提供的 APK 摘要与 JSON 不一致时也拒绝下载。
- 例如先发布测试构建 `601`，再发布正式构建 `701`，可直接覆盖回正式；反向安装 `601` 属于降级，需要重装。同一个标签重建可以有不同编号，安装和更新依然按数字判断。显示版本继续沿用项目原文。
- 当前上游只有Stable/Beta更新渠道，Alpha/CI自动发现并不完整。安卓端暂不暴露旧MirrorChyan RID，避免拉取遗留ZIP；Mirror的APK与架构对接需单独验证。
- 新安装默认使用 GitHub 更新源。此前已经保存 MirrorChyan 选择的测试安装需要在设置中手动切换；不会强行覆盖用户已保存的来源偏好。
- 单元测试覆盖同日 SHA 逆序、渠道过滤、测试回正式、同标签重建、不完整附件、元数据不匹配及降级拒绝；在 Android CI 中运行 `:app:testReleaseUnitTest` 的 MFABD2 更新测试和更新模块装配测试。真机安装和下载体验仍单独验收。
- AGP 9 默认只生成 Debug 单元测试，CI 对测试命令显式传入 `-Pandroid.onlyEnableUnitTestForTheTestedBuildType=false` 以生成 Release 测试任务；测试报告作为 `android-update-tests` 附件保存。

## 当前验收边界

真机已确认资源列表、任务选项及部分agent回调。截图整体偏暗的上游问题仍影响颜色匹配，暂不调整公共颜色阈值。
固定签名 APK 已通过 CI 的身份及结构检查；主流程等待 APK 后统一发布的链路仍待实际发版验收。原生启动覆盖、完整任务、数字版本内置更新和覆盖升级保留存档仍需要实机验收。旧实验包迁移不在开发范围内。
应用图标沿用已入库的 `ReadMe/logo.png`（180×180），后续可提供更高分辨率的品牌图标。

## 上游依据

- [MaaFwApp 接入文档](https://github.com/Aliothmoon/MaaFwApp/blob/f4f6f220e21e3a1b7b0cf5df4bdbe0ec04c668f7/INTEGRATION.md)
- [MaaFwApp 资源替换](https://github.com/Aliothmoon/MaaFwApp/blob/f4f6f220e21e3a1b7b0cf5df4bdbe0ec04c668f7/app/src/main/java/com/aliothmoon/maafw/project/PiInstaller.kt)
- [Android agent core](https://github.com/Aliothmoon/MaaAgentCoreAndroid/releases/tag/3.13.15-maafw5.12.3)
- [MFAAvalonia Android 资源替换](https://github.com/MaaXYZ/MFAAvalonia/blob/v2.16.1/MFAAvalonia.Android/AndroidAssetBootstrap.cs)
- [Android 版本名与版本码](https://developer.android.com/studio/publish/versioning)
- [Android 应用签名](https://developer.android.com/studio/publish/app-signing)
