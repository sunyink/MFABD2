from pathlib import Path
import shutil
import sys
import os

# 解决 Windows CI 环境下打印 Emoji 报错的问题
# 本地编辑器报错,作为次选手段,先注释掉看看yml-env效果.
# if hasattr(sys.stdout, 'reconfigure'):
#     sys.stdout.reconfigure(encoding='utf-8')
#     sys.stderr.reconfigure(encoding='utf-8')

from configure import configure_ocr_model

working_dir = Path(__file__).parent
install_path = working_dir / Path("install")

# 发布 interface.json 的全部改写都在 scripts/release_interface.py，本文件只拷文件、调用它一次。
sys.path.insert(0, str(working_dir / "scripts"))
import release_interface  # noqa: E402

version = len(sys.argv) > 1 and sys.argv[1] or "v0.0.1"
target_os = len(sys.argv) > 2 and sys.argv[2] or "win"
# 确保这里能接收到 CI 传进来的版本号，默认为 0.0.0
maa_ver = len(sys.argv) > 3 and sys.argv[3] or "0.0.0"


# def install_deps():
#     if not (working_dir / "deps" / "bin").exists():
#         print("Please download the MaaFramework to \"deps\" first.")
#         print("请先下载 MaaFramework 到 \"deps\"。")
#         sys.exit(1)
# ... (保留原有注释代码) ...


def copy_resources_for_target(source, destination, target_os):
    """Copy resource packs, excluding other platforms from Android output."""
    android = release_interface.release_target(target_os) == "android"
    # Announcement is shared documentation, not a platform resource pack.
    allowed = {"base", "android_native", "Announcement"}

    def ignore(directory, names):
        if not android or Path(directory) != source:
            return []
        return [name for name in names if (source / name).is_dir() and name not in allowed]

    # A local Android build may reuse output from a previous desktop build.
    if android and destination.exists():
        for child in destination.iterdir():
            if child.is_dir() and child.name not in allowed:
                if child.is_symlink() or child.resolve().parent != destination.resolve():
                    raise ValueError(f"Refusing to remove resource directory outside output: {child}")
                shutil.rmtree(child)

    shutil.copytree(
        source,
        destination,
        dirs_exist_ok=True,
        ignore=ignore,
    )


def install_client_config():
    """Ship first-launch desktop defaults without packaging an active user config."""
    template = working_dir / "assets" / "config.template.json"
    if release_interface.release_target(target_os) == "android" or not template.is_file():
        return
    config_dir = install_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template, config_dir / "config.template.json")


def install_resource():
    configure_ocr_model()

    copy_resources_for_target(working_dir / "assets" / "resource", install_path / "resource", target_os)
    install_client_config()
    
    # ================= [MFAA布局文件预配置写入开始] =================
    # 单文件适配: 显式复制 assets/mfa_layout.json 到 install/resource/
    # 这样您就不需要改变文件结构，它会自动归位
    layout_src = working_dir / "assets" / "mfa_layout.json"
    if layout_src.exists():
        print(f"📦 检测到自定义布局，正在注入: {layout_src}")
        shutil.copy2(layout_src, install_path / "resource" / "mfa_layout.json")
    # ================= [预配置写入结束] =================


def install_chores():
    # 1. 基础文件复制 (保持不变)
    shutil.copy2(working_dir / "README.md", install_path)
    shutil.copy2(working_dir / "LICENSE", install_path)
    shutil.copy2(working_dir / "LICENSE-APACHE", install_path)
    shutil.copy2(working_dir / "LICENSE-MIT", install_path)
    shutil.copy2(working_dir / "TRADEMARKS.md", install_path)
    
    # 2. Mac 专属脚本处理
    if release_interface.release_target(target_os) == "macos":
        script_src_dir = working_dir / "release" / "mac"
        
        # 1. 处理 AKeySetup (需注入版本号)
        src_script = script_src_dir / "Mac启动方案2-系统环境联网配置_mac.command"
        dst_script = install_path / "2_备案-系统环境联网配置_mac.command"

        if src_script.exists():
            print(f"📦 发现脚本文件: {src_script}")
            try:
                # 读取内容
                with open(src_script, 'r', encoding='utf-8') as f:
                    content = f.read()

                # 替换占位符 {{MAA_VERSION}}
                # [修改] 强制替换，不依赖 if 检查失败
                new_content = content.replace("{{MAA_VERSION}}", maa_ver)
                
                with open(dst_script, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                
                # 尝试赋予执行权限
                try: os.chmod(dst_script, 0o755)
                except OSError as e:
                    print(f"⚠️ 无法为脚本设置执行权限 {dst_script}: {e}")
                
                print(f"✅ 版本号注入完成: {maa_ver}")

            except Exception as e:
                print(f"❌ 处理 Mac 脚本失败: {e}")
                # 失败兜底：至少拷贝过去
                shutil.copy2(src_script, dst_script)
        else:
            print(f"⚠️ 未找到 Mac 脚本源文件: {src_script}")

        # 2. [修改] 处理修复工具 (Fix Permission) - 路径也改到了 scripts/release
        fix_tool_src = script_src_dir / "Mac启动方案1-内置环境修复赋权_mac.command"
        fix_tool_dst = install_path / "1_Mac用户请先双击运行此环境修复.command"
        
        if fix_tool_src.exists():
            print(f"🚑 注入 Mac 修复工具...")
            shutil.copy2(fix_tool_src, fix_tool_dst)
            try: os.chmod(fix_tool_dst, 0o755)
            except OSError as e:
                print(f"⚠️ 无法为修复工具设置执行权限 {fix_tool_dst}: {e}")
        else:
            print(f"⚠️ 未找到修复工具: {fix_tool_src}")

def install_agent(target_os):
    print("正在安装 Agent...")
    print(f"Installing agent for {target_os}...")
    # 1. 复制 agent 文件夹
    agent_src = working_dir / "agent"
    agent_dst = install_path / "agent"
    if not agent_src.exists():
        # 没有 agent 目录 = 出的包 Agent 必然起不来。不能只警告后继续。
        print("::error::未找到 agent 源码目录，请确认代码结构！")
        sys.exit(1)
    shutil.copytree(agent_src, agent_dst, dirs_exist_ok=True)


def install_interface():
    """发布 interface.json 只在这里生成一次：资源、杂项与 agent/ 都已就位之后。"""
    try:
        release_interface.install(install_path, target_os, version, working_dir / "assets" / "interface.json")
    except Exception as error:
        # 读写失败时绝不能带着仓库版 interface 出包（child_exec 指向开发机 .venv），
        # 所有平台的 Agent 都会起不来，而后续没有任何一步会发现。
        print(f"::error::生成发布 interface.json 失败: {error}")
        raise


if __name__ == "__main__":
    # install_deps()
    install_resource()
    install_chores()
    install_agent(target_os)
    install_interface()
    print(f"Install to {install_path} successfully.")
