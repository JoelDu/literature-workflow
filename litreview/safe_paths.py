"""litreview/safe_paths.py — 请求方提供的文件名/ID 落盘前的净化与目录包含校验。

Web/MCP 接口里凡是把客户端传来的名字拼进文件系统路径的地方，都必须先过这里：
只取基名、拒绝控制字符与 "."/".."，最后以 realpath 确认目标仍在允许目录之内
（同时挡住符号链接逃逸）。中文等 Unicode 文件名保持原样。
"""
import os
import re
from typing import Optional

# 候选 ID 由 snowball 生成：cand_found_/cand_emerg_ + run_<hex> + OpenAlex Work ID。
_CANDIDATE_ID_RE = re.compile(r"^[A-Za-z0-9_\-.]{1,200}$")


def safe_basename(name: Optional[str]) -> Optional[str]:
    """把客户端文件名收敛为单一安全基名；无法安全使用时返回 None。

    同时按 "/" 与 "\\" 切分（Windows 浏览器可能上传完整路径），拒绝空名、
    "."/".."、NUL 与其他控制字符。
    """
    if not isinstance(name, str):
        return None
    base = re.split(r"[\\/]", name)[-1].strip()
    if not base or base in (".", ".."):
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in base):
        return None
    return base


def resolve_within(base_dir: str, name: str) -> Optional[str]:
    """返回 base_dir/name 的真实路径；若解析后落在 base_dir 之外则返回 None。"""
    root = os.path.realpath(base_dir)
    target = os.path.realpath(os.path.join(root, name))
    try:
        if os.path.commonpath([root, target]) != root or target == root:
            return None
    except ValueError:
        return None
    return target


def is_valid_candidate_id(candidate_id: Optional[str]) -> bool:
    """候选 ID 只允许 [A-Za-z0-9_-.]，且不得含 ".."。"""
    return (isinstance(candidate_id, str)
            and bool(_CANDIDATE_ID_RE.match(candidate_id))
            and ".." not in candidate_id)
