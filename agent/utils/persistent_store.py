import json
import os
import shutil
import re
import time
from datetime import datetime
from pathlib import Path
from . import mfaalog as logger
from .runtime_environment import StoragePolicy, resolve_storage_policy
import tempfile

# 读存档时遇到 OSError(占用/权限)的退避重试。多是瞬时的:另一个实例正在写、
# 杀毒软件正在扫。区别于 JSON 解析失败 —— 那才是数据真的坏了。
_READ_RETRY_TIMES = 3
_READ_RETRY_DELAY = 0.2

# ==============================================================================
# 🛠️ 存档系统使用指南 (PersistentStore Usage) - 多账号加强版
# ==============================================================================
# 特性：
# 1. 智能路径: 优先全局系统目录，检测到根目录存档自动切为绿色模式
# 2. 原子写入: 防止断电导致文件损坏 (.tmp 机制)
# 3. 自动备份: 每次写入自动生成 .bak 备份
# 4. 多账号隔离: 通过 switch_account(id) 动态切换读写文件
# ==============================================================================

class AccountNotReadyError(RuntimeError):
    """No verified account owns this storage access."""


class PersistentStore:
    APP_NAME = "MFABD2"
    
    # 状态与路径变量
    _initialized = False
    _storage_policy: StoragePolicy | None = None
    _mode = None
    _current_account_id = None
    _sanitized_account_id = None
    _mounted_account_id = None
    _account_ready = False
    _bound_task_key = None
    _directory_initialized = False
    # 存档暂时不可读(权限/占用)时置位。此时 load() 返回空视图,若不拦住 save(),
    # set() 的 load→mutate→save 会把这个空视图写回去,真实数据就被抹了。
    _degraded_readonly = False
    
    # 动态生成的文件名和路径
    FILE_NAME = "agent_save_data.json"
    BAK_NAME = "agent_save_data.json.bak"
    CONFIG_DIR = None
    FILE_PATH = None
    BACKUP_PATH = None

    @classmethod
    def configure_storage(cls, policy: StoragePolicy) -> None:
        """The entrypoint supplies its resolved policy before the first load."""
        if cls._directory_initialized and policy != cls._storage_policy:
            raise RuntimeError("Configure storage before loading a save")
        cls._storage_policy = policy

    @classmethod
    def block_account(cls, task_key=None):
        """Revoke access without creating, restoring or modifying any account file."""
        cls._account_ready = False
        cls._bound_task_key = task_key
        cls._current_account_id = None
        cls._sanitized_account_id = None
        cls._initialized = False
        cls.FILE_PATH = cls.BACKUP_PATH = None

    @classmethod
    def is_bound(cls, task_key, account_id):
        return (cls._account_ready and cls._initialized
                and cls._bound_task_key == task_key and cls._current_account_id == account_id)

    @classmethod
    def bind_account(cls, account_id, task_key):
        """Bind a verified choice to its root task; mounting does not load a save."""
        if account_id is None or not str(account_id).strip():
            cls.block_account(task_key)
            raise ValueError("An empty account is not the default account")
        safe_id = str(account_id).strip()
        cls._account_ready = False
        cls._bound_task_key = None
        if safe_id != cls._mounted_account_id:
            cls._initialized = False
            cls._degraded_readonly = False
        cls._current_account_id = safe_id
        try:
            cls._init_paths()
        except Exception:
            cls.block_account(task_key)
            raise
        cls._mounted_account_id = safe_id
        cls._bound_task_key = task_key
        cls._account_ready = True

    @classmethod
    def switch_account(cls, account_id):
        """Explicit selection for standalone callers; None never means account 0."""
        cls.bind_account(account_id, None)

    @classmethod
    def _require_account(cls):
        if not cls._account_ready:
            raise AccountNotReadyError("账号尚未确定，禁止读取账号存档")

    @classmethod
    def _init_paths(cls):
        """根据启动策略挂载当前账号路径。"""
        if cls._initialized:
            return
        if cls._current_account_id is None:
            raise AccountNotReadyError("账号尚未确定")

        # 1. 根据当前 _current_account_id 动态生成文件名和净化 ID
        if cls._current_account_id == "0":
            cls._sanitized_account_id = "0"
            cls.FILE_NAME = "agent_save_data.json"
            cls.BAK_NAME = "agent_save_data.json.bak"
        else:
            # 双重保险：后端过滤系统不支持的路径字符
            original_id = cls._current_account_id
            clean_id = re.sub(r'[\\/*?:"<>|]', "_", original_id)
            cls._sanitized_account_id = clean_id
            cls.FILE_NAME = f"agent_save_data_{clean_id}.json"
            cls.BAK_NAME = f"agent_save_data_{clean_id}.json.bak"
            
            # 记录原始 ID 与实际文件名之间的映射，方便排查问题
            if original_id != clean_id:
                logger.info(f"[Py] ⚠️ 账号 ID 已清洗: 原始='{original_id}', 清洗后='{clean_id}', 映射文件={cls.FILE_NAME}")

        cls.prepare_directory()
        cls.FILE_PATH = cls.CONFIG_DIR / cls.FILE_NAME
        cls.BACKUP_PATH = cls.CONFIG_DIR / cls.BAK_NAME

        cls._initialized = True

        # 状态汇报 (使用清洗后的 _sanitized_account_id)
        mode_str = {'global': '系统全局模式', 'portable': '绿色便携模式', 'host': '宿主指定模式'}[cls._mode]
        logger.info(f"[Py] 💾 存档挂载完成 | 账号ID: {cls._sanitized_account_id} | 模式: {mode_str}")
        logger.info(f"[Py] 📂 存档路径: {cls.FILE_PATH}")

    @classmethod
    def prepare_directory(cls):
        """Validate storage policy without mounting or loading account 0."""
        if cls._directory_initialized:
            return cls.CONFIG_DIR
        # 获取项目根目录
        base_dir = Path(__file__).resolve().parent.parent.parent

        # Standalone callers resolve once; normal startup injects this policy.
        # Account switching keeps the policy and only changes filenames.
        if cls._storage_policy is None:
            cls._storage_policy = resolve_storage_policy(base_dir)
        policy = cls._storage_policy
        portable_root = policy.portable_root
        has_portable_archive = (
            portable_root is not None and (
                any(portable_root.glob("agent_save_data*.json"))
                or any(portable_root.glob("agent_save_data*.json.bak"))
                or (portable_root / "agent_shared_data.json").exists()
                or (portable_root / "agent_shared_data.json.bak").exists()
            )
        )
        
        if has_portable_archive:
            cls._set_portable_mode(portable_root)
        else:
            try:
                cls._set_directory(policy.directory, 'global' if portable_root is not None else 'host')
            except OSError:
                if portable_root is None:
                    raise  # Explicit directories must not fall back to another save.
                logger.warning("[Py] ⚠️ 全局目录读写测试失败，自动降级为【绿色便携模式】。")
                cls._set_portable_mode(portable_root)
                
        cls._directory_initialized = True
        return cls.CONFIG_DIR

    @classmethod
    def _set_portable_mode(cls, base_dir: Path):
        """设定为绿色便携模式"""
        cls._set_directory(base_dir, 'portable')

    @classmethod
    def _set_directory(cls, directory: Path, mode: str) -> None:
        """Mount a preselected directory. Platform selection belongs to startup."""
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=directory) as probe:
            probe.write(b"test")
            probe.flush()
            os.fsync(probe.fileno())
        cls._mode = mode
        cls.CONFIG_DIR = directory

    @classmethod
    def load(cls) -> dict:
        """【智能读取】优先读主文件，坏了读备份，完全没有则初始化新档"""
        cls._require_account()
        cls._init_paths()

        assert cls.FILE_PATH is not None
        assert cls.BACKUP_PATH is not None
        
        # 💡纯新账号：主文件和备份都不存在，直接静默初始化
        if not cls.FILE_PATH.exists() and not cls.BACKUP_PATH.exists():
            logger.info(f"[Py] 🌱 账号 [{cls._sanitized_account_id}] 为全新存档，正在初始化...")
            empty_data = {}
            if cls._save_file(cls.FILE_PATH, empty_data):
                # 空档落盘成功 = 这个路径此刻确实可写，先前的降级态到此为止。
                # 这是 load() 唯一的提前返回口，不在这里清就再没机会清了。
                # 写失败则维持原状：文件本就不存在，放行写入也不会覆盖掉任何真实数据。
                cls._degraded_readonly = False
            return empty_data

        if not cls.FILE_PATH.exists() and cls.BACKUP_PATH.exists():
             try:
                 shutil.copy2(cls.BACKUP_PATH, cls.FILE_PATH)
                 logger.info(f"[Py] ✅ 账号 {cls._sanitized_account_id} 已从备份自动生成主存档！")
             except Exception as e:
                 logger.error(f"[Py] ❌ 恢复备份失败: {e}")

        data, status = cls._try_load_file(cls.FILE_PATH)
        if status == "ok" and data is not None:
            cls._degraded_readonly = False
            return data

        if status == "unreadable":
            # 读不到 ≠ 数据坏了。这里绝不能按"损坏"重置:
            #   · 真实数据大概率完好,写空就真毁了;
            #   · 而且读都读不了(权限/占用),写多半也写不进去,
            #     结果只是既没读到又没写成的静默失败。
            # 降级为只读空视图并锁写,让本轮跑完,下次再读。
            cls._degraded_readonly = True
            logger.error(
                f"[Py] ⛔ 账号 {cls._sanitized_account_id} 存档暂时不可读，"
                f"本轮降级为只读空视图且不会回写。请检查文件占用或权限。"
            )
            return {}

        # 缺失与损坏是两回事,日志别混着说(原先文件不存在也会报"主存档损坏")
        if status == "missing":
            logger.warning(f"[Py] ⚠️ 主存档缺失: {cls.FILE_PATH}")
        else:
            logger.warning(f"[Py] ⚠️ 主存档损坏: {cls.FILE_PATH}")

        if cls.BACKUP_PATH.exists():
            logger.info(f"[Py] 🔄 正在尝试从备份恢复: {cls.BACKUP_PATH}")
            bak_data, bak_status = cls._try_load_file(cls.BACKUP_PATH)
            if bak_status == "ok" and bak_data is not None:
                logger.info("[Py] ✅ 备份恢复成功！")
                cls._degraded_readonly = False
                cls._save_file(cls.FILE_PATH, bak_data)
                return bak_data
            if bak_status == "unreadable":
                # 主档已不可用、备份又读不到 —— 此时重置为空会把仅存的线索一并覆盖。
                cls._degraded_readonly = True
                logger.error(
                    f"[Py] ⛔ 账号 {cls._sanitized_account_id} 主存档不可用且备份暂时不可读，"
                    f"本轮降级为只读空视图且不会回写。"
                )
                return {}

        logger.error(f"[Py] ❌ 账号 {cls._sanitized_account_id} 存档彻底损坏且无有效备份，重置为空。")
        cls._quarantine(cls.FILE_PATH)   # 覆盖前先把坏档留一份，便于事后人工抢救
        cls._degraded_readonly = False
        empty_data = {}
        cls._save_file(cls.FILE_PATH, empty_data)
        return empty_data

    @classmethod
    def _try_load_file(cls, path: Path) -> tuple[dict | None, str]:
        """读一个存档文件，并区分失败的性质。

        原实现是裸 `except Exception: return None`,把 PermissionError、OSError、
        UnicodeDecodeError 和 JSONDecodeError 一视同仁地判成"损坏",调用方随即
        重置为空并写回磁盘 —— 一次瞬时的文件占用就能把用户存档清零。

        Returns
        -------
        (data, status), status 取值:
            ok         读到了合法 dict
            missing    文件不存在
            corrupt    文件在但内容不是合法 JSON / 不是 dict —— 数据真的坏了
            unreadable 文件在但读不出来(权限/占用) —— 数据大概率完好,只是这一刻拿不到
        """
        if not path.exists():
            return None, "missing"

        last_err = None
        for attempt in range(_READ_RETRY_TIMES):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except json.JSONDecodeError as e:
                logger.error(f"[Py] ❌ 存档 JSON 解析失败 {path}: {e}")
                return None, "corrupt"
            except UnicodeDecodeError as e:
                logger.error(f"[Py] ❌ 存档编码错误 {path}: {e}")
                return None, "corrupt"
            except OSError as e:
                last_err = e
                if attempt + 1 < _READ_RETRY_TIMES:
                    time.sleep(_READ_RETRY_DELAY)
                continue

            if not isinstance(data, dict):
                logger.error(f"[Py] ❌ 存档内容不是有效的字典结构: {path}")
                return None, "corrupt"
            return data, "ok"

        logger.error(
            f"[Py] ❌ 存档重试 {_READ_RETRY_TIMES} 次仍不可读 {path}: {last_err}"
        )
        return None, "unreadable"

    @classmethod
    def _quarantine(cls, path: Path) -> None:
        """把确认损坏的存档改名留档，别让它被空档直接覆盖掉。"""
        try:
            if not path.exists():
                return
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            dst = path.with_name(f"{path.name}.corrupt.{stamp}")
            os.replace(path, dst)
            logger.warning(f"[Py] 📦 损坏的存档已留档: {dst}")
        except OSError as e:
            logger.warning(f"[Py] 留档损坏存档失败（不影响后续流程）: {e}")

    @classmethod
    def save(cls, data: dict):
        """【安全写入】写主文件 -> 成功 -> 覆盖备份"""
        if not cls._account_ready:
            return False
        cls._init_paths()
        assert cls.FILE_PATH is not None
        assert cls.BACKUP_PATH is not None

        # 降级只读态下必须拦住写入。set() 是 load→mutate→save,而降级时 load 给的是
        # 空视图 —— 放行就会把 {唯一这个键} 写回去,真实存档里其余的键全没了。
        if cls._degraded_readonly:
            logger.error(
                f"[Py] ⛔ 账号 {cls._sanitized_account_id} 存档处于不可读降级态，"
                f"已拒绝本次写入以免覆盖真实数据。"
            )
            return False

        if cls._save_file(cls.FILE_PATH, data):
            try:
                shutil.copy2(cls.FILE_PATH, cls.BACKUP_PATH)
            except Exception as e:
                logger.warning(f"[Py] 备份更新失败 (不影响主流程): {e}")
            return True
        return False

    @classmethod
    def _save_file(cls, path: Path, data: dict) -> bool:
        # tmp 名带 pid:原先是 path.with_suffix(".tmp"),同账号的两个进程会写同一个
        # 临时文件,内容交错后被一起搬成主文件。
        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())   # 不 fsync,断电后这次 replace 可能落地成空文件
            # 必须是 os.replace 而不是 shutil.move:后者在 Windows 上目标已存在时
            # os.rename 会抛 FileExistsError,于是回落到 copy2(先截断再写)——非原子。
            # 也就是说除首次创建外,过去每一次 save 走的都是非原子路径,
            # 正好推翻本文件开头"原子写入:防止断电导致文件损坏"那句承诺。
            os.replace(tmp_path, path)
            return True
        except Exception as e:
            logger.error(f"[Py] 写入文件失败 {path}: {e}")
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    @classmethod
    def get(cls, key: str, default=None):
        data = cls.load()
        return data.get(key, default)

    @classmethod
    def set(cls, key: str, value):
        if not cls._account_ready:
            return False
        data = cls.load()
        data[key] = value
        return cls.save(data)


class SharedStore(PersistentStore):
    """所有游戏存档共用的持久化文件。

    账号存档继续由 PersistentStore 管理；这里只承载与账号无关、可以跨存档复用的
    观察事实，例如同一天的商店行情。读写、损坏隔离、原子替换与备份策略全部复用
    PersistentStore，唯一差别是文件名固定、不随账号切换，也不受账号封锁限制。
    """

    _initialized = False
    _mode = None
    _current_account_id = "shared"
    _sanitized_account_id = "shared"
    _account_ready = True
    _degraded_readonly = False

    FILE_NAME = "agent_shared_data.json"
    BAK_NAME = "agent_shared_data.json.bak"
    CONFIG_DIR = None
    FILE_PATH = None
    BACKUP_PATH = None

    @classmethod
    def switch_account(cls, account_id):
        """共享数据没有账号维度；保留同名方法以防调用方误切。"""
        return None

    @classmethod
    def _init_paths(cls):
        """与账号存档同目录，但不需要先确定账号。"""
        config_dir = PersistentStore.prepare_directory()
        if config_dir is None:
            raise RuntimeError("存档目录不可用，无法确定共享存档目录")

        if cls._initialized and cls.CONFIG_DIR == config_dir:
            return

        cls._mode = PersistentStore._mode
        cls.CONFIG_DIR = config_dir
        cls.FILE_PATH = config_dir / cls.FILE_NAME
        cls.BACKUP_PATH = config_dir / cls.BAK_NAME
        cls._initialized = True
        cls._degraded_readonly = False

        mode_str = {'global': '系统全局模式', 'portable': '绿色便携模式', 'host': '宿主指定模式'}[cls._mode]
        logger.info(f"[Py] 🌐 共享存档挂载完成 | 模式: {mode_str}")
        logger.info(f"[Py] 📂 共享存档路径: {cls.FILE_PATH}")
