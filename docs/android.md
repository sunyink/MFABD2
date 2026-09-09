# Android ARM64 实验构建

当前接入 MaaFwApp，面向 Android 9 及以上的 ARM64 真机；运行需要 Shizuku 或 root。
游戏内语言须设为**简体中文**。当前流程按简体文本匹配；繁体游戏界面即使被 OCR 正确读出，也可能因文字不同而不命中。
已在真机确认 agent 连接和部分自定义回调；完整任务、截图点击准确性和覆盖升级仍待验收，实验构建成功不等于已支持所有任务。

## 构建

`.github/workflows/android.yml` 是独立实验工作流，推送 `feat/android-native` 的相关改动或手动触发后构建。
仅上传 Actions 构建产物，不发布 Release，也不替换既有桌面构建。

- MaaFwApp 固定 commit `f4f6f220e21e3a1b7b0cf5df4bdbe0ec04c668f7`。
- MaaFramework 原生库与 Python binding 均为 5.12.3。
- Android agent core 为 `3.13.15-maafw5.12.3`，包含 NumPy 2.3.2。
- Pillow 固定 11.0.0，使用 Chaquopy 的 Android wheel；其共享库路径已加入 profile。
- `android/pi-profile.yaml` 指向 `install/` 资源和 `android-build/agent-dist/` 解释器。
- 包名为 `com.aliothmoon.maafw.mfabd2.experimental`，仅为实验用途。

工作流运行现有 `install.py` 并恢复 OCR 模型，再将资源、agent 和 Android Python 打进 APK。
构建后检查 ARM64 库、agent 启动配置、Python、NumPy/Pillow 原生扩展及资源包，拒绝夹带用户存档。
UI 原版从自身 git 历史生成 versionCode；本工作流在临时上游 checkout 中改为 CI 运行序号，
以便资源代码变化时也能触发手机端重新解包。

当前使用构建机临时 debug 签名。不同构建机的包不保证能互相覆盖安装；正式升级验收前必须
配置持续保管的签名密钥。不要为了安装新测试包而卸载仍有重要存档的旧包。

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
python agent/recognition/test_rdd_hsv_rescue.py
python tools/verify_android_apk.py path/to/app-debug.apk
```

前三项验证宿主接口、资源筛选和 Python 行为；不能替代真机上的 agent 连接、权限、日志、图像处理和升级测试。

## 资源列表

MaaFwApp 的“服务器”列表对应 PI 的 `resource`。其固定版本在配置层采用第一条 `Adb`
controller 声明，实际运行使用 AndroidNativeController；该版本未按 `resource.controller`
筛选列表。安装脚本在生成 Android interface 时应用这些限制，仅保留可用的控制器和资源。
当前项目产物中只保留 `Adb` / `ADB`。桌面源码 interface 和桌面安装产物继续包含各平台声明。

## 当前版号与发布阶段

- 显示版本：`android-dev.<GITHUB_RUN_NUMBER>.<GITHUB_RUN_ATTEMPT>`。
- 系统内部 `versionCode`：`GITHUB_RUN_NUMBER * 100 + GITHUB_RUN_ATTEMPT`，例如第2次构建首次运行是201。
- 资源 interface.version 和 APK versionName 使用相同显示版本；versionCode 变化也触发 UI 资源重新解包。
- 这套序号用于当前实验工作流。正式发布应让显示版本跟随项目发布tag，内部versionCode采用独立、持续递增的编号；切换工作流或重建编号来源时不能归零。
- GitHub Release 可以直接提供 `.apk` 和 `.sha256`。当前项目主发布工作流仅收集ZIP，安卓实验工作流仅上传Actions产物；正式APK发布与稳定签名尚未接入。

## 上游依据

- [MaaFwApp 接入文档](https://github.com/Aliothmoon/MaaFwApp/blob/f4f6f220e21e3a1b7b0cf5df4bdbe0ec04c668f7/INTEGRATION.md)
- [MaaFwApp 资源替换](https://github.com/Aliothmoon/MaaFwApp/blob/f4f6f220e21e3a1b7b0cf5df4bdbe0ec04c668f7/app/src/main/java/com/aliothmoon/maafw/project/PiInstaller.kt)
- [Android agent core](https://github.com/Aliothmoon/MaaAgentCoreAndroid/releases/tag/3.13.15-maafw5.12.3)
- [MFAAvalonia Android 资源替换](https://github.com/MaaXYZ/MFAAvalonia/blob/v2.16.1/MFAAvalonia.Android/AndroidAssetBootstrap.cs)
