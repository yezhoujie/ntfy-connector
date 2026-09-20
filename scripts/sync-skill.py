#!/usr/bin/env python3
"""把 `src/*.py` 同步到 `skill/agent-ntfy/scripts/`：源码维护在 `src/` 下，
skill 目录里的副本是给 agent 直接消费的独立拷贝，两边必须逐字节一致。

默认模式：把 `src/*.py` 复制到 `skill/agent-ntfy/scripts/`；目标目录里多出的 `.py`
文件一并删除，保证两边文件集合与内容都一致。

`--check` 模式：只逐文件比对字节，不写任何文件；发现不一致打印差异文件名并以非零
状态退出；一致则打印一行摘要，状态码 0。
"""

import filecmp
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
DEST = ROOT / "skill" / "agent-ntfy" / "scripts"


def _mismatched(src_files: dict[str, Path], dest_files: dict[str, Path]) -> list[str]:
    """两侧文件名并集里，内容不同或只在一侧存在的文件名，按名字排序。"""
    names = sorted(set(src_files) | set(dest_files))
    result = []
    for name in names:
        s, d = src_files.get(name), dest_files.get(name)
        if s is None or d is None or not filecmp.cmp(s, d, shallow=False):
            result.append(name)
    return result


def _utf8_stdio() -> None:
    """标准流切到 UTF-8：Windows 控制台与 CI 的缺省代码页（如 cp1252）编不了中文，会让一条摘要变成异常。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str]) -> int:
    _utf8_stdio()
    if argv not in ([], ["--check"]):
        print(f"用法：python3 {sys.argv[0]} [--check]", file=sys.stderr)
        return 2
    check_only = argv == ["--check"]

    src_files = {p.name: p for p in SRC.glob("*.py")}
    if not src_files:
        print(f"{SRC} 下没有任何 .py 文件，这道检查本身已失效", file=sys.stderr)
        print(f"no .py files under {SRC}; this check cannot work without them", file=sys.stderr)
        return 1

    dest_files = {p.name: p for p in DEST.glob("*.py")} if DEST.is_dir() else {}

    mismatched = _mismatched(src_files, dest_files)

    if check_only:
        if mismatched:
            print(f"{DEST} 与 {SRC} 不一致：")
            print(f"{DEST} differs from {SRC}:")
            for name in mismatched:
                print(f"  {name}")
            return 1
        print(f"{DEST} 与 {SRC} 一致（{len(src_files)} 个文件）")
        return 0

    DEST.mkdir(parents=True, exist_ok=True)
    for name in dest_files:
        if name not in src_files:
            (DEST / name).unlink()
    for name, path in src_files.items():
        shutil.copy2(path, DEST / name)
    print(f"已把 {len(src_files)} 个文件同步到 {DEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
