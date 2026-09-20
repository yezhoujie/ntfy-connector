"""老用户自动迁移：用户目录 rename / 钥匙串读旧写新删旧 / 旧 daemon 守卫 / 环境变量只警告 / 项目状态目录 / CLI 入口接线。

临时目录 + 内存密钥存储替身，不打真 ntfy.sh、不碰真钥匙串；真钥匙串只在 NTFY_CONNECTOR_SMOKE=1 下用临时条目走一遍，跑完删。
起真子进程的用例把 HOME / USERPROFILE 指到临时目录：开发者机器上可能真有一个 0.1.x 留下的 ~/.agent-ntfy，不能碰。
"""

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import ipc
import migrate
import state
import texts
from state import StateError
from tests.test_state import MemoryStore

SCRIPT = Path(__file__).resolve().parents[1] / "src" / "ntfy_connector.py"


def Z(key, **fmt):
    return texts.t(key, "zh", **fmt)


def other_transport() -> str | None:
    """与本进程当前传输相反的那种；本平台没有它（Windows 没有 unix）就 None。"""
    other = "tcp" if ipc.transport() == "unix" else "unix"
    return None if other == "unix" and ipc.AF_UNIX is None else other


class LegacyMemoryStore(MemoryStore):
    """旧钥匙串条目的替身：比 MemoryStore 多一个 delete()，可预设删除时抛的错。"""

    def __init__(self, topics=None, *, delete_error=None):
        super().__init__()
        self.topics = topics
        self.delete_error = delete_error
        self.deleted = 0

    def delete(self):
        if self.delete_error is not None:
            raise self.delete_error
        self.topics = None
        self.deleted += 1


class FailingLoadStore(LegacyMemoryStore):
    def load(self):
        raise StateError("keychain.read_failed", service="X", rc=1, detail="locked")


def fake_legacy_daemon(case: unittest.TestCase, legacy_home: Path, via: str | None = None) -> None:
    """在旧目录里起一个只会答 status 的监听端，模拟还在跑的 0.1.x daemon；via 指定传输（缺省按本进程的）。用例结束时收掉。"""
    legacy_home.mkdir(parents=True, exist_ok=True)
    with mock.patch.dict(os.environ, {ipc.ENV_VAR: via} if via else {}):
        listener, cleanup = ipc.listen(legacy_home)
    listener.setblocking(True)
    listener.settimeout(0.2)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _addr = listener.accept()
            except OSError:
                continue
            with conn:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                conn.sendall(b'{"event":"status","pid":123}\n')

    t = threading.Thread(target=serve, daemon=True)
    t.start()

    def teardown():
        stop.set()
        t.join(2)
        listener.close()
        cleanup()
    case.addCleanup(teardown)


class MigrateRunTest(unittest.TestCase):
    """run()：四步（守卫 → 用户目录 → 钥匙串 → 环境变量）各自独立，替身全部显式注入。

    home 缺省就是目标目录（正常用户：--home 没改、就是 ~/.ntfy-connector）；旧目录的去处固定是 new，与 home 无关。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mig-"))  # 短路径：unix 传输下 socket 路径有长度上限
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.legacy = self.tmp / ".agent-ntfy"
        self.new = self.tmp / ".ntfy-connector"
        self.lines: list[str] = []

    def make_legacy(self):
        self.legacy.mkdir()
        (self.legacy / "leases.json").write_text('{"slot1": {}}\n', encoding="utf-8")

    def run_migrate(self, home=None, **kw):
        kw.setdefault("keychain", (LegacyMemoryStore(), MemoryStore()))
        kw.setdefault("env", {})
        kw.setdefault("probe", lambda h: None)
        return migrate.run(self.new if home is None else home, lang="zh", err=self.lines.append, legacy_home=self.legacy, new_home=self.new, **kw)

    # ---- 用户目录

    def test_only_legacy_dir_is_renamed(self):
        self.make_legacy()
        r = self.run_migrate()
        self.assertFalse(self.legacy.exists())
        self.assertEqual(json.loads((self.new / "leases.json").read_text(encoding="utf-8")), {"slot1": {}})
        self.assertEqual(r.moved, [str(self.legacy)])
        self.assertEqual(r.warnings, [])
        self.assertEqual(self.lines, [Z("migrate.home.moved", old=self.legacy, new=self.new)])
        # 可重入：再跑一次什么都不做
        self.lines.clear()
        r = self.run_migrate()
        self.assertEqual((r.moved, r.warnings, self.lines), ([], [], []))

    def test_legacy_dir_goes_to_the_default_place_not_to_home(self):
        # 随手 --home /tmp/x 跑一条命令不能把用户数据搬进那个目录
        self.make_legacy()
        other = self.tmp / "elsewhere"
        r = self.run_migrate(home=other)
        self.assertEqual(r.moved, [str(self.legacy)])
        self.assertTrue((self.new / "leases.json").exists())
        self.assertFalse(other.exists())
        self.assertEqual(self.lines, [Z("migrate.home.moved", old=self.legacy, new=self.new)])

    def test_missing_parent_of_new_home_is_created(self):
        self.make_legacy()
        new = self.tmp / "deep" / "er" / "home"
        r = migrate.run(new, lang="zh", err=self.lines.append, legacy_home=self.legacy, new_home=new, keychain=(LegacyMemoryStore(), MemoryStore()), env={}, probe=lambda h: None)
        self.assertEqual(r.moved, [str(self.legacy)])
        self.assertTrue((new / "leases.json").exists())
        if sys.platform != "win32":
            self.assertEqual(new.parent.stat().st_mode & 0o777, 0o700)

    def test_only_new_dir_is_left_alone(self):
        self.new.mkdir()
        (self.new / "leases.json").write_text("{}\n", encoding="utf-8")
        r = self.run_migrate()
        self.assertEqual((r.moved, r.warnings, self.lines), ([], [], []))
        self.assertTrue((self.new / "leases.json").exists())

    def test_both_dirs_keep_legacy_and_warn(self):
        self.make_legacy()
        self.new.mkdir()
        r = self.run_migrate()
        self.assertTrue((self.legacy / "leases.json").exists())
        self.assertEqual(r.moved, [])
        self.assertEqual(r.warnings, [Z("migrate.home.both_exist", old=self.legacy, new=self.new)])
        self.assertEqual(self.lines, r.warnings)  # 警告已经打过了，调用方不用再打

    def test_rename_failure_keeps_legacy_and_warns(self):
        self.make_legacy()
        with mock.patch.object(Path, "rename", side_effect=OSError(18, "Cross-device link")):
            r = self.run_migrate()
        self.assertTrue((self.legacy / "leases.json").exists())
        self.assertEqual(r.moved, [])
        self.assertEqual(len(r.warnings), 1)
        self.assertIn("Cross-device link", r.warnings[0])
        self.assertIn(str(self.legacy), r.warnings[0])

    def test_home_that_is_the_legacy_dir_counts_as_migrated(self):
        # 把 AGENT_NTFY_HOME=~/.agent-ntfy 机械改成 NTFY_CONNECTOR_HOME=~/.agent-ntfy 的用户：那里听着的是新 daemon，不是旧的；
        # 也没有什么可搬、可并存的
        self.make_legacy()
        for home in (self.legacy, self.legacy / ".." / ".agent-ntfy"):
            with self.subTest(home=str(home)):
                self.lines.clear()
                r = self.run_migrate(home=home, probe=lambda h: {"event": "status", "pid": 1})
                self.assertEqual((r.moved, r.warnings, self.lines), ([], [], []))
        self.assertTrue((self.legacy / "leases.json").exists())
        self.assertFalse(self.new.exists())

    # ---- 旧 daemon 守卫

    def test_legacy_daemon_listening_blocks_everything(self):
        self.make_legacy()
        fake_legacy_daemon(self, self.legacy)  # 真端点：按当前传输在旧目录里听
        old, new = LegacyMemoryStore(["agent-ntfy-a"]), MemoryStore()
        with self.assertRaises(migrate.LegacyDaemonRunning) as cm:
            migrate.run(self.new, lang="zh", err=self.lines.append, legacy_home=self.legacy, new_home=self.new, keychain=(old, new), env={"AGENT_NTFY_URL": "x"})
        self.assertEqual(cm.exception.legacy_home, self.legacy)
        self.assertTrue((self.legacy / "leases.json").exists())
        self.assertFalse(self.new.exists())
        self.assertEqual((old.topics, new.topics, old.deleted), (["agent-ntfy-a"], None, 0))  # 钥匙串一步没做
        self.assertEqual(self.lines, [])  # 环境变量警告也没打：什么都没迁，由调用方报一条

    def test_legacy_daemon_on_the_other_transport_is_still_caught(self):
        # 旧 daemon 的传输由当年的环境变量定：旧目录里是 daemon.port 而本进程缺省 unix（或反过来），守卫照样要探到它
        via = other_transport()
        if via is None:
            self.skipTest("本平台只有一种传输")
        self.make_legacy()
        fake_legacy_daemon(self, self.legacy, via=via)
        self.assertIsNone(ipc.probe(self.legacy))  # 按本进程的传输探不到——正是要补的那个洞
        with self.assertRaises(migrate.LegacyDaemonRunning):
            migrate.run(self.new, lang="zh", err=self.lines.append, legacy_home=self.legacy, new_home=self.new, keychain=(LegacyMemoryStore(), MemoryStore()), env={})
        self.assertTrue((self.legacy / "leases.json").exists())
        self.assertFalse(self.new.exists())

    def test_injected_probe_decides_the_guard(self):
        self.make_legacy()
        with self.assertRaises(migrate.LegacyDaemonRunning):
            self.run_migrate(probe=lambda h: {"event": "status", "pid": 1})
        self.assertTrue(self.legacy.exists())
        # 没有旧目录就不探活：探针根本不会被调
        shutil.rmtree(self.legacy)
        calls = []
        self.run_migrate(probe=lambda h: calls.append(h) or {"pid": 1})
        self.assertEqual(calls, [])

    def test_guard_probes_the_legacy_home_not_the_new_one(self):
        self.make_legacy()
        seen = []
        self.run_migrate(probe=lambda h: seen.append(h))
        self.assertEqual(seen, [self.legacy])

    # ---- 钥匙串（只在本次运行开始时旧目录还在才做）

    def test_keychain_copied_to_new_then_old_deleted(self):
        self.make_legacy()
        old, new = LegacyMemoryStore(["agent-ntfy-a", "agent-ntfy-b"]), MemoryStore()
        r = self.run_migrate(keychain=(old, new))
        self.assertEqual(new.topics, ["agent-ntfy-a", "agent-ntfy-b"])  # topic 名原样：手机订阅不变
        self.assertEqual((old.topics, old.deleted), (None, 1))
        self.assertEqual(r.moved, [str(self.legacy), "keychain"])
        self.assertEqual(r.warnings, [])
        self.assertEqual(self.lines, [Z("migrate.home.moved", old=self.legacy, new=self.new),
                                      Z("migrate.keychain.moved", old=migrate.LEGACY_SERVICE, new=state.KEYCHAIN_SERVICE)])

    def test_keychain_step_needs_the_legacy_dir_to_be_present(self):
        # 目录早就搬过（或从来没有）⇒ 不碰钥匙串：不然每条命令都会 spawn 一次 security
        old, new = FailingLoadStore(["agent-ntfy-a"]), FailingLoadStore()  # 真被读到就会抛、进 warnings
        r = self.run_migrate(keychain=(old, new))
        self.assertEqual((r.moved, r.warnings, self.lines, new.topics, old.deleted), ([], [], [], None, 0))

    def test_keychain_step_runs_while_both_dirs_exist(self):
        self.make_legacy()
        self.new.mkdir()
        old, new = LegacyMemoryStore(["agent-ntfy-a"]), MemoryStore()
        r = self.run_migrate(keychain=(old, new))
        self.assertEqual((new.topics, old.deleted, r.moved), (["agent-ntfy-a"], 1, ["keychain"]))
        self.assertEqual(self.lines, [Z("migrate.home.both_exist", old=self.legacy, new=self.new),
                                      Z("migrate.keychain.moved", old=migrate.LEGACY_SERVICE, new=state.KEYCHAIN_SERVICE)])

    def test_keychain_step_runs_when_home_is_the_legacy_dir(self):
        # 视为已迁只是不动目录；池子还在旧条目里，新 daemon 读的是新条目，不搬就会凭空建新池子
        self.make_legacy()
        old, new = LegacyMemoryStore(["agent-ntfy-a"]), MemoryStore()
        r = self.run_migrate(home=self.legacy, keychain=(old, new))
        self.assertEqual((new.topics, old.deleted, r.moved), (["agent-ntfy-a"], 1, ["keychain"]))
        self.assertEqual(self.lines, [Z("migrate.keychain.moved", old=migrate.LEGACY_SERVICE, new=state.KEYCHAIN_SERVICE)])

    def test_keychain_new_present_is_left_alone(self):
        self.make_legacy()
        old, new = LegacyMemoryStore(["agent-ntfy-a"]), MemoryStore()
        new.save(["ntfy-connector-x"])
        r = self.run_migrate(keychain=(old, new))
        self.assertEqual((new.topics, old.topics, old.deleted), (["ntfy-connector-x"], ["agent-ntfy-a"], 0))
        self.assertEqual((r.moved, r.warnings), ([str(self.legacy)], []))
        self.assertEqual(self.lines, [Z("migrate.home.moved", old=self.legacy, new=self.new)])

    def test_keychain_nothing_old_is_a_noop(self):
        for topics in (None, []):  # 空池子与不存在同义
            with self.subTest(old=topics):
                self.make_legacy()
                self.lines.clear()
                old, new = LegacyMemoryStore(topics), MemoryStore()
                r = self.run_migrate(keychain=(old, new))
                self.assertEqual((new.topics, old.deleted, r.moved), (None, 0, [str(self.legacy)]))
                self.assertEqual(self.lines, [Z("migrate.home.moved", old=self.legacy, new=self.new)])
                shutil.rmtree(self.new)

    def test_keychain_delete_failure_only_warns(self):
        self.make_legacy()
        old = LegacyMemoryStore(["agent-ntfy-a"], delete_error=StateError("keychain.delete_failed", service="X", rc=1, detail="boom"))
        new = MemoryStore()
        r = self.run_migrate(keychain=(old, new))
        self.assertEqual(new.topics, ["agent-ntfy-a"])
        self.assertEqual(r.moved, [str(self.legacy), "keychain"])
        self.assertEqual(len(r.warnings), 1)
        self.assertIn("boom", r.warnings[0])
        self.assertIn(f"security delete-generic-password -a {migrate.LEGACY_ACCOUNT} -s {migrate.LEGACY_SERVICE}", r.warnings[0])  # 可手抄的处理办法
        self.assertEqual(self.lines[1:], [Z("migrate.keychain.moved", old=migrate.LEGACY_SERVICE, new=state.KEYCHAIN_SERVICE), r.warnings[0]])

    def test_keychain_read_failure_only_warns_and_changes_nothing(self):
        for old, new in ((FailingLoadStore(), MemoryStore()), (LegacyMemoryStore(["agent-ntfy-a"]), FailingLoadStore())):
            with self.subTest(failing="old" if isinstance(old, FailingLoadStore) else "new"):
                self.make_legacy()
                self.lines.clear()
                r = self.run_migrate(keychain=(old, new))
                self.assertEqual(r.moved, [str(self.legacy)])
                self.assertEqual(len(r.warnings), 1)
                self.assertIn("locked", r.warnings[0])
                self.assertIn(migrate.LEGACY_ACCOUNT, r.warnings[0])  # 手动处理的提示：旧条目的服务名与账户
                self.assertIn(migrate.LEGACY_SERVICE, r.warnings[0])
                self.assertEqual(old.deleted, 0)
                shutil.rmtree(self.new)

    def test_keychain_write_failure_keeps_old_entry(self):
        class NoSave(MemoryStore):
            def save(self, topics):
                raise StateError("keychain.write_failed", service="X", rc=1, detail="denied")
        self.make_legacy()
        old = LegacyMemoryStore(["agent-ntfy-a"])
        r = self.run_migrate(keychain=(old, NoSave()))
        self.assertEqual((old.topics, old.deleted, r.moved), (["agent-ntfy-a"], 0, [str(self.legacy)]))
        self.assertEqual(len(r.warnings), 1)
        self.assertIn("denied", r.warnings[0])

    def test_default_keychain_pair_only_when_store_is_the_keychain(self):
        # 文件 / DPAPI 存储在 home 目录里，随目录一起搬，不需要钥匙串那一步
        with mock.patch.dict(os.environ, {"NTFY_CONNECTOR_STORE": "file"}):
            self.assertIsNone(migrate.default_keychain(self.new))
        with mock.patch.dict(os.environ, {"NTFY_CONNECTOR_STORE": "keychain"}), mock.patch.object(sys, "platform", "darwin"):
            pair = migrate.default_keychain(self.new)
        assert pair is not None
        old, new = pair
        self.assertIsInstance(old, state.KeychainStore)
        self.assertIsInstance(new, state.KeychainStore)
        assert isinstance(old, state.KeychainStore) and isinstance(new, state.KeychainStore)
        self.assertEqual((old.service, old.account), (migrate.LEGACY_SERVICE, migrate.LEGACY_ACCOUNT))
        self.assertEqual((new.service, new.account), (state.KEYCHAIN_SERVICE, state.KEYCHAIN_ACCOUNT))
        # 只有 macOS 上才构造：别的平台上 security 命令不存在，NTFY_CONNECTOR_STORE=keychain 会由 daemon 自己报错
        with mock.patch.dict(os.environ, {"NTFY_CONNECTOR_STORE": "keychain"}), mock.patch.object(sys, "platform", "linux"):
            self.assertIsNone(migrate.default_keychain(self.new))
        # 非法的 NTFY_CONNECTOR_STORE 不在这里报：瘦客户端用不到密钥存储，daemon 会报
        with mock.patch.dict(os.environ, {"NTFY_CONNECTOR_STORE": "bogus"}):
            self.assertIsNone(migrate.default_keychain(self.new))

    # ---- 环境变量

    def test_legacy_env_vars_are_reported_not_read(self):
        r = self.run_migrate(env={"AGENT_NTFY_URL": "secret-value", "NTFY_CONNECTOR_STORE": "file", "AGENT_NTFY_HOME": "/old", "PATH": "/bin"})
        self.assertEqual(len(r.warnings), 1)
        w = r.warnings[0]
        self.assertIn(Z("migrate.env.pair", old="AGENT_NTFY_HOME", new="NTFY_CONNECTOR_HOME"), w)
        self.assertIn(Z("migrate.env.pair", old="AGENT_NTFY_URL", new="NTFY_CONNECTOR_URL"), w)
        self.assertLess(w.index("AGENT_NTFY_HOME"), w.index("AGENT_NTFY_URL"))  # 按名字排序，输出稳定
        self.assertNotIn("secret-value", w)  # 只看键名，值一个字都不进文案
        self.assertNotIn("/old", w)
        self.assertNotIn("NTFY_CONNECTOR_STORE", w)
        self.assertEqual(self.lines, [w])
        self.assertEqual(r.moved, [])

    def test_no_legacy_env_vars_no_warning(self):
        r = self.run_migrate(env={"NTFY_CONNECTOR_URL": "x", "AGENT_NTFYX": "1"})
        self.assertEqual((r.warnings, self.lines), ([], []))

    def test_default_env_is_the_process_environment(self):
        with mock.patch.dict(os.environ, {"AGENT_NTFY_LANG": "zh"}):
            r = migrate.run(self.new, lang="zh", err=self.lines.append, legacy_home=self.legacy, new_home=self.new, keychain=(LegacyMemoryStore(), MemoryStore()), probe=lambda h: None)
        self.assertEqual(len(r.warnings), 1)
        self.assertIn("AGENT_NTFY_LANG", r.warnings[0])

    # ---- 一次跑完四步的顺序

    def test_steps_run_in_order_and_each_prints_one_line(self):
        self.make_legacy()
        old, new = LegacyMemoryStore(["agent-ntfy-a"]), MemoryStore()
        r = self.run_migrate(keychain=(old, new), env={"AGENT_NTFY_URL": "x"})
        self.assertEqual(r.moved, [str(self.legacy), "keychain"])
        self.assertEqual(len(r.warnings), 1)
        self.assertEqual(self.lines, [Z("migrate.home.moved", old=self.legacy, new=self.new),
                                      Z("migrate.keychain.moved", old=migrate.LEGACY_SERVICE, new=state.KEYCHAIN_SERVICE),
                                      r.warnings[0]])
        self.assertEqual(new.topics, ["agent-ntfy-a"])


class MigrateProjectStateTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="proj-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.old = self.root / migrate.LEGACY_PROJECT_DIR
        self.new = self.root / ".ntfy-connector"
        self.lines: list[str] = []

    def write(self, d: Path):
        d.mkdir()
        (d / "state.json").write_text('{"away": true}\n', encoding="utf-8")
        (d / ".gitignore").write_text("*\n", encoding="utf-8")

    def test_only_legacy_is_renamed(self):
        self.write(self.old)
        self.assertTrue(migrate.migrate_project_state(self.root, lang="zh", err=self.lines.append))
        self.assertFalse(self.old.exists())
        self.assertEqual(json.loads((self.new / "state.json").read_text(encoding="utf-8")), {"away": True})
        self.assertTrue((self.new / ".gitignore").exists())
        self.assertEqual(self.lines, [Z("migrate.project.moved", old=self.old, new=self.new)])
        self.lines.clear()
        self.assertFalse(migrate.migrate_project_state(self.root, lang="zh", err=self.lines.append))  # 可重入
        self.assertEqual(self.lines, [])

    def test_both_present_keeps_legacy_and_warns(self):
        self.write(self.old)
        self.write(self.new)
        self.assertFalse(migrate.migrate_project_state(self.root, lang="zh", err=self.lines.append))
        self.assertTrue((self.old / "state.json").exists())
        self.assertEqual(self.lines, [Z("migrate.project.both_exist", old=self.old, new=self.new)])

    def test_neither_present_is_silent(self):
        self.assertFalse(migrate.migrate_project_state(self.root, lang="zh", err=self.lines.append))
        self.assertEqual(self.lines, [])
        self.assertFalse(self.new.exists())  # 没启用过的项目不建目录

    def test_rename_failure_only_warns(self):
        self.write(self.old)
        with mock.patch.object(Path, "rename", side_effect=OSError(13, "Permission denied")):
            self.assertFalse(migrate.migrate_project_state(self.root, lang="zh", err=self.lines.append))
        self.assertTrue((self.old / "state.json").exists())
        self.assertEqual(self.lines, [Z("migrate.project.failed", old=self.old, new=self.new, error="[Errno 13] Permission denied")])


class CliWiringTest(unittest.TestCase):
    """真脚本子进程：任一子命令启动即迁移，--help 不触发，旧 daemon 在听退 4，解析出项目根的命令顺手迁项目状态目录。

    子进程的 ~ 是临时目录：旧目录 <tmp>/.agent-ntfy，目标固定 <tmp>/.ntfy-connector；--home 另给一个目录，验证数据不会跟着它走。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mig-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.legacy = self.tmp / ".agent-ntfy"
        self.target = self.tmp / ".ntfy-connector"
        self.home = self.tmp / "new"
        self.legacy.mkdir()
        (self.legacy / "leases.json").write_text("{}\n", encoding="utf-8")
        self.env = {k: v for k, v in os.environ.items() if k != "NTFY_CONNECTOR_SMOKE" and not k.startswith("AGENT_NTFY_")}
        # HOME / USERPROFILE 都指到临时目录：POSIX 与 Windows 的 ~ 各看一个
        self.env.update({"HOME": str(self.tmp), "USERPROFILE": str(self.tmp), "NTFY_CONNECTOR_STORE": "file", "NTFY_CONNECTOR_OFFLINE": "1",
                         "NTFY_CONNECTOR_LANG": "zh", "NTFY_CONNECTOR_URL": "http://127.0.0.1:1"})

    def cli(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=self.env, cwd=cwd, capture_output=True, encoding="utf-8", errors="replace", timeout=60)

    def test_any_subcommand_migrates_the_legacy_home_to_the_default_place(self):
        r = self.cli("--home", str(self.home), "daemon", "--status")
        self.assertEqual(r.returncode, 1, r.stderr)  # daemon 没跑：--status 的正常退出码；迁移在它之前已经做完
        self.assertFalse(self.legacy.exists())
        self.assertTrue((self.target / "leases.json").exists())
        self.assertFalse(self.home.exists())  # --home 指到哪都不会收到旧数据
        self.assertIn(Z("migrate.home.moved", old=self.legacy, new=self.target), r.stderr)

    def test_help_does_not_migrate(self):
        r = self.cli("--home", str(self.home), "--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.legacy / "leases.json").exists())
        self.assertFalse(self.target.exists())
        self.assertEqual(r.stderr, "")

    def test_legacy_daemon_listening_exits_4_and_moves_nothing(self):
        fake_legacy_daemon(self, self.legacy)
        r = self.cli("--home", str(self.home), "daemon", "--status")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn(Z("migrate.old_daemon_running", legacy_home=self.legacy), r.stderr)
        self.assertTrue((self.legacy / "leases.json").exists())
        self.assertFalse(self.target.exists())

    def test_legacy_daemon_on_the_other_transport_also_exits_4(self):
        via = other_transport()
        if via is None:
            self.skipTest("本平台只有一种传输")
        fake_legacy_daemon(self, self.legacy, via=via)
        r = self.cli("--home", str(self.home), "daemon", "--status")  # 子进程沿用本进程的传输，与旧目录里的那种不同
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertTrue((self.legacy / "leases.json").exists())
        self.assertFalse(self.target.exists())

    def test_home_pointing_at_the_legacy_dir_is_left_alone(self):
        r = self.cli("--home", str(self.legacy), "daemon", "--status")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(r.stderr, "")  # 不搬、不警告并存、不劝删
        self.assertTrue((self.legacy / "leases.json").exists())
        self.assertFalse(self.target.exists())

    def test_legacy_env_var_is_warned_about_on_stderr(self):
        self.env["AGENT_NTFY_URL"] = "https://ntfy.example/x"
        r = self.cli("--home", str(self.home), "daemon", "--status")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("AGENT_NTFY_URL", r.stderr)
        self.assertIn("NTFY_CONNECTOR_URL", r.stderr)
        self.assertNotIn("ntfy.example", r.stderr)

    def test_project_state_dir_migrates_when_a_command_resolves_the_project(self):
        def legacy_project(name):
            proj = self.tmp / name
            (proj / ".agent-ntfy").mkdir(parents=True)
            (proj / ".agent-ntfy" / "state.json").write_text('{"away": false}\n', encoding="utf-8")
            return proj

        def assert_migrated(proj, stderr):
            self.assertFalse((proj / ".agent-ntfy").exists())
            self.assertEqual(json.loads((proj / ".ntfy-connector" / "state.json").read_text(encoding="utf-8")), {"away": False})
            resolved = proj.resolve()  # 项目根是 resolve 过的（macOS 的 /var 是 /private/var 的链接）
            self.assertIn(Z("migrate.project.moved", old=resolved / ".agent-ntfy", new=resolved / ".ntfy-connector"), stderr)

        # 三条解析路径各验一次：project_root_or_none（slots）· away · confirm-sub；daemon 都没跑，命令本身退 3，改名在那之前
        proj = legacy_project("p1")
        r = self.cli("--home", str(self.home), "slots", cwd=str(proj))
        self.assertEqual(r.returncode, 3, r.stderr)
        assert_migrated(proj, r.stderr)
        r = self.cli("--home", str(self.home), "away", "status", cwd=str(proj))  # 已改名：不建第二个目录
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((proj / ".agent-ntfy").exists())
        proj = legacy_project("p2")
        r = self.cli("--home", str(self.home), "away", "status", cwd=str(proj))
        self.assertEqual(r.returncode, 0, r.stderr)
        assert_migrated(proj, r.stderr)
        proj = legacy_project("p3")
        r = self.cli("--home", str(self.home), "confirm-sub", "slot1", "--show-topic", cwd=str(proj))
        self.assertEqual(r.returncode, 3, r.stderr)
        assert_migrated(proj, r.stderr)


@unittest.skipUnless(os.environ.get("NTFY_CONNECTOR_SMOKE") == "1", "设 NTFY_CONNECTOR_SMOKE=1 才真调 security（会在钥匙串建临时条目，跑完删除）")
class KeychainMigrateSmokeTest(unittest.TestCase):
    """对真实钥匙串走一遍：旧条目有池子、新条目没有 → 迁移后新条目有、旧条目没了。服务名都是临时的。"""

    def test_old_entry_moves_to_new_entry(self):
        tag = secrets.token_hex(4)
        old_svc, new_svc = f"ntfy-connector-smoke-old-{tag}", f"ntfy-connector-smoke-new-{tag}"

        def cleanup():
            for svc in (old_svc, new_svc):
                subprocess.run(["security", "delete-generic-password", "-s", svc], capture_output=True, text=True)
                r = subprocess.run(["security", "find-generic-password", "-s", svc, "-w"], capture_output=True, text=True)
                self.assertEqual(r.returncode, state.KeychainStore.NOT_FOUND_RC, f"临时钥匙串条目 {svc} 没删干净，请手动 security delete-generic-password -s {svc}")
        self.addCleanup(cleanup)

        old = state.KeychainStore(service=old_svc, account=migrate.LEGACY_ACCOUNT)
        new = state.KeychainStore(service=new_svc)
        old.save(["agent-ntfy-smoke-a", "agent-ntfy-smoke-b"])
        self.assertIsNone(new.load())
        tmp = Path(tempfile.mkdtemp(prefix="mig-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        legacy = tmp / ".agent-ntfy"
        legacy.mkdir()  # 钥匙串一步只在旧目录还在时做
        lines: list[str] = []
        r = migrate.run(tmp / ".ntfy-connector", lang="zh", err=lines.append, legacy_home=legacy, new_home=tmp / ".ntfy-connector",
                        keychain=(old, new), env={}, probe=lambda h: None)
        self.assertEqual(r.moved, [str(legacy), "keychain"])
        self.assertEqual(r.warnings, [])
        self.assertEqual(new.load(), ["agent-ntfy-smoke-a", "agent-ntfy-smoke-b"])
        self.assertIsNone(old.load())


if __name__ == "__main__":
    unittest.main()
