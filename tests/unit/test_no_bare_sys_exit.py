"""防退化：proxyctl/ 下不允许出现裸 sys.exit(非 0)，
所有错误退出都应走 _io.fail()（统一带 hint / doc / envelope）。

允许的例外（共 ≤ 14 处）：
  - _io.py: 2 处（fail() 内部 + SIGINT handler 130）
  - cli.py: 4 处（cmd_help / cmd_subcommand_help / --version 的 OK 退出）
  - audit.py / trace.py: 各 1 处（命令收尾 OK 退出）
  - explain.py: 2 处（cmd_doctor 已自行 emit_json 后退出）
  - check.py: 3 处（4 阶段检查 json/plain/human 路径 + bench 已自行汇总退出）

总上限 14；超出此数说明引入了新的裸 sys.exit。
"""

from __future__ import annotations

import re
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent.parent / "src" / "proxyctl"


def _bare_sys_exit_count() -> tuple[int, list[str]]:
    """扫描 proxyctl/ 下所有 .py，返回 sys.exit / _sys.exit 出现次数 + 位置列表。"""
    pattern = re.compile(r"^\s*(?:_)?sys\.exit\(")
    hits: list[str] = []
    for path in sorted(PKG.rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(PKG.parent)}:{i}: {line.strip()}")
    return len(hits), hits


def test_bare_sys_exit_count_under_threshold():
    count, hits = _bare_sys_exit_count()
    assert count <= 14, (
        f"裸 sys.exit 数量超过阈值 14（当前 {count}）。"
        f"新增错误退出请走 _io.fail()。命中位置：\n"
        + "\n".join(hits)
    )
