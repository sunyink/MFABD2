# -*- coding: utf-8 -*-

import os
import sys
import platform
import threading
from pathlib import Path

# =========================================================================
# [配置] 调试开关
# =========================================================================
# True  : 开启虚拟环境自动检查与接管 (默认)
# False : 强制关闭虚拟化逻辑 (用于排查环境问题，或手动管理环境时)
ENABLE_VENV_AUTO_CHECK = True
# 宿主(UI)存活守护的轮询周期(秒)。
# 注意这不是响应延迟：父进程退出会让 WaitForSingleObject 立即返回，是即时感知的。
# 该值只决定「消息循环已正常结束」这件事最迟多久被发现。
WATCHDOG_POLL_SECONDS = 3.0
# =========================================================================
# [新增] 强制全链路 UTF-8 (解决 Windows 命令行/pip 读取中文报错问题)
# PYTHONUTF8=1 : 让 Python 3.7+ 忽略系统区域设置，强制使用 UTF-8 (PEP 540)
# PYTHONIOENCODING : 强制标准输入输出流的编码
# =========================================================================
os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"

# --- 添加依赖库路径 ---
current_file_path = Path(__file__).resolve()
project_root = current_file_path.parent.parent  # 指向 install/ 目录
deps_path = project_root / "agent"

# 将 agent 目录加入 python 搜索路径
if deps_path.exists():
    sys.path.insert(0, str(deps_path))

from utils import mfaalog # 日志
from utils import venv_ops # 虚拟化

# 环境治理逻辑 (虚拟化 + 模式判断)
# -----------------------------
def get_env_mode():
    """
    判断当前运行模式
    返回: 'dev' (开发/源码) 或 'release' (发布)
    判据: requirements.txt 是否存在
    """
    req_file = project_root / "requirements.txt"
    if req_file.exists():
        return 'dev'
    return 'release'

# 获取当前模式
current_mode = get_env_mode()
# 虚拟环境接管逻辑 (仅在开发模式且非内嵌环境时触发)
# 这里保留我们之前讨论的逻辑
if current_mode == 'dev':
    # 1. 优先检查手动开关
    if not ENABLE_VENV_AUTO_CHECK:
        mfaalog.warning("⚠️ [调试模式] 虚拟环境自动接管已通过开关禁用 (ENABLE_VENV_AUTO_CHECK=False)")
    
    else:
        # 2. 正常逻辑：检查是不是 Windows 内嵌 Python 防止误判
        is_embedded = False
        if sys.platform == "win32":
            try:
                if project_root in Path(sys.executable).resolve().parents:
                    is_embedded = True
            except:
                pass
        
        if not is_embedded:
            mfaalog.info("开发模式: 启动虚拟环境管理...")
            venv_ops.ensure_venv(project_root)

# -----------------------------
# 1. 动态计算 RID (Runtime Identifier)
system_name = platform.system().lower()  # 'windows', 'linux', 'darwin'
proc_arch = platform.machine().lower()   # 'amd64', 'x86_64', 'aarch64', 'arm64'

# [修复2] 必须给 rid 一个默认值，或者补全 Windows 分支
rid = "win-x64" # 默认值，防崩溃

if system_name == 'windows':
    if 'arm64' in proc_arch:
        rid = "win-arm64"
    else:
        rid = "win-x64"
elif system_name == 'linux':
    rid = "linux-arm64" if 'aarch64' in proc_arch else "linux-x64"
elif system_name == 'darwin':
    rid = "osx-arm64" if 'arm64' in proc_arch else "osx-x64"

# 2. 拼接 Native 库路径
dll_path = project_root / "runtimes" / rid / "native"

if current_mode == 'release':
    # 【发布模式】：必须手动指定 DLL 路径
    # 因为发布包里没有 pip 安装库，只有 runtimes 文件夹里的裸 DLL
    dll_path = project_root / "runtimes" / rid / "native"
    mfaalog.info(f"发布模式: 强制注入 DLL 路径 -> {dll_path}")
    
    os.environ["MAAFW_BINARY_PATH"] = str(dll_path)
    if system_name == 'windows':
        os.environ["PATH"] = str(dll_path) + os.pathsep + os.environ["PATH"]

else:
    # 【开发模式】：绝对不要乱指路！
    # 开发环境下，Python 会自动去 venv/site-packages 里找 pip 安装好的最新 DLL
    # 如果这里强行指向 runtimes，就会导致"代码是新的，DLL 是旧的"版本冲突
    mfaalog.info("开发模式: 跳过 DLL 路径注入 (使用 Python 库自带 DLL)  | agent//utils//venv_ops.py的maafw版本需要手动指定与agent一致")

from maa.agent.agent_server import AgentServer
from maa.toolkit import Toolkit

# 如果你有自定义动作/识别，在这里导入
import action # action子文件夹:agent/action/__init__.py里声明的全部
import recognition
from utils.persistent_store import PersistentStore # Agent配置文件热备份
from utils.instance_resolver import resolve_account_id  # [新增] 实例探测器
from utils.host_watchdog import HostWatchdog, cleanup_socket_file  # 宿主(UI)存活守护
import fishing_agent # 钓鱼~

def main():
    # 设置 stdout 为 utf-8 (防止中文乱码)
    if sys.version_info >= (3, 7):
        sys.stdout.reconfigure(encoding='utf-8') # type: ignore

    print(f"Agent 正在启动... 根目录: {project_root}")

    # =========================================================================
    # [核心变更] 启动时一次性解析实例与存档号
    # =========================================================================
    # 获取 socket_id (由 MaaFramework 传入)
    socket_id = sys.argv[-1] if len(sys.argv) >= 2 else ""
    # 去除可能的 "socket_id=" 前缀
    if socket_id.startswith("socket_id="):
        socket_id = socket_id.split("=", 1)[1]

    try:
        if socket_id:
            account_id = resolve_account_id(socket_id, project_root)
            if account_id != "0":
                PersistentStore.switch_account(account_id)
        
        PersistentStore.load() 
        mfaalog.info("✅ [Agent] 存档/备份系统已就绪")
    except Exception as e:
        mfaalog.error(f"⚠️ 存档解析或加载发生异常，降级使用默认存档 '0': {e}")
        PersistentStore.switch_account("0")
        PersistentStore.load()

    # 1. 初始化 Toolkit (借鉴 B 项目)
    # AgentServer 模式下仅 set_log_dir 生效，其余被忽略（上游已知行为）
    # 注：路径含非 ASCII 字符时，底层 C++ DLL 会在抛出 OSError 前先创建乱码目录
    # 因此提前检测，非 ASCII 路径直接跳过，避免副作用
    project_root_str = str(project_root)
    if project_root_str.isascii():
        try:
            Toolkit.init_option(project_root_str)
        except OSError as e:
            mfaalog.warning(f"Toolkit.init_option 调用失败，已跳过日志目录设置: {e}")
    else:
        mfaalog.warning("路径含非ASCII字符，已跳过 Toolkit.init_option（避免生成乱码目录）")

    # 2. 获取 socket_id (由 MaaFramework 传入)
    if not socket_id:
        print("错误: 未收到有效的 socket_id 参数，请勿直接运行此脚本，需由 MAA 启动。")
        return
    
    # [调整] socket_id 已在上方提取，此处直接使用
    print(f"Socket ID: {socket_id}")

    # 3. 启动服务
    # start_up 返回 bool。失败时若继续走 join()，C++ 侧会打一条
    # "msg_thread is not joinable" 然后立刻返回，进程静默退出 —— 必须在这里拦下。
    if not AgentServer.start_up(socket_id):
        mfaalog.error(f"❌ AgentServer 启动失败 (socket_id={socket_id})，Agent 退出")
        return

    mfaalog.info("AgentServer 已启动，等待指令...")

    watchdog = HostWatchdog()
    if not watchdog.available:
        # 拿不到父进程句柄时退化为原来的阻塞等待，行为不比改动前差。
        mfaalog.warning("[Agent] 宿主守护不可用，回退为阻塞等待（UI 异常退出时本进程可能残留）")
        # 这里**不要**照下面主循环那样补 `except KeyboardInterrupt: os._exit()`。
        # AgentServer.join() 是 ctypes 调用(MaaAgentServerJoin)，主线程一旦进去就不再
        # 执行字节码；Ctrl+C 时信号处理器只能设个标志，异常要等 C 调用返回才兑现，
        # 而它永不返回 —— 那个 handler 到不了，纯死代码。下面主循环则不同：主线程在跑
        # Python while，host_exited() 最多阻塞一个轮询周期就交回控制权，中断能兑现。
        try:
            AgentServer.join()
        except Exception as e:
            mfaalog.error(f"Agent 运行发生异常: {e}")
        finally:
            AgentServer.shut_down()
            mfaalog.info("AgentServer 已关闭")
        return

    # 这里必须打出实际监视的目标：上一版只打 getppid()，把 venv launcher 说成了 UI，
    # 排查时正是靠这条日志与进程树对不上才发现守护失效的。别再让它说谎。
    mfaalog.info(f"[Agent] 宿主守护已启动，监视 {watchdog.describe()}")

    # 把阻塞的 join 挪到后台线程，主线程腾出来守护宿主。
    # 用 join 而非 detach：msg_thread_ 仍保持 joinable，正常关闭时 shut_down()
    # 能干净收尾；而且 join 返回本身就是「消息循环已正常结束」的信号。
    loop_ended = threading.Event()

    def _serve():
        try:
            AgentServer.join()
        except Exception as e:
            mfaalog.error(f"Agent 运行发生异常: {e}")
        finally:
            loop_ended.set()

    threading.Thread(target=_serve, name="AgentServerJoin", daemon=True).start()

    try:
        while not loop_ended.is_set():
            if watchdog.host_exited(WATCHDOG_POLL_SECONDS):
                # 宿主 UI 已消失。此刻 msg_thread 仍永久阻塞在 recv() 上
                # (Transceiver 的 timeout_ 是 milliseconds::max())，调 shut_down()
                # 会挂死在它内部的 join() 里，所以只能硬退出。
                gone = watchdog.exited_target or f"pid={watchdog.ppid}"
                mfaalog.error(f"[Agent] 宿主进程 {gone} 已退出，Agent 立即终止")
                watchdog.close()
                cleanup_socket_file(socket_id)
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(1)
    except KeyboardInterrupt:
        # 与「宿主已退出」同一个死锁：能中断到这里，msg_thread 几乎必然还卡在 recv()，
        # 落到下面的 shut_down() 就会挂在它的 join 里，Ctrl+C 反而按不出去。只能硬退。
        # （极小概率是 loop_ended 刚置位的竞态，那种情况下硬退只少打一行收尾日志。）
        mfaalog.info("[Agent] 收到中断信号，正在退出")
        watchdog.close()
        cleanup_socket_file(socket_id)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(130)
    finally:
        watchdog.close()

    # 走到这里说明消息循环已正常结束（UI 下发了 ShutDownRequest），
    # msg_thread 已退出，shut_down() 的 join 会立即返回。
    AgentServer.shut_down()
    mfaalog.info("AgentServer 已关闭")

if __name__ == "__main__":
    main()
