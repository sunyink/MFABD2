#!/usr/bin/env python3
"""
Git操作模块 - 获取精确的提交列表（修复编码问题）
"""

import subprocess
import re
from typing import List, Dict, Optional
from version_rules import filter_valid_versions, sort_versions
import time

def get_all_tags() -> list:
    """获取所有Git标签"""
    try:
        result = subprocess.run(
            ["git", "tag", "-l", "v*"],
            capture_output=True, 
            text=True,
            check=True
        )
        tags = [tag for tag in result.stdout.strip().split('\n') if tag]
        return tags
    except Exception as e:
        print(f"获取Git标签失败: {e}")
        return []

def run_git_command(args: List[str]) -> str:
    """运行Git命令并返回输出（修复编码问题）"""
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            encoding='utf-8',  # ✅ 强制使用UTF-8编码
            errors='ignore',   # ✅ 忽略无法解码的字符
            check=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        # 仅在非验证类命令失败时打印详细日志，避免 rev-parse 刷屏
        if "rev-parse" not in args:
            print(f"Git命令失败: {' '.join(args)}")
            print(f"错误码: {e.returncode}")
            if e.stderr:
                print(f"错误信息: {e.stderr}")
        return ""

def get_commit_date(tag: str) -> Optional[str]:
    """获取标签的提交日期"""
    date_str = run_git_command(["log", "-1", "--format=format:%ai", tag])
    return date_str if date_str else None

def compare_tag_dates(tag1: str, tag2: str) -> int:
    """比较两个标签的时间顺序"""
    date1 = get_commit_date(tag1)
    date2 = get_commit_date(tag2)
    
    print(f"标签 {tag1} 日期: {date1}")
    print(f"标签 {tag2} 日期: {date2}")
    
    if date1 and date2:
        return -1 if date1 < date2 else (1 if date1 > date2 else 0)
    else:
        print("无法获取标签日期，使用版本号顺序")
        return 0

def get_simple_commit_list(from_ref: str, to_ref: str) -> List[Dict]:
    """获取简化的提交列表（更稳定的方法）"""
    
    print(f"尝试获取提交: {from_ref}..{to_ref}")
    
    # 方法1: 使用简单的oneline格式
    log_output = run_git_command([
        "log", 
        f"{from_ref}..{to_ref}",
        "--oneline",
        "--no-merges"
    ])
    
    commits = []
    for line in log_output.split('\n'):
        if line.strip():
            # 解析格式: "哈希 提交信息"
            parts = line.split(' ', 1)
            if len(parts) == 2:
                commit = {
                    'hash': parts[0],
                    'subject': parts[1],
                    'author_name': '未知',  # 简化版本
                    'author_email': '',
                    'date': '',
                    'body': ''
                }
                commits.append(commit)
    
    return commits

def get_detailed_commit_info(commit_hash: str) -> Dict:
    """获取单个提交的详细信息"""
    author = run_git_command(["log", "-1", "--format=format:%an", commit_hash])
    email = run_git_command(["log", "-1", "--format=format:%ae", commit_hash])
    date = run_git_command(["log", "-1", "--format=format:%ad", commit_hash])
    subject = run_git_command(["log", "-1", "--format=format:%s", commit_hash])
    body = run_git_command(["log", "-1", "--format=format:%b", commit_hash])
    
    return {
        'hash': commit_hash,
        'author_name': author if author else '未知',
        'author_email': email if email else '',
        'date': date if date else '',
        'subject': subject if subject else '',
        'body': body if body else ""
    }

def get_commit_list(from_ref: str, to_ref: str) -> List[Dict]:
    """获取两个引用之间的提交列表（稳定版本）"""
    
    # 首先检查时间顺序
    print("检查标签时间顺序...")
    date_comparison = compare_tag_dates(from_ref, to_ref)
    
    if date_comparison > 0:
        # from_ref 比 to_ref 新，需要交换顺序
        print(f"注意: {from_ref} 比 {to_ref} 新，自动调整对比顺序")
        actual_from = to_ref
        actual_to = from_ref
    else:
        actual_from = from_ref
        actual_to = to_ref
    
    print(f"最终对比范围: {actual_from}..{actual_to}")
    
    # 先获取简化的提交列表
    simple_commits = get_simple_commit_list(actual_from, actual_to)
    print(f"找到 {len(simple_commits)} 个提交")
    
    # 然后为每个提交获取详细信息
    detailed_commits = []
    for i, simple_commit in enumerate(simple_commits):
        # print(f"获取提交详情 {i+1}/{len(simple_commits)}: {simple_commit['hash'][:8]}") # 注释掉避免刷屏
        detailed_commit = get_detailed_commit_info(simple_commit['hash'])
        detailed_commits.append(detailed_commit)
    
    return detailed_commits

def test_git_operations_simple():
    """简单的Git操作测试"""
    print("=== 简化Git操作测试 ===\n")
    
    # 先测试一个更简单的命令
    print("1. 测试Git基础功能...")
    git_version = run_git_command(["--version"])
    print(f"Git版本: {git_version}")
    
    print("\n2. 测试标签列表...")
    tags = run_git_command(["tag", "-l", "v2.3.*", "--sort=-version:refname"])
    tag_list = tags.split('\n') if tags else []
    print(f"找到 {len(tag_list)} 个v2.3.*标签: {tag_list[:5]}...")
    
    print("\n3. 测试提交范围...")
    
    # 测试小范围的提交
    test_commits = get_simple_commit_list("v2.3.5", "v2.3.6")
    print(f"简单提交列表数量: {len(test_commits)}")
    
    if test_commits:
        print("\n前3个提交:")
        for i, commit in enumerate(test_commits[:3]):
            print(f"  {i+1}. [{commit['hash'][:8]}] {commit['subject']}")
    else:
        print("没有找到提交，可能的原因:")
        print("  - 标签之间确实没有提交")
        print("  - 标签顺序特殊")
        print("  - 可以尝试其他标签范围")

# 注意：这里原文件有两个 get_all_tags，我把重复的去掉了

def ensure_reference_exists(ref: str) -> bool:
    """确保Git引用存在"""
    result = run_git_command(["rev-parse", "--verify", ref])
    return bool(result)

def resolve_branch_reference(ref: str) -> str:
    """
    【新增】智能解析分支引用
    优先查找本地分支，如果不存在则查找远程分支（适配CI环境）
    """
    if ensure_reference_exists(ref):
        return ref
    
    # 尝试查找远程分支
    remote_ref = f"origin/{ref}"
    if ensure_reference_exists(remote_ref):
        print(f"本地分支 '{ref}' 不存在，使用远程分支 '{remote_ref}'")
        return remote_ref
        
    return ref  # 如果都不存在，返回原值让后续报错

def safe_get_commit_list(from_ref: str, to_ref: str) -> List[Dict]:
    """安全的提交列表获取（处理引用不存在的情况）"""
    
    # 确保引用存在
    if not ensure_reference_exists(from_ref):
        print(f"警告: 引用 {from_ref} 不存在，尝试使用默认基准")
        # 尝试使用最新的正式版作为基准
        all_tags = get_all_tags()
        filtered = filter_valid_versions(all_tags)
        if filtered['formal']:
            from_ref = sort_versions(filtered['formal'])[0]
            print(f"使用最新正式版作为基准: {from_ref}")
        else:
            # 如果没有正式版，使用初始提交
            from_ref = "HEAD~100"
            print(f"使用初始提交作为基准: {from_ref}")
    
    if not ensure_reference_exists(to_ref):
        print(f"警告: 引用 {to_ref} 不存在，使用HEAD")
        to_ref = "HEAD"
    
    return get_commit_list(from_ref, to_ref)

def test_specific_range():
    """测试特定的提交范围"""
    print("\n=== 测试特定提交范围 ===")
    
    # 测试一个肯定有提交的范围
    test_from = "v2.3.4"
    test_to = "v2.3.5"
    
    print(f"测试范围: {test_from}..{test_to}")
    commits = get_commit_list(test_from, test_to)
    print(f"详细提交数量: {len(commits)}")

def test_safe_operations():
    """测试安全操作"""
    print("\n=== 测试安全Git操作 ===")
    
    # 测试不存在的引用
    print("测试不存在的引用处理...")
    commits = safe_get_commit_list("main", "v2.3.5")
    print(f"安全操作提交数量: {len(commits)}")


def get_commit_timestamp(ref: str) -> int:
    """获取提交的Committer Unix时间戳"""
    ts = run_git_command(["log", "-1", "--format=%ct", ref])
    return int(ts) if ts and ts.strip().isdigit() else 0

# 【修改函数】获取合并提交列表 (增加时间戳返回)
def get_merge_commits(from_ref: str, to_ref: str) -> List[Dict]:
    """
    获取合并提交列表，包含时间戳
    """
    # 使用自定义格式输出: hash | timestamp | subject
    # %ct 是提交人的Unix时间戳
    log_output = run_git_command([
        "log", 
        f"{from_ref}..{to_ref}",
        "--format=%h|%ct|%s",
        "--merges",
        "--topo-order"
    ])
    
    commits = []
    for line in log_output.split('\n'):
        if line.strip():
            parts = line.split('|', 2)
            if len(parts) == 3:
                commits.append({
                    'hash': parts[0],
                    'timestamp': int(parts[1]),
                    'subject': parts[2]
                })
    return commits

# 【修改函数】包含之前的正则终极修复
def get_released_branches_from_main(ref: str = "main", limit: int = 2000) -> set:
    """
    扫描指定引用(ref)的合并记录，提取已发布的分支名
    修复：全面覆盖 GitHub PR、同仓库合并、中文客户端及自定义格式
    """
    target_ref = resolve_branch_reference(ref)
    print(f"正在扫描 {target_ref} 的已发布分支...")
    
    log_output = run_git_command([
        "log",
        target_ref,
        "-n", str(limit),
        "--oneline",
        "--merges"
    ])
    
    released = set()
    
    # 1. 自定义格式 (Merge:'xxx')
    pattern_custom = r"Merge:'([^']+)'"
    # 2. 标准/中文 Git 格式 (带引号)
    pattern_git_quoted = r"(?:Merge branch|合并分支)\s*'([^']+)'"
    # 3. GitHub PR 格式 (提取 from 后面部分)
    pattern_pr = r"Merge pull request #[0-9]+ from (\S+)"
    # 4. 无引号格式 (兜底)
    pattern_git_plain = r"(?:Merge branch|合并分支)\s+(\S+)"

    for line in log_output.split('\n'):
        # 依次尝试匹配
        match = re.search(pattern_custom, line)
        if match:
            released.add(match.group(1))
            continue
            
        match = re.search(pattern_git_quoted, line)
        if match:
            released.add(match.group(1))
            continue
            
        match = re.search(pattern_pr, line)
        if match:
            full_ref = match.group(1)
            released.add(full_ref)
            if '/' in full_ref: # 尝试剥离用户名 fix/branch
                parts = full_ref.split('/', 1)
                if len(parts) > 1: released.add(parts[1])
            continue

        match = re.search(pattern_git_plain, line)
        if match:
            candidate = match.group(1)
            if candidate.lower() not in ['into', 'from']:
                released.add(candidate)
            
    print(f"共发现 {len(released)} 个已发布分支")
    return released

if __name__ == "__main__":
    # print("=== Git操作模块测试 ===") # 保持原样
    test_git_operations_simple()
    test_specific_range()
    test_safe_operations()