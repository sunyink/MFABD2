"""源 interface.json → 发布 interface.json 的唯一出口。

assets/interface.json 是开发配置：VS Code MaaSupport 直接读它，agent 与 pretask 指向开发机
的 .venv，几个选项按「每个任务各存一份」的客户端摆放。发出去的每个包都要按目标改写，
而改写只在这里发生——平台差异只写在 PLATFORMS，改写顺序只写在 STEPS。install.py 拷完
资源与 agent 之后调用 install() 一次；其他脚本只读、不写 install/interface.json。

设计、步骤顺序的理由与否决过的方案见 priv doc/发布与CI.md §二。
"""

import argparse
from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile

from prepare_icons import report, validate_png
from strip_build_comments import strip_jsonc_comments, strip_trailing_commas

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "assets" / "interface.json"
BRANDING = ROOT / "release" / "branding"


# ── 平台表 ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Platform:
    agent_exec: str
    agent_args: tuple
    # 本平台跑不起来的控制器类型：只服务这些控制器的 pretask 不发出去。
    foreign_controllers: frozenset = frozenset()
    # 把 pretask 里的 pc_bootstrap.py 换成 -c 载荷（理由见 PRETASK_* 常量）。
    launch_pretask: bool = False
    # MFAA 的标题栏/窗口/托盘图标（见 set_runtime_icon）。安卓启动器图标在 pi-profile，不在这里。
    runtime_icon: bool = True


AGENT_MAIN = "{PROJECT_DIR}/agent/main.py"
PLATFORMS = {
    # -s：嵌入式 Python 的 ._pth 开着 import site，用户目录
    # %APPDATA%\Python\Python310\site-packages 会排在包内 site-packages 之前。
    # 用户自己 pip install --user 过 maafw/numpy 时，Agent 会静默用上那一份。
    "win": Platform("{PROJECT_DIR}/python/python.exe", ("-u", "-s", "-X", "utf8=1", AGENT_MAIN),
                    launch_pretask=True),
    # 便携 Python 没就位时退回系统 python3，见 set_agent_launch。Mac 通常不需要 -X utf8=1。
    "macos": Platform("{PROJECT_DIR}/python/bin/python3", ("-u", AGENT_MAIN), frozenset({"Win32"})),
    "linux": Platform("python3", ("-u", AGENT_MAIN), frozenset({"Win32"})),
    # APK 的 Agent 由 android/pi-profile.yaml 的 agent.runtimes 拉起，这里只需声明存在。
    "android": Platform("python3", ("-u", AGENT_MAIN), frozenset({"Win32"}), runtime_icon=False),
}
TARGET_PREFIXES = (
    ("win", ("win",)),
    ("macos", ("macos", "darwin", "osx")),
    ("linux", ("linux",)),
    ("android", ("android",)),
)


def release_target(raw: str) -> str:
    """CI 传 matrix.os 或 android；本地出包与测试也会带架构（win-x64）。认不出就报错，不猜。"""
    name = str(raw).lower()
    for platform, prefixes in TARGET_PREFIXES:
        if name.startswith(prefixes):
            return platform
    raise ValueError(f"Unknown release target {raw!r}; expected one of {sorted(PLATFORMS)}")


@dataclass(frozen=True)
class Target:
    platform: str
    version: str
    install: Path  # 产物根：agent 步骤要看便携 Python 在不在，校验要看 pretask 落点
    branding: Path = BRANDING
    # 终检重算一遍时不必再往日志与 step summary 里重复报一次图标与解释器的选择。
    announce: bool = True


def target_for(target_os: str, version: str = "v0.0.0", install: Path = ROOT / "install",
               branding: Path = BRANDING, announce: bool = True) -> Target:
    return Target(release_target(target_os), version, Path(install), Path(branding), announce)


# ── pretask 的启动路径 ─────────────────────────────────────────────────────────
# 协议规定 pretask 的 CWD 是 interface.json 所在目录，即安装根
# （deps/tools/interface.schema.json 的 pretaskConfig.exec）。但 MFAA v2.15.2 至少有
# 一条代码路径用的是 resource/base：线上实测中，点「刷新连接目标」时两级上跳能跑通并
# 写出 pc_bootstrap.log，而点「开始任务」时一行日志都没有。一个相对路径不可能同时满足
# 两个 CWD，这里必须选一个锚点——默认沿用唯一已知可用的那个，不拿「协议正确」去换掉它。
# 切换只需改这一个常量。
PRETASK_ANCHOR = "base"  # "base" = resource/base（实测可用）；"root" = 安装根（协议正确）
PRETASK_EXEC = {"base": "../../python/python.exe", "root": "./python/python.exe"}[PRETASK_ANCHOR]
# 脚本路径反过来可以做到与 CWD 无关：CreateProcess 一定把 exec 解析成绝对路径，于是
# sys.executable 必然是 <安装根>/python/python.exe，往上两级就是安装根。这样即便 CWD
# 与 exec 的解析锚点不一致（上面那两条路径的差异），脚本也一定找得到。
PRETASK_LAUNCH = (
    "import os,runpy,sys;d=os.path.dirname;"
    "r=d(d(os.path.realpath(sys.executable)));"
    "runpy.run_path(os.path.join(r,'agent','pc_bootstrap.py'),run_name='__main__')"
)
PRETASK_SCRIPT = "pc_bootstrap.py"


def pretask_anchor(install: Path) -> Path:
    return install if PRETASK_ANCHOR == "root" else install / "resource" / "base"


# ── 步骤 ──────────────────────────────────────────────────────────────────────
# 每一步只改内存里的 dict，签名统一为 (interface, target)。挪选项的两步与目标无关，
# target 可省，测试直接拿文档调用。

# The source keeps the multi-save switch in global_option: VS Code MaaSupport stores global
# values per task, so a single task run can pick its account, injected into the Agt_ node.
# MFAA shares global values across instances, so releases move the switch into the
# multi-save task (saved per instance) and drop the injection; the agent reads MFAA's file.
# 用户侧说明见 docs/multi-save.md。
MULTI_SAVE_ENTRY = "Env_MultiSave_Config"
MULTI_SAVE_SWITCH = "启用多存档"
MULTI_SAVE_OPTIONS = (MULTI_SAVE_SWITCH, "存档名称")
MULTI_SAVE_INJECT_NODE = "Agt_MultiSave_Inject"


def relocate_multi_save_switch(interface, target=None):
    """Move the global multi-save switch into its task, in place; release shapes pass unchanged."""
    tasks = [task for task in interface.get("task", []) if task.get("entry") == MULTI_SAVE_ENTRY]
    in_global = MULTI_SAVE_SWITCH in interface.get("global_option", [])
    if not tasks and not in_global:
        return
    if len(tasks) != 1:
        raise ValueError(f"Expected exactly one {MULTI_SAVE_ENTRY} task, found {len(tasks)}")
    task = tasks[0]
    if in_global:
        if task.get("option"):
            raise ValueError("The source multi-save task must not declare its own options")
        interface["global_option"] = [name for name in interface["global_option"] if name != MULTI_SAVE_SWITCH]
        task["option"] = [MULTI_SAVE_SWITCH]
    elif task.get("option") != [MULTI_SAVE_SWITCH]:
        raise ValueError(f"The multi-save task must carry only {MULTI_SAVE_SWITCH!r}")
    for name in MULTI_SAVE_OPTIONS:
        option = interface.get("option", {}).get(name)
        if option is None:
            raise ValueError(f"Missing multi-save option {name!r}")
        for holder in (option, *option.get("cases", [])):
            override = holder.get("pipeline_override")
            if isinstance(override, dict):
                override.pop(MULTI_SAVE_INJECT_NODE, None)
                if not override:
                    del holder["pipeline_override"]


# The source keeps the PC window options in global_option for VS Code MaaSupport. MFAA keeps one
# global value per instance whatever its controller, so releases move each option into the Win32
# controllers it names: MFAA then saves a value per controller and merges it into every task, and
# the startup sink re-reads it at each task start — a task-level option would reach only its task.
PC_WINDOW_OPTIONS = ("PC窗口分辨率", "PC启动最小化")


def relocate_pc_window_options(interface, target=None):
    """Move the PC window options from global_option into their controllers, in place.

    Runs before narrow_to_android: it checks the owners by name, and those are gone after narrowing.
    """
    controllers = {item.get("name"): item for item in interface.get("controller", [])}
    for name in PC_WINDOW_OPTIONS:
        option = interface.get("option", {}).get(name)
        if option is None:
            if name in interface.get("global_option", []):
                raise ValueError(f"Missing PC window option {name!r}")
            continue
        owners = option.get("controller") or []
        if not owners:
            raise ValueError(f"PC window option {name!r} must name its controllers")
        lists = []
        for owner in owners:
            controller = controllers.get(owner)
            if controller is None or controller.get("type") != "Win32":
                raise ValueError(f"PC window option {name!r} names a non-Win32 controller {owner!r}")
            lists.append(controller.setdefault("option", []))
        if name in interface.get("global_option", []):
            interface["global_option"] = [item for item in interface["global_option"] if item != name]
            for listed in lists:
                if name not in listed:
                    listed.append(name)
        elif not all(name in listed for listed in lists):
            raise ValueError(f"PC window option {name!r} is neither global nor on all its controllers")


ANDROID_NATIVE_LAYER = "./resource/android_native"
ANDROID_ACCOUNT_OPTIONS = MULTI_SAVE_OPTIONS


def narrow_to_android(interface, target):
    """Keep only what the APK can run: its Adb controller and what that controller can reach."""
    if target.platform != "android":
        return
    adb = next((item for item in interface.get("controller") or [] if item.get("type") == "Adb"), None)
    if adb is None:
        raise ValueError("Android target requires a controller with type=Adb")
    adb_name = adb.get("name")
    if not adb_name:
        raise ValueError("Android Adb controller requires a name")
    interface["controller"] = [adb]

    resources = []
    for resource in interface.get("resource") or []:
        allowed = resource.get("controller")
        if not allowed or adb_name in allowed:
            resources.append(resource)
    if not resources:
        raise ValueError("Android target has no resource compatible with the Adb controller")
    interface["resource"] = resources
    # Android runs base plus a minimal native-controller overlay, in that order.
    for resource in resources:
        # setdefault would happily build a resource whose only layer is the overlay —
        # a pack with no base under it loads, and then silently lacks every node.
        paths = resource.get("path")
        if not paths:
            raise ValueError(f"Android resource {resource.get('name')!r} has no base layer to overlay")
        if ANDROID_NATIVE_LAYER not in paths:
            paths.append(ANDROID_NATIVE_LAYER)
        if resource.get("name") == "ADB":
            resource["label"] = "安卓原生机"
    # Keep the normal task entries, but omit tasks for other controllers.
    interface["task"] = [task for task in interface.get("task", [])
                         if task.get("entry") != MULTI_SAVE_ENTRY
                         and (not task.get("controller") or adb_name in task["controller"])]
    # The APK has one local account; no MFAA instance settings or account override.
    interface["global_option"] = [name for name in interface.get("global_option", [])
                                  if name not in ANDROID_ACCOUNT_OPTIONS]
    for name in ANDROID_ACCOUNT_OPTIONS:
        interface.get("option", {}).pop(name, None)
    # Options that name only controllers the APK does not have (the PC window pair, which
    # relocate_pc_window_options hung on the Win32 controllers just removed) would be orphans.
    for name, option in list(interface.get("option", {}).items()):
        if option.get("controller") and adb_name not in option["controller"]:
            del interface["option"][name]
    task_names = {task["name"] for task in interface["task"]}
    for preset in interface.get("preset", []):
        preset["task"] = [task for task in preset.get("task", []) if task.get("name") in task_names]
    # Left here the RID would reach both Android identities. android_build.py pins it in the
    # PI profile for the released identity only (apply_identity_to_profile).
    interface.pop("mirrorchyan_rid", None)


def shape_pretask(interface, target):
    """One rule for every target: ship a pretask only if a controller it serves can run here.

    Unrestricted pretasks always ship. On Windows the bootstrap script additionally becomes a
    -c payload, since pretask exec/args get no {PROJECT_DIR} expansion (the protocol has none).
    """
    if "pretask" not in interface:
        return
    platform = PLATFORMS[target.platform]
    runnable = {item.get("name") for item in interface.get("controller", [])
                if item.get("type") not in platform.foreign_controllers}
    interface["pretask"] = [pretask for pretask in interface["pretask"]
                            if not pretask.get("controller") or runnable & set(pretask["controller"])]
    if not platform.launch_pretask:
        return
    for pretask in interface["pretask"]:
        args = pretask.get("args", [])
        script_indices = [index for index, arg in enumerate(args)
                          if Path(arg.replace("\\", "/")).name == PRETASK_SCRIPT]
        if script_indices:
            pretask["exec"] = PRETASK_EXEC
            # 倒序替换：一项换两项会移动后面的下标。
            for index in reversed(script_indices):
                args[index:index + 1] = ["-c", PRETASK_LAUNCH]


def set_agent_launch(interface, target):
    """The source points at the developer's .venv (kept on purpose, see 发布与CI.md §2.1)."""
    platform = PLATFORMS[target.platform]
    executable = platform.agent_exec
    if target.platform == "macos":
        # 有嵌入用嵌入，没嵌入用系统。CI 的便携 Python 步骤排在 install.py 之前。
        portable = (target.install / "python" / "bin" / "python3").exists()
        if not portable:
            executable = "python3"
        if target.announce:
            print("[macOS] 检测到便携版 Python，已启用独立环境模式。" if portable
                  else "[macOS] 未检测到便携版 Python，回退到系统 python3。")
    agent = interface.setdefault("agent", {})
    agent["child_exec"] = executable
    agent["child_args"] = list(platform.agent_args)


# MFAA shares one bitmap between its title, window and tray icons (release/branding/README.md).
# resource/ui/ is not in any resource's loading list, so the image is UI only.
RUNTIME_ICON = "resource/ui/title.png"
RUNTIME_ICON_SOURCE = "title.png"


def set_runtime_icon(interface, target):
    """Optional branding: art that is missing or malformed warns and keeps the previous icon.

    Only the art is optional. The image itself is written by install() together with this
    file, image first, so no interface.json ever points at a missing or partial image.
    """
    if not PLATFORMS[target.platform].runtime_icon:
        return
    try:
        validate_png(target.branding / RUNTIME_ICON_SOURCE)
    except Exception as error:
        if target.announce:
            report("MFAA runtime", error)
        return
    interface["icon"] = RUNTIME_ICON
    if target.announce:
        report("MFAA runtime")


def package_files(interface: dict, target: Target) -> dict:
    """Files that ship beside interface.json because it points at them."""
    if PLATFORMS[target.platform].runtime_icon and interface.get("icon") == RUNTIME_ICON:
        return {RUNTIME_ICON: validate_png(target.branding / RUNTIME_ICON_SOURCE)}
    return {}


# The version shown between "...(sunyink/MFABD2)" and " | 游戏版本：". The space before the
# bar stays outside the match, so the replacement is the bare version.
TITLE_VERSION = re.compile(r"(?<=MFABD2\))(.*?)(?=\s*\|\s*游戏版本：)")


def stamp_version(interface, target):
    """Stamp the package version into "version" and the title.

    A resource hot update installs the package's copy of this file, then MFAA rewrites only
    "version" and "github" (MFAAvalonia VersionChecker.UpdateResourceCore), so the title keeps
    this stamp.
    """
    interface["version"] = target.version
    if "title" in interface:
        title, hits = TITLE_VERSION.subn(target.version, interface["title"], count=1)
        # A reworded title used to keep the source's stale version without a word.
        if not hits:
            raise ValueError(f"title has no version slot between 'MFABD2)' and '| 游戏版本：': {interface['title']!r}")
        interface["title"] = title


# 顺序即执行顺序：先挪选项（与平台无关，且要在裁剪前按名字核对 Win32 控制器），再按目标
# 裁剪，然后定启动方式（要看裁剪后还剩哪些控制器），最后是图标与版本号。
STEPS = (
    relocate_multi_save_switch,
    relocate_pc_window_options,
    narrow_to_android,
    shape_pretask,
    set_agent_launch,
    set_runtime_icon,
    stamp_version,
)


# ── 读、算、写、查 ─────────────────────────────────────────────────────────────

def load_source(path: Path = SOURCE) -> dict:
    """Read as JSONC, matching the json-with-comments loader install.py used to rely on."""
    text = Path(path).read_text(encoding="utf-8-sig")
    return json.loads(strip_trailing_commas(strip_jsonc_comments(text)), strict=False)


def build(source: dict, target: Target) -> dict:
    """Return the release interface for target. Never mutates source and writes nothing."""
    interface = deepcopy(source)
    for step in STEPS:
        step(interface, target)
    return interface


def dumps(interface: dict) -> str:
    return json.dumps(interface, ensure_ascii=False, indent=4) + "\n"


def atomic_write(path: Path, data: bytes) -> None:
    """Replace path in one rename, so a crash never leaves half a file in the package.

    NamedTemporaryFile creates 0600; give the result the usual 0644 before it takes the name.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


ANDROID_BASE_LAYER = "./resource/base"
# What the pinned MaaFwApp parses; it silently drops options of any other (or no) type.
ANDROID_OPTION_TYPES = {"select", "switch", "checkbox", "input"}


def dev_paths(value, where="interface"):
    """Yield (location, text) for every string that still points into a developer .venv."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from dev_paths(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            name = item.get("name") if isinstance(item, dict) else None
            yield from dev_paths(item, f"{where}[{name!r}]" if name else f"{where}[{index}]")
    elif isinstance(value, str) and ".venv" in value.lower():
        yield where, value


def option_references(interface: dict) -> list:
    """(owner, option name) for every place that names an option."""
    references = [("global_option", name) for name in interface.get("global_option", [])]
    for section in ("task", "resource", "controller"):
        for item in interface.get(section, []):
            references += [(f"{section}:{item.get('name')}", name) for name in item.get("option", [])]
    for option_name, option in interface.get("option", {}).items():
        for case in option.get("cases", []):
            references += [(f"option:{option_name}/{case.get('name')}", name) for name in case.get("option", [])]
    for preset in interface.get("preset", []):
        for task in preset.get("task", []):
            references += [(f"preset:{preset.get('name')}/{task.get('name')}", name) for name in task.get("option", {})]
    return references


def check_options(interface: dict, android: bool = False) -> list:
    """Every named option must exist; returns the options nothing names (orphans)."""
    options = interface.get("option", {})
    if android:
        invalid = [name for name, value in options.items() if value.get("type") not in ANDROID_OPTION_TYPES]
        if invalid:
            raise ValueError(f"Android UI cannot parse option types: {invalid}")
    references = option_references(interface)
    missing = [(owner, name) for owner, name in references if name not in options]
    if missing:
        raise ValueError(f"Option references are missing: {missing}")
    tasks = {task.get("name") for task in interface.get("task", [])}
    lost = [(preset.get("name"), task.get("name")) for preset in interface.get("preset", [])
            for task in preset.get("task", []) if task.get("name") not in tasks]
    if lost:
        raise ValueError(f"Presets name tasks that do not exist: {lost}")
    return sorted(set(options) - {name for _, name in references})


def verify(written: dict, target: Target, on_disk: bool = True) -> None:
    """The release contract, on the file as packaged (or as found inside the APK).

    on_disk=False skips what needs the package directory around the file. target.version
    None skips the version checks (an APK checked without its build metadata).
    """
    platform = PLATFORMS[target.platform]
    # install.py once only printed when writing failed: the package kept the repository's
    # child_exec (the developer .venv), every platform's Agent failed to start, and no later
    # step noticed. Any .venv string anywhere is a development path that leaked.
    for where, text in dev_paths(written):
        raise ValueError(f"{where} 仍指向开发环境虚拟环境: {text}")

    if target.version is not None:
        if written.get("version") != target.version:
            raise ValueError(f"version is {written.get('version')!r}, expected {target.version!r}")
        match = TITLE_VERSION.search(written.get("title", ""))
        if "title" in written and (not match or match.group(1) != target.version):
            raise ValueError(f"title does not show {target.version!r}: {written['title']!r}")

    agent = written.get("agent") or {}
    if not agent:
        raise ValueError("interface.json does not declare its required agent")
    allowed = {platform.agent_exec}
    if target.platform == "macos" and not (on_disk and (target.install / "python" / "bin" / "python3").exists()):
        allowed = {"python3"} if on_disk else allowed | {"python3"}
    if agent.get("child_exec") not in allowed or agent.get("child_args") != list(platform.agent_args):
        raise ValueError(f"agent {agent!r} does not match the {target.platform} launch")

    runnable = {item.get("name") for item in written.get("controller", [])
                if item.get("type") not in platform.foreign_controllers}
    for pretask in written.get("pretask", []):
        name = pretask.get("name", "<unnamed>")
        if pretask.get("controller") and not runnable & set(pretask["controller"]):
            raise ValueError(f"pretask {name!r} serves no controller that runs on {target.platform}")
        scripts = [arg for arg in pretask.get("args", []) if Path(arg.replace("\\", "/")).name == PRETASK_SCRIPT]
        if platform.launch_pretask and scripts:
            raise ValueError(f"pretask {name!r} still launches {scripts[0]} by a relative path")
        # pretask 路径存在性：悬空的 exec 会让 pretask 静默不执行——上层软件不报错，
        # 运行日志里连一条警告都没有。这正是线上踩过的坑，必须在构建期就发现。
        if not on_disk or PRETASK_SCRIPT not in "".join(pretask.get("args", [])):
            continue
        install = target.install
        if not (install / "agent" / PRETASK_SCRIPT).is_file():
            raise ValueError(f"{install / 'agent' / PRETASK_SCRIPT} 缺失，pretask 必然起不来")
        resolved = (pretask_anchor(install) / pretask.get("exec", "")).resolve()
        if not (install / "python").is_dir():
            # 本地手动出包时还没解开嵌入式解释器，只提示不中止；CI 里它总是先就绪。
            if target.announce:
                print(f"⚠️ 跳过 pretask exec 存在性检查（{install / 'python'} 尚未就绪）：{resolved}")
        elif not resolved.is_file():
            raise ValueError(f"pretask exec 在 {PRETASK_ANCHOR} 锚点下不存在: {resolved}")

    # Unreferenced options are harmless to the clients, so they warn (once, at install time).
    orphans = check_options(written, android=target.platform == "android")
    if orphans and target.announce:
        print(f"::warning title=Release interface::Options nothing refers to: {orphans}")

    if target.platform == "android":
        controllers = written.get("controller", [])
        if len(controllers) != 1 or controllers[0].get("type") != "Adb":
            raise ValueError("Android payload must expose only its Adb controller declaration")
        resources = written.get("resource", [])
        if not resources or any(resource.get("controller") and controllers[0]["name"] not in resource["controller"]
                                for resource in resources):
            raise ValueError("Android payload contains incompatible resource choices")
        if any(resource.get("path", [])[-1:] != [ANDROID_NATIVE_LAYER] for resource in resources):
            raise ValueError("Android native overlay is not the last resource layer")
        # An overlay with no base under it still loads, and then has almost no nodes.
        if any(resource.get("path", [])[:1] != [ANDROID_BASE_LAYER] for resource in resources):
            raise ValueError("Android payload lost its base resource layer")
        # The released identity gets its RID from the PI profile; one here would also
        # point CI builds at MirrorChyan, which only ever serves the released APK.
        if "mirrorchyan_rid" in written:
            raise ValueError("Android payload must not advertise a MirrorChyan RID")

    if not platform.runtime_icon and written.get("icon") == RUNTIME_ICON:
        raise ValueError(f"{target.platform} does not ship {RUNTIME_ICON}")
    if on_disk:
        for relative, data in package_files(written, target).items():
            if not (target.install / relative).is_file() or (target.install / relative).read_bytes() != data:
                raise ValueError(f"interface.json points at {relative}, which is missing or is not the branding image")


def check_installed(written: dict, expected: dict, target: Target) -> None:
    """Read-back right after writing: the file must hold what was built, and keep the contract."""
    if written != expected:
        raise ValueError("interface.json 回读不符: 写入后的内容与本次配置不一致")
    verify(written, target)


def write_package(install_dir: Path, interface: dict, files: dict) -> None:
    """Write the files interface.json points at first, then interface.json itself.

    If interface.json cannot be written, put back what those files replaced, so the
    package is left exactly as it was.
    """
    previous = {}
    for relative, data in files.items():
        path = install_dir / relative
        previous[path] = path.read_bytes() if path.exists() else None
        atomic_write(path, data)
    try:
        atomic_write(install_dir / "interface.json", dumps(interface).encode("utf-8"))
    except Exception:
        for path, data in previous.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, data)
        raise


def install(install_dir: Path, target_os: str, version: str, source: Path = SOURCE,
            branding: Path = BRANDING) -> dict:
    """Build, write once, read back. Call after resources and agent/ are in install_dir."""
    install_dir = Path(install_dir)
    target = target_for(target_os, version, install_dir, branding)
    interface = build(load_source(source), target)
    write_package(install_dir, interface, package_files(interface, target))
    path = install_dir / "interface.json"
    written = json.loads(path.read_text(encoding="utf-8"))
    check_installed(written, interface, target)
    print(f"✅ interface.json 已按 {target.platform} 生成: child_exec={written['agent']['child_exec']}")
    return written


def verify_package(install_dir: Path, target_os: str, version: str, source: Path = SOURCE,
                   branding: Path = BRANDING) -> dict:
    """The last gate before upload, run as its own CI step after everything else touched install/.

    The packaged file must still equal a fresh build from the source: no later step (MFAA
    merge, comment stripping, icons, ...) may change or overwrite it. Then the contract again.
    """
    install_dir = Path(install_dir)
    target = target_for(target_os, version, install_dir, branding, announce=False)
    written = json.loads((install_dir / "interface.json").read_text(encoding="utf-8"))
    expected = build(load_source(source), target)
    if written != expected:
        keys = sorted(key for key in set(written) | set(expected) if written.get(key) != expected.get(key))
        raise ValueError(f"interface.json 与按源文件重算的结果不一致（字段 {keys}）：install.py 之后有步骤"
                         f"改动或覆盖了它，或者这里的 --target/--version 与传给 install.py 的不同")
    verify(written, target)
    return written


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Release interface.json (scripts/release_interface.py).")
    commands = parser.add_subparsers(dest="command", required=True)
    gate = commands.add_parser("verify", help="last gate before upload (see verify_package)")
    gate.add_argument("install", type=Path, help="package directory holding interface.json")
    gate.add_argument("--target", required=True, help="matrix.os or android")
    gate.add_argument("--version", required=True, help="the tag this package is built for")
    args = parser.parse_args(argv)
    try:
        verify_package(args.install, args.target, args.version)
    except Exception as error:
        print(f"::error title=Release interface::{error}")
        raise SystemExit(1) from error
    print(f"✅ {args.install / 'interface.json'} 符合 {args.target} 的发布约定")


if __name__ == "__main__":
    main()
