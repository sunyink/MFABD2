from pathlib import Path
import shutil
import sys
import re
import os

try:
    import jsonc
except ImportError as e:
    print("❌ 缺少依赖: json-with-comments")
    print("请运行以下命令安装:")
    print("  pip install json-with-comments")
    print("或")
    print("  pip install -r requirements.txt")
    sys.exit(1)

from configure import configure_ocr_model

working_dir = Path(__file__).parent
install_path = working_dir / Path("install")
version = len(sys.argv) > 1 and sys.argv[1] or "v0.0.1"

def install_deps():
    if not (working_dir / "deps" / "bin").exists():
        print("Please download the MaaFramework to \"deps\" first.")
        print("请先下载 MaaFramework 到 \"deps\"。")
        sys.exit(1)

    shutil.copytree(
        working_dir / "deps" / "bin",
        install_path,
        ignore=shutil.ignore_patterns(
            "*MaaDbgControlUnit*",
            "*MaaThriftControlUnit*",
            "*MaaRpc*",
            "*MaaHttp*",
        ),
        dirs_exist_ok=True,
    )
    shutil.copytree(
        working_dir / "deps" / "share" / "MaaAgentBinary",
        install_path / "MaaAgentBinary",
        dirs_exist_ok=True,
    )

def convert_line_endings(file_path):
    """将文件的换行符统一转换为 Windows 格式 (CRLF)"""
    try:
        # 读取文件内容
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 统一转换为 CRLF
        content = content.replace('\r\n', '\n')  # 先标准化为 LF
        
        # 写回文件
        with open(file_path, 'w', encoding='utf-8', newline='\r\n') as f:
            f.write(content)
        return True
    except Exception as e:
        print(f"转换换行符失败: {file_path} - {str(e)}")
        return False

def process_markdown_files(directory):
    """递归处理目录中的所有 Markdown 文件"""
    success = True
    if directory.exists():
        print(f"处理 Markdown 文件: {directory}")
        # 遍历目录中的所有文件
        for root, _, files in os.walk(directory):
            for file in files:
                file_path = Path(root) / file
                if file_path.suffix.lower() == '.md':  # 只处理 Markdown 文件
                    if convert_line_endings(file_path):
                        print(f"已转换: {file_path}")
                    else:
                        success = False
    return success

def process_json_files(directory):
    """递归处理目录中的所有 JSON 文件"""
    success = True
    if directory.exists():
        print(f"处理 JSON 文件: {directory}")
        # 遍历目录中的所有文件
        for root, _, files in os.walk(directory):
            for file in files:
                file_path = Path(root) / file
                if file_path.suffix.lower() == '.json':  # 只处理 JSON 文件
                    if convert_line_endings(file_path):
                        print(f"已转换: {file_path}")
                    else:
                        success = False
    return success

def install_resource():
    configure_ocr_model()

    # 复制整个 resource 目录
    shutil.copytree(
        working_dir / "assets" / "resource",
        install_path / "resource",
        dirs_exist_ok=True,
    )
    
    # 分别处理 MD 和 JSON 文件换行符
    all_success = True
    
    # 1. 处理公告文件夹的 Markdown 文件
    announcement_dir = install_path / "resource" / "Announcement"
    if not process_markdown_files(announcement_dir):
        all_success = False
    
    # 2. 处理 pipeline 文件夹的 JSON 文件
    pipeline_dir = install_path / "resource" / "pipeline"
    if not process_json_files(pipeline_dir):
        all_success = False
    
    # 3. 处理 Changelog.md 文件
    changelog_path = install_path / "resource" / "Changelog.md"
    if changelog_path.exists():
        print(f"处理更新日志文件: {changelog_path}")
        if not convert_line_endings(changelog_path):
            all_success = False
    else:
        print(f"注意: 未找到更新日志文件 {changelog_path}，跳过处理")
    
    if not all_success:
        print("警告: 部分文件换行符转换失败")

    # 复制并更新 interface.json
    shutil.copy2(
        working_dir / "assets" / "interface.json",
        install_path,
    )

    with open(install_path / "interface.json", "r", encoding="utf-8") as f:
        interface = jsonc.load(f)
    
    # 1. 更新根版本字段（保持 CI 原始格式）
    interface["version"] = version
    
    # 2. 动态更新 custom_title 中的版本号
    if "custom_title" in interface:
        # 匹配 "MFABD2)" 后到 " | 游戏版本" 前的所有内容
        pattern = r"(?<=MFABD2\))(.*?)(?=\s*\|\s*游戏版本：)"
        
        # 使用原始版本号，不修改格式
        display_version = f"{version} "
        
        # 执行替换
        new_title = re.sub(
            pattern, 
            display_version,
            interface["custom_title"]
        )
        interface["custom_title"] = new_title

    with open(install_path / "interface.json", "w", encoding="utf-8") as f:
        jsonc.dump(interface, f, ensure_ascii=False, indent=4)

def install_chores():
    shutil.copy2(working_dir / "README.md", install_path)
    shutil.copy2(working_dir / "LICENSE", install_path)
    shutil.copy2(working_dir / "LICENSE-APACHE", install_path)
    shutil.copy2(working_dir / "LICENSE-MIT", install_path)

def install_agent():
    shutil.copytree(
        working_dir / "agent",
        install_path / "agent",
        dirs_exist_ok=True,
    )

if __name__ == "__main__":
    install_deps()
    install_resource()
    install_chores()
    install_agent()
    print(f"Install to {install_path} successfully.")