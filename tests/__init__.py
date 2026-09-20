"""让 src/ 下的模块能被测试直接 import（它们是以脚本形态运行的，不是包）；同时把 NTFY_CONNECTOR_STORE 缺省钉为 file。"""

import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

# 测试进程缺省不碰真钥匙串 / DPAPI：不注入 store 就起 daemon 的用例（如真起子进程的）按平台缺省会走 KeychainStore / DpapiStore，
# 这里把缺省钉成文件存储（Harness 自己注入 MemoryStore，不经这条）。显式设了的用例不受影响；
# 测「平台缺省」的用例自己从 environ 里删掉这个变量。
os.environ.setdefault("NTFY_CONNECTOR_STORE", "file")

# 迁移逻辑缺省搬 ~/.agent-ntfy——开发者机器上可能真有一个 0.1.x 留下的。测试进程里把缺省钉到一个不存在的路径：
# 进程内跑 main() 的用例（--home 是一次性临时目录）不会把真目录搬进去。起真子进程的用例自己把 HOME / USERPROFILE 指到临时目录。
import migrate  # noqa: E402  上面刚把 src 加进 sys.path

migrate.LEGACY_HOME = migrate.LEGACY_HOME.with_name("no-legacy-home-in-tests")
# 开发者 shell 里残留的 AGENT_NTFY_* 会让每条进程内跑的命令多打一行「检测到旧版环境变量」，断言 stderr 原文的用例就全红；
# 测这条警告的用例自己往 environ 里放
for _k in [k for k in os.environ if k.startswith(migrate.LEGACY_ENV_PREFIX)]:
    os.environ.pop(_k)
