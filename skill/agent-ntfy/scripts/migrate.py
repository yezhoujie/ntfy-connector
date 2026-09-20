"""老用户自动迁移：把 0.1.x（当时叫 agent-ntfy）留在这台机器上的东西搬到 ntfy-connector 的新名字下。

旧名字下的四样东西与各自的处置：
    用户目录  ~/.agent-ntfy（租约 / 端点 / 日志 / 文件或 DPAPI 形态的 topic 池）→ 整个 rename 成 ~/.ntfy-connector。
              目标固定是这个缺省位置，不是 --home / NTFY_CONNECTOR_HOME 指向的目录：随手 `--home /tmp/x status`
              不能把用户的数据搬进一个临时目录。--home 恰好就是 ~/.agent-ntfy（把旧环境变量机械改名的用户）⇒ 视为已迁，不动
    钥匙串    macOS 上的 generic password 条目（service AGENT_NTFY_TOPICS · account agent-ntfy）→ 读旧写新、再删旧。
              只在旧目录还在的那次运行里做：每条命令都 spawn security 太贵
    项目目录  <项目根>/.agent-ntfy/（远程模式的状态文件）→ rename 成 .ntfy-connector/
    环境变量  AGENT_NTFY_*  → 只警告、列出对应的新名字，值一个字都不读：旧值悄悄生效会让「明明没设」的配置起作用，没法排查

run() 由 CLI 入口在每条子命令解析完参数之后调一次（--help 到不了那里）；migrate_project_state() 由解析出项目根的命令各调一次。
两者都可重入：该搬的搬完之后再跑就是空操作；新旧并存时旧的一律不动，只警告——删东西的决定留给人。

守卫：旧目录里还有一个旧版 daemon 在听（探活有应答）就什么都不做、抛 LegacyDaemonRunning。它手上握着 topic 订阅与租约，
新 daemon 若同时起来会两边收同一条消息；目录被搬走后它的端点文件也跟着走，停都停不掉。探活按旧目录里实际存在的端点文件
逐种传输探（ipc.probe_any）：旧 daemon 用的传输由当年的环境变量决定，与本进程的 NTFY_CONNECTOR_IPC 无关。

topic 名与手机订阅不变：池子原样带过去，只是换了存放的名字。
"""

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import ipc
import projstate
import state
import texts
from state import KeychainStore, SecretStore, StateError

LEGACY_HOME_DIR_NAME = ".agent-ntfy"
LEGACY_HOME = Path("~").expanduser() / LEGACY_HOME_DIR_NAME  # 不看 AGENT_NTFY_HOME（旧值一律不读）
NEW_HOME = Path("~").expanduser() / ".ntfy-connector"  # 旧目录只搬到这里；不看 NTFY_CONNECTOR_HOME（与 --home 无关，见模块 docstring）
LEGACY_SERVICE = "AGENT_NTFY_TOPICS"
LEGACY_ACCOUNT = "agent-ntfy"
LEGACY_PROJECT_DIR = ".agent-ntfy"
LEGACY_ENV_PREFIX = "AGENT_NTFY_"
NEW_ENV_PREFIX = "NTFY_CONNECTOR_"
KEYCHAIN = "keychain"  # Result.moved 里代表「钥匙串条目」的那一项（其余项都是路径）


class LegacyDaemonRunning(Exception):
    """旧版 daemon 还在旧目录上听：什么都没迁。.legacy_home 是那个目录。"""

    def __init__(self, legacy_home: Path):
        super().__init__(f"legacy daemon is listening in {legacy_home}")  # 程序间的异常文案不进 texts 表，保持 ASCII
        self.legacy_home = legacy_home


class LegacyStore(Protocol):
    """旧钥匙串条目要能读、能删。SecretStore 接口没有 delete（文件 / DPAPI 存储随目录一起搬，用不着删），只有钥匙串实现有。"""

    def load(self) -> list[str] | None: ...

    def delete(self) -> None: ...


@dataclass
class Result:
    moved: list[str] = field(default_factory=list)  # 做了什么：搬走的旧路径 / KEYCHAIN
    warnings: list[str] = field(default_factory=list)  # 已经经 err 打出去的警告原文（调用方不用再打）


def _to_stderr(msg: str) -> None:
    print(msg, file=sys.stderr)


def default_keychain(home: Path) -> tuple[LegacyStore, SecretStore] | None:
    """只在 macOS 且密钥存储选的是钥匙串时才有钥匙串这一步；文件 / DPAPI 形态的池子在用户目录里，随目录一起搬。

    NTFY_CONNECTOR_STORE 的非法值不在这里报：瘦客户端用不到密钥存储，daemon 起动时会响亮报错。
    """
    if sys.platform != "darwin":
        return None
    try:
        new = state.default_store(home)
    except StateError:
        return None
    if not isinstance(new, KeychainStore):
        return None
    return KeychainStore(LEGACY_SERVICE, LEGACY_ACCOUNT), new


def run(home: Path, *, lang: str, err: Callable[[str], None] = _to_stderr, legacy_home: Path | None = None,
        new_home: Path | None = None, keychain: tuple[LegacyStore, SecretStore] | None = None,
        env: Mapping[str, str] | None = None, probe: Callable[[Path], object] | None = None) -> Result:
    """守卫 → 用户目录 → 钥匙串 → 环境变量，顺序固定。每做一步经 err 打一行；警告同样经 err 打，并收进 Result.warnings。

    home 是本次命令用的用户目录（已含 --home / NTFY_CONNECTOR_HOME），只用来判「是不是就是旧目录」：是 ⇒ 守卫与目录两步
    都跳过（视为已迁，也不打并存警告）。旧目录的去处是 new_home（缺省 ~/.ntfy-connector），与 home 无关。
    钥匙串一步只在本次运行开始时旧目录还在才做。legacy_home / new_home / keychain / env / probe 都可注入，缺省分别是
    ~/.agent-ntfy、~/.ntfy-connector、default_keychain(home)、os.environ、ipc.probe_any。
    """
    home = Path(home)
    legacy = LEGACY_HOME if legacy_home is None else Path(legacy_home)
    target = NEW_HOME if new_home is None else Path(new_home)
    result = Result()
    warn = _Warner(result, err, lang)
    legacy_present = legacy.exists()

    if legacy_present and not _same_path(legacy, home):
        if (ipc.probe_any if probe is None else probe)(legacy) is not None:
            raise LegacyDaemonRunning(legacy)
        if target.exists():
            warn("migrate.home.both_exist", old=legacy, new=target)
        else:
            try:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                legacy.rename(target)
            except OSError as e:
                warn("migrate.home.failed", old=legacy, new=target, error=e)
            else:
                result.moved.append(str(legacy))
                err(texts.t("migrate.home.moved", lang, old=legacy, new=target))

    if legacy_present:
        pair = default_keychain(home) if keychain is None else keychain
        if pair is not None:
            _migrate_keychain(pair, lang, err, result, warn)

    legacy_vars = sorted(k for k in (os.environ if env is None else env) if k.startswith(LEGACY_ENV_PREFIX))
    if legacy_vars:
        pairs = ", ".join(texts.t("migrate.env.pair", lang, old=k, new=NEW_ENV_PREFIX + k[len(LEGACY_ENV_PREFIX):]) for k in legacy_vars)
        warn("migrate.env.legacy_vars", pairs=pairs)
    return result


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:  # 相对路径而 cwd 已被删：算不出就当不是同一个，后面的命令会自己报 cwd 的问题
        return False


class _Warner:
    """按 key 取文案、经 err 打出去、同时收进 Result.warnings——警告只在这一处成形。"""

    def __init__(self, result: Result, err: Callable[[str], None], lang: str):
        self.result, self.err, self.lang = result, err, lang

    def __call__(self, key: str, **fmt) -> None:
        msg = texts.t(key, self.lang, **fmt)
        self.result.warnings.append(msg)
        self.err(msg)


def _migrate_keychain(pair: tuple[LegacyStore, SecretStore], lang: str, err: Callable[[str], None], result: Result, warn: _Warner) -> None:
    """新条目里没有池子、旧条目里有 ⇒ 写新、删旧。读或写失败只警告、两边都不动；删旧失败时新条目已写好、池子没丢，也只警告。

    文案里的服务名取常量而不取存储对象上的属性：注入替身时对象上没有它，而 default_keychain 构造出来的那一对用的正是这两个名字。
    """
    old, new = pair
    try:
        if new.load():
            return
        topics = old.load()
        if not topics:
            return
        new.save(topics)
    except StateError as e:
        warn("migrate.keychain.failed", old=LEGACY_SERVICE, new=state.KEYCHAIN_SERVICE, account=LEGACY_ACCOUNT, error=e)
        return
    result.moved.append(KEYCHAIN)
    err(texts.t("migrate.keychain.moved", lang, old=LEGACY_SERVICE, new=state.KEYCHAIN_SERVICE))
    try:
        old.delete()
    except StateError as e:
        warn("migrate.keychain.delete_failed", old=LEGACY_SERVICE, account=LEGACY_ACCOUNT, error=e)


def migrate_project_state(root: Path, *, lang: str, err: Callable[[str], None] = _to_stderr) -> bool:
    """<root>/.agent-ntfy/ → <root>/.ntfy-connector/：只有旧的才改名（返回 True）；两者并存或改名失败只警告；都没有就静默（不建目录）。"""
    old, new = Path(root) / LEGACY_PROJECT_DIR, projstate.state_dir(Path(root))
    if not old.is_dir():
        return False
    if new.exists():
        err(texts.t("migrate.project.both_exist", lang, old=old, new=new))
        return False
    try:
        old.rename(new)
    except OSError as e:
        err(texts.t("migrate.project.failed", lang, old=old, new=new, error=e))
        return False
    err(texts.t("migrate.project.moved", lang, old=old, new=new))
    return True
