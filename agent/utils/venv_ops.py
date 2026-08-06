import os
import sys
import subprocess
import site
import shutil
import hashlib
from pathlib import Path
from . import mfaalog  # 日志工具

# =========================================================
# [配置区] 环境与依赖设定
# =========================================================
# MAAFW的py库版本需要手动指定,与C++库版号一致。
DEV_MAAFW_VERSION = "5.11.1" 
# 精确安装失败时的回退范围。更新上方版本时，请务必同步更新此处！
FALLBACK_MAAFW_SPEC = ">=5.11,<6.1"
VENV_NAME = ".venv"
PREFERRED_PYTHON_VERSION = "3.10"
# =========================================================

def get_venv_path(project_root: Path) -> Path:
    return project_root / VENV_NAME

def is_running_in_venv() -> bool:
    return sys.prefix != sys.base_prefix

def get_venv_executable(venv_path: Path) -> Path:
    if sys.platform == "win32":
        return venv_path / "Scripts" / "python.exe"
    else:
        return venv_path / "bin" / "python"

def check_existing_venv_version(venv_path: Path):
    """[新增] 读取现有虚拟环境的配置文件，检查版本是否匹配"""
    cfg_path = venv_path / "pyvenv.cfg"
    if not cfg_path.exists():
        return
    
    try:
        with open(cfg_path, 'r', encoding='utf-8') as f:
            for line in f:
                if line.startswith('version'):
                    # 提取版本号，如 "version = 3.11.4" -> "3.11.4"
                    ver = line.split('=')[1].strip()
                    if not ver.startswith(PREFERRED_PYTHON_VERSION):
                        mfaalog.warning(f"⚠️ [环境提示] 当前虚拟环境版本为 {ver}，与首选版本 {PREFERRED_PYTHON_VERSION} 不匹配！")
                        mfaalog.warning("💡 如果遇到兼容性问题，建议删除 .venv 文件夹让程序重新创建。")
                    break
    except Exception as e:
        mfaalog.debug(f"读取 pyvenv.cfg 失败: {e}")

def find_preferred_python() -> str:
    current_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    
    if current_version != PREFERRED_PYTHON_VERSION:
        # 明确指出这是宿主环境，避免与虚拟环境混淆
        mfaalog.info(f"💡 [环境提示] 当前外部终端使用的宿主 Python 版本 ({current_version}) 与首选开发版本 ({PREFERRED_PYTHON_VERSION}) 不一致。")
        mfaalog.info(f"正在系统中寻找 Python {PREFERRED_PYTHON_VERSION} 以为您克隆标准的虚拟隔离环境...")
        
    if sys.platform == "win32":
        try:
            cmd = ["py", f"-{PREFERRED_PYTHON_VERSION}", "-c", "import sys; print(sys.executable)"]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True) # nosec
            return result.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            mfaalog.warning(f"⚠️ 未能通过 py 启动器找到 Python {PREFERRED_PYTHON_VERSION}，将回退使用 ({current_version})。")
    else:
        target_cmd = f"python{PREFERRED_PYTHON_VERSION}"
        path = shutil.which(target_cmd)
        if path:
            return path
        mfaalog.warning(f"⚠️ 未在环境变量中找到 {target_cmd}，将回退使用 ({current_version})。")
            
    return sys.executable
def create_venv(venv_path: Path):
    if venv_path.exists() and (venv_path / "pyvenv.cfg").exists():
        mfaalog.debug("虚拟环境已存在，跳过创建。")
        check_existing_venv_version(venv_path) # [修复] 在跳过创建时，补充版本检查提示
        return

    mfaalog.info(f"正在创建虚拟环境: {venv_path} ...")
    base_python = find_preferred_python()
    mfaalog.info(f"🔧 使用基础解释器: {base_python}")
    
    try:
        subprocess.check_call([base_python, "-m", "venv", str(venv_path)]) # nosec
        mfaalog.info("✅ 虚拟环境创建成功")
    except subprocess.CalledProcessError as e:
        mfaalog.error(f"❌ 创建失败: {e}")
        raise

def get_deps_hash(project_root: Path) -> str:
    """[新增] 计算依赖项的 MD5 指纹"""
    req_file = project_root / "requirements.txt"
    # 将框架版本和 requirements 的内容拼接在一起计算
    content = f"MAAFW:{DEV_MAAFW_VERSION}\n"
    if req_file.exists():
        content += req_file.read_text(encoding='utf-8')
    return hashlib.md5(content.encode('utf-8')).hexdigest()

def install_deps(venv_python: Path, project_root: Path, venv_path: Path):
    """安装依赖 (带指纹缓存与核心库完整性双重校验)"""
    marker_file = venv_path / ".deps_marker"
    current_hash = get_deps_hash(project_root)

    # --- 1. 检查缓存，如果一致就跳出 (极速启动的核心) ---
    if marker_file.exists():
        if marker_file.read_text(encoding='utf-8').strip() == current_hash:
            try:
                # 仅做毫秒级探测，绝不执行 pip install
                subprocess.check_call( # nosec
                    [str(venv_python), "-c", "import maa"], 
                    stdout=subprocess.DEVNULL, 
                    stderr=subprocess.DEVNULL
                )
                mfaalog.info("⚡ 依赖库指纹一致且核心库完整，跳过 pip 安装步骤。")
                return  # 👈 必须有这个 return，程序才会在这里完美刹车！
            except subprocess.CalledProcessError:
                mfaalog.warning("⚠️ 发现缓存指纹匹配，但核心库 (maa) 丢失！准备重新安装...")

    # --- 2. 如果没有缓存，或者核心库损坏，开始拉取依赖 ---
    mfaalog.info("📦 依赖配置有更新或未安装，开始拉取依赖...")
    
    req_file = project_root / "requirements.txt"
    if req_file.exists():
        subprocess.check_call([ # nosec
            str(venv_python), "-m", "pip", "install", 
            "-i", "https://pypi.tuna.tsinghua.edu.cn/simple", 
            "-r", str(req_file)
        ])
    
    # --- 3. 安装框架，完美引用顶部的变量 ---
    try:
        subprocess.check_call([ # nosec
            str(venv_python), "-m", "pip", "install",
            "-i", "https://pypi.tuna.tsinghua.edu.cn/simple",
            f"maafw=={DEV_MAAFW_VERSION}"
        ])
    except subprocess.CalledProcessError as e:
        mfaalog.warning(f"指定版本 {DEV_MAAFW_VERSION} 安装失败: {e}，将使用备用范围 ({FALLBACK_MAAFW_SPEC}) 尝试重新安装...")
        subprocess.check_call([ # nosec
            str(venv_python), "-m", "pip", "install",
            "-i", "https://pypi.tuna.tsinghua.edu.cn/simple",
            f"maafw{FALLBACK_MAAFW_SPEC}"
        ])
    
    # --- 4. 全部安装成功后，写入新缓存 ---
    marker_file.write_text(current_hash, encoding='utf-8')
    mfaalog.info("✅ 依赖安装完成并已更新指纹缓存。")

def ensure_venv(project_root: Path):
    if is_running_in_venv():
        mfaalog.debug("当前已在虚拟环境中运行")
        return

    venv_path = get_venv_path(project_root)
    venv_python = get_venv_executable(venv_path)

    create_venv(venv_path)
    # [修改] 传入 venv_path 以便读写 marker 文件
    install_deps(venv_python, project_root, venv_path)

    if sys.platform == "win32":
        # Windows 没有 exec 语义。原先用 subprocess.run + sys.exit(0) 重启，会留下
        # 父进程空转等待，形成「宿主 python(A) → venv python(B)」双层进程：
        #   · 两者在任务管理器里同名，kill 哪个都不确定；
        #   · 杀 A 则 B 变孤儿，继续持有 AgentServer 的 socket 不放手。
        # 故此处不再自动重启，改为硬失败并给出可直接粘贴的配置。
        # 环境本身已在上面备妥，开发者改一次启动配置即可，不需要每次重配。
        # 对照上游 M9A agent/bootstrap.py:56-57，其 relaunch 同样只在 Linux 生效。
        main_py = (project_root / "agent" / "main.py").as_posix()
        mfaalog.error("=" * 64)
        mfaalog.error("[venv] Agent 需要用虚拟环境的解释器启动，当前用的是宿主解释器。")
        mfaalog.error(f"[venv] 虚拟环境已就绪: {venv_python}")
        mfaalog.error("[venv] 请把 interface.json 的 agent 段改为:")
        mfaalog.error(f'[venv]     "child_exec": "{venv_python.as_posix()}",')
        mfaalog.error(f'[venv]     "child_args": ["-u", "-X", "utf8=1", "{main_py}"]')
        mfaalog.error("[venv] 或直接用该解释器运行本脚本。")
        mfaalog.error("=" * 64)
        raise SystemExit(1)
    else:
        mfaalog.info(f"正在切换到虚拟环境: {venv_python}")
        mfaalog.info(">>> 重启 Agent 进程 >>>")
        args = [str(venv_python), sys.argv[0]] + sys.argv[1:]
        os.execv(str(venv_python), args)