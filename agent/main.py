# -*- coding: utf-8 -*-

import os
import sys
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
from utils.runtime_environment import RuntimeConfig

# Resolve platform differences once, before importing the native binding.
runtime = RuntimeConfig.detect(project_root, enable_venv_auto_check=ENABLE_VENV_AUTO_CHECK)
runtime.prepare()

from maa.agent.agent_server import AgentServer
from maa.toolkit import Toolkit

# 如果你有自定义动作/识别，在这里导入
from utils.persistent_store import PersistentStore
PersistentStore.configure_storage(runtime.storage)

import action # action子文件夹:agent/action/__init__.py里声明的全部
import recognition
from utils.instance_resolver import resolve_instance_id  # 实例身份探测(仅日志)
from utils.host_watchdog import HostWatchdog, cleanup_socket_file  # 宿主(UI)存活守护
from utils.log_event_sink import LogEventSink
import fishing_agent # 钓鱼~

def main():
    # 设置 stdout 为 utf-8 (防止中文乱码)
    if sys.version_info >= (3, 7):
        sys.stdout.reconfigure(encoding='utf-8') # type: ignore

    print(f"Agent 正在启动... 根目录: {project_root}")

    # =========================================================================
    # 启动时的实例探测与存档系统预热
    # =========================================================================
    # 存档号**不在这里决定**。启动阶段拿不到 context，也就拿不到用户当前选的
    # 存档号 —— 它由 utils/account_sync.py 在首个 custom 回调里从 context 读出
    # 并切换（见该模块 docstring）。这里只用默认档预热一次，验证路径与读写权限。
    #
    # 获取 socket_id (由 MaaFramework 传入)
    socket_id = sys.argv[-1] if len(sys.argv) >= 2 else ""
    # 去除可能的 "socket_id=" 前缀
    if socket_id.startswith("socket_id="):
        socket_id = socket_id.split("=", 1)[1]

    try:
        if socket_id:
            resolve_instance_id(socket_id, project_root)  # 仅记日志，供多实例排查

        PersistentStore.load()
        mfaalog.info("✅ [Agent] 存档/备份系统已就绪（存档号将在首个任务运行时确定）")
    except Exception as e:
        mfaalog.error(f"⚠️ 存档系统预热异常: {e}")
        if runtime.strict_storage:
            raise  # 安卓目录错误必须阻止启动，不能继续运行后看似保存成功。

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
    # 独立于 startup.sink 的同步窗口准备；仅任务开始时刷新日志显示配置。
    AgentServer._set_api_properties()
    AgentServer.add_tasker_sink(LogEventSink(project_root / "config" / "maa_option.json"))
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
