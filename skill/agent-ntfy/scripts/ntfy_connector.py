#!/usr/bin/env python3
"""ntfy-connector：让任意 AI CLI 经 ntfy.sh 把「需要人拍板的事」推到手机，并把人的裁决带回来。

子命令:
    ask [--timeout 秒]（默认 43200，即 12 小时）
                            阻塞提问：从 stdin 读一段 JSON，推到手机，等到回复后把回复原文打到 stdout
    notify                  单向通知：从 stdin 读 {"title", "body"}，推一张无按钮的卡片到手机，发出即返回（不等回复、不占「提问中」）
    daemon [--detach|--status|--stop]
                            常驻订阅进程（唯一的 ntfy 订阅者）：前台跑 / 脱离会话跑 / 看状态 / 停掉
    slots                   看槽位池与租约状态
    release [<槽位>]        释放租约；不给槽位就释放本项目租的那个
    confirm-sub <槽位>      可达性确认闸：验「手机收得到通知」（在终端跑会显示 topic 名；agent 在 herdr 里代跑会自动开一个窗格）
        [--subscribed]        用户已订阅、跳过显示 topic 那段直接发测试通知（agent 代跑用，非终端也行）
        [--show-topic]        只打印 topic 名就退出，不发（⚠️ 会进调用方的输出）
        [--again]             已确认过的槽位重新确认（换手机后）
        [--timeout 秒]        等按钮点击的秒数（默认 600）
    add-slot                新建一个槽位（之后要 confirm-sub）

confirm-sub / release / add-slot 的退出码: 0 成功 / 1 槽位名不对 / 2 超时没点按钮（confirm-sub）/ 3 通道故障（daemon 没跑、状态文件读写失败、发布失败）
    / 4 需要人介入（confirm-sub 在非终端、没给 --subscribed 且不在 herdr 里或开不出窗格——在 herdr 里会开窗格退 0；confirm-sub 的槽位正忙）
    / 130 被 Ctrl-C 中断；release 撞上活跃槽位或无租约是 3

ask 的退出码（三种结局不能都表现为空输出）:
    0  拿到回复，stdout 是回复原文（末尾一个换行）
    1  输入校验未通过，消息未发送（stderr 有可照着改的文案）
    2  超时，消息已发送
    3  通道故障：daemon 没在跑 / 连接中途断开 / 向 ntfy 发布失败（stderr 写明是「未发送」还是「已发送但…」）
    4  需要人介入：槽位未过可达性闸 / 全部槽位已租用 / 该目标已有一个提问在等
  130  被 Ctrl-C 中断（stderr 仍说明消息发了没有）

notify 的退出码同 ask 的 0 / 1 / 3 / 4（0 = 已发出），没有 2——它不等回复。提问挂着时也能发。

除 daemon 外的子命令都是瘦客户端：经本机 IPC（unix socket；Windows 上是 127.0.0.1 上的 tcp + 口令，由 ipc 模块按平台选）
向 daemon 说话（一行 JSON 请求，若干行 JSON 事件），不读租约文件、不碰钥匙串。探活 / 停机也都走这条通路，不看 pid、不发信号。

租约主体是项目（git 仓根，否则 cwd）：同一项目里任意窗格 / 会话共用一个槽位。每次跑命令都把本项目租约的
注入窗格刷新成当前 herdr 窗格（不在 herdr 里 ⇒ 清空，手机消息走「未送达」回执）。卡片 Title 的 [<tag>] 是项目目录名。

环境变量: NTFY_CONNECTOR_HOME（默认 ~/.ntfy-connector）· HERDR_PANE_ID / HERDR_ENV（在 herdr 里时自动带上窗格标识）
          NTFY_CONNECTOR_TARGET（覆盖租约主体「我是谁」，同一个值复用同一个槽位；herdr 内外都生效）
          NTFY_CONNECTOR_LANG（固定文案的语言 zh / en；--lang 压过它；ask 的 JSON 里给了 lang 以它为准；都没有就看系统 locale，再缺省 en）

固定文案的语言只在这里解析一次（ask：JSON lang → --lang → NTFY_CONNECTOR_LANG → 系统 locale → en；其余子命令从 --lang 起同一条链），
随请求交给 daemon、开窗格 / detach 时用 --lang 带给子进程；深层模块不读环境变量。
"""

import argparse
import json
import locale
import os
import select
import socket
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import NamedTuple

import inject
import ipc
import migrate
import platform_
import projstate
import texts
import validate

PROG = "ntfy-connector"
HOME = Path(os.environ.get("NTFY_CONNECTOR_HOME", "~/.ntfy-connector")).expanduser()
DEFAULT_TIMEOUT = 12 * 3600
EXIT_REPLY, EXIT_INVALID, EXIT_TIMEOUT, EXIT_CHANNEL, EXIT_NEEDS_HUMAN, EXIT_INTERRUPTED = 0, 1, 2, 3, 4, 130
EXIT_SENT = EXIT_REPLY  # notify 的 0：发出去了（它不等回复）
# error 事件的 kind → 退出码
EXIT_BY_KIND = {"invalid_input": EXIT_INVALID, "unknown_slot": EXIT_INVALID, "busy": EXIT_NEEDS_HUMAN, "no_free_slot": EXIT_NEEDS_HUMAN,
                "unconfirmed": EXIT_NEEDS_HUMAN, "not_yours": EXIT_NEEDS_HUMAN}  # 其余 kind（state / publish_failed / daemon_stopping / bad_request …）都是通道故障 3
CONFIRM_TIMEOUT = 600  # 与 daemon.CONFIRM_TIMEOUT 同步（这里刻意不 import daemon）
DAEMON_START_TIMEOUT = 5.0  # away on 起 daemon 后等它在 socket 上应答的上限（秒）
PROBE_TIMEOUT = 2.0  # 单次探活的 socket 超时：daemon 已 bind 但还没进主循环（卡在初始化）时不能让调用方挂死
REQUEST_TIMEOUT = 60.0  # 一问一答命令等 daemon 回第一条事件的上限：daemon 接了连接却不应答时不能让调用方挂死；要 > ntfyclient.TIMEOUT（daemon 同步发布最坏等 30 秒）
STOP_TIMEOUT = 30.0  # --stop 等 daemon 退干净的总预算（关停最坏拖 2 + 5×N 秒：在途注入的宽限期 + 每张未送达回执的发布上限）
STATE_KEYS = ("unassigned", "idle", "active", "confirming")  # daemon 的 slots 事件里 state_key 的取值；显示文案按语言取


def err(msg: str) -> None:
    print(f"{PROG}: {msg}", file=sys.stderr)


class ProtocolError(ValueError):
    """daemon 回的东西不合协议；文案由 CLI 按语言取（cli.protocol.<key>）。"""

    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


class BadEnvLang(ValueError):
    """NTFY_CONNECTOR_LANG 给了却不是 zh / en。响亮失败，不静默回退——语言错了整个进程的文案都会错。"""


def _locale_lang() -> str | None:
    """系统 locale 是不是中文：LC_ALL / LC_MESSAGES / LANG 里第一个非空值以 zh 开头 ⇒ zh；三个都没有（Windows 常见）再看
    locale.getlocale()。不是中文 ⇒ None（交给缺省 en）。只在进程入口调一次。"""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value:
            return "zh" if value.lower().startswith("zh") else None
    try:
        name = (locale.getlocale()[0] or "").lower()
    except ValueError:
        name = ""
    return "zh" if name.startswith("zh") or "chinese" in name else None


def resolve_lang(explicit: str | None = None) -> str:
    """进程入口解析一次：--lang > NTFY_CONNECTOR_LANG > 系统 locale（中文 ⇒ zh）> en。ask / notify 再拿 JSON 里的 lang 压过它。
    环境变量空串当没给；给了非法值抛 BadEnvLang（--lang 的非法值由 argparse 拦）。"""
    if texts.is_lang(explicit):
        return str(explicit)
    value = os.environ.get(texts.ENV_VAR)
    if value and not texts.is_lang(value):
        raise BadEnvLang(value)
    if value:
        return value
    return _locale_lang() or texts.DEFAULT_LANG


def _prescan_lang(argv: list[str]) -> str | None:
    """在 argparse 之前把 --lang 挑出来：--help 的文案也要按它取。非法值这里不拦，留给 argparse 的 choices 报。"""
    for i, a in enumerate(argv):
        if a == "--lang" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--lang="):
            return a.split("=", 1)[1]
    return None


def describe(e: BaseException, lang: str) -> str:
    """异常进人读文案：协议错按语言取表，其余（OSError 之类）原样。"""
    return texts.t(f"cli.protocol.{e.key}", lang) if isinstance(e, ProtocolError) else str(e)


def start_hint(lang: str) -> None:
    print(texts.t("cli.start_hint", lang), file=sys.stderr)


# ---------------------------------------------------------------- herdr（CLI 侧）

def herdr_run(argv: list[str]) -> inject.HerdrResult:
    """CLI 侧所有 herdr 调用的唯一出口（测试在这里换替身，不真开窗格）。"""
    return inject.run_herdr(argv)


def herdr_available() -> bool:
    """能不能在 herdr 里开窗格：进程在 herdr 窗格内（两个环境变量都有）且 herdr CLI 真的通。环境变量不全就不去跑命令。"""
    if not (os.environ.get("HERDR_ENV") and os.environ.get("HERDR_PANE_ID")):
        return False
    return herdr_run([inject.HERDR, "pane", "list"]).ok


def run_self_in_new_pane(home: Path, lang: str, *subcommand: str) -> str | None:
    """在当前窗格下方开一个新窗格，在里面跑本程序的一个子命令（同一解释器、同一脚本、同一 --home、同一语言），返回新窗格 id，
    不等结果。开不出窗格 / 命令敲不进去就 None（后者会留下一个空窗格，herdr 没有从这里关它的办法）。

    新窗格是一个新 shell，不继承调用方的环境：语言用 --lang 显式带过去，否则窗格里的文案 / daemon 的缺省语言会按那个 shell
    自己的环境重新解析（实测过退回 en）。
    """
    new_pane = inject.split_pane(os.getcwd(), os.environ.get("HERDR_PANE_ID") or "", run=herdr_run)
    if new_pane is None:
        return None
    # --lang / --home 是顶层选项，必须放在子命令前面
    argv = [sys.executable, os.path.abspath(__file__), "--lang", lang, "--home", str(home), *subcommand]
    return new_pane if inject.run_in_pane(new_pane, argv, run=herdr_run) else None


def open_confirm_pane(home: Path, slot: str, lang: str, *, again: bool = False, timeout: float = CONFIRM_TIMEOUT) -> str | None:
    """在新窗格里跑默认形态的 confirm-sub（显示 topic → 等回车 → 发测试通知 → 等按钮），返回窗格 id，不等结果。
    topic 只出现在那个窗格里，不进本进程的输出。--again / --timeout 原样转进去（缺省的 timeout 不必带）。"""
    flags = (["--again"] if again else []) + (["--timeout", f"{timeout:g}"] if timeout != CONFIRM_TIMEOUT else [])
    # 自动开的窗格：结果注入回开它的这个窗格（--report-to），确认成功后再问一句要不要关掉（--close-pane）
    return run_self_in_new_pane(home, lang, "confirm-sub", slot, "--close-pane", "--report-to", os.environ["HERDR_PANE_ID"], *flags)


# ---------------------------------------------------------------- socket 协议（客户端侧）

def sock_path(home: Path = HOME) -> Path:
    """当前传输的监听端点文件（unix：daemon.sock；tcp：daemon.port），只用于报错文案。"""
    return ipc.endpoint_path(home)


def connect(home: Path = HOME) -> socket.socket:
    """连 daemon；连不上抛 OSError（调用方决定怎么说）。传输由 ipc 按环境 / 平台选。"""
    return ipc.connect(home)


def send_request(sock: socket.socket, req: dict, *, home: Path | None = None) -> None:
    """发一行请求。给了 home 就按传输补口令（tcp 每条连接的首行都要带）；同一连接后续的行（confirm-sub 的 ready）不必带。"""
    if home is not None:
        req = ipc.stamp(req, home)
    sock.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))


def read_events(sock: socket.socket) -> Iterator[dict]:
    """逐行读 daemon 回的事件，直到对端关连接。行不是 JSON 对象就当协议坏了，抛 ValueError。"""
    buf = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return
        buf += chunk
        while b"\n" in buf:
            line, _, buf = buf.partition(b"\n")
            if not line.strip():
                continue
            ev = json.loads(line.decode("utf-8"))
            if not isinstance(ev, dict):
                raise ProtocolError("not_object")
            yield ev


class Identity(NamedTuple):
    """谁在问、往哪注、卡片上标什么。"""

    leased_by: str  # 租约主体：项目 id（proj:<仓根>），NTFY_CONNECTOR_TARGET 可覆盖；同一项目的任何窗格 / 会话共用一个槽位
    pane: str | None  # 当前 herdr 窗格：手机消息注入到它；不在 herdr 里就是 None（注入走「未送达」回执）
    tag: str  # 卡片 Title 的 [<tag>]：项目目录名


def identity(root: Path | None = None) -> Identity:
    """租约主体是项目（git 仓根，否则 cwd）而不是窗格：同一项目里换窗格、换会话、重置上下文都还是它，
    不会每换一次就烧掉一个槽位。窗格另记（每次跑命令都刷新），只作注入目标。

    root 不给就现查；cwd 已被删时 project_root() 抛 OSError，原样抛出——命令入口用 project_root_or_none() 收成人读报错。
    """
    root = root or projstate.project_root()
    leased_by = os.environ.get("NTFY_CONNECTOR_TARGET") or f"proj:{root}"
    pane = (os.environ.get("HERDR_PANE_ID") or None) if os.environ.get("HERDR_ENV") else None
    return Identity(leased_by, pane, root.name or leased_by)  # 根目录名为空（/）时退回项目 id


def require_confirmed(root: Path | None = None) -> bool:
    """本项目开着远程交互模式 ⇒ 只能用已过闸的槽位：没人在键盘旁替一个新槽位过闸。"""
    root = root or projstate.project_root()
    return projstate.exists(root) and projstate.load(root).get("away") is True


def project_root_or_none(lang: str, *, not_sent: bool = False) -> Path | None:
    """定项目根；定不出来（cwd 已被删）就打一句人读报错、返回 None，调用方退 3——不能让 traceback 退 1 冒充「输入无效」。
    定出来了就顺手把旧版留下的 .agent-ntfy/ 改名成 .ntfy-connector/：状态文件要在这条命令读它之前就在新名字下。"""
    try:
        root = projstate.project_root()
    except OSError as e:
        message = texts.t("cli.project.unresolved", lang, error=e.strerror or e)
        err(texts.t("cli.ask.error_not_sent", lang, message=message) if not_sent else message)
        return None
    migrate.migrate_project_state(root, lang=lang, err=err)
    return root


# ---------------------------------------------------------------- ask

def cmd_ask(args) -> int:
    home = Path(args.home)
    text = sys.stdin.read()
    payload, problems, lang = validate.check_json(text, args.lang)  # 报错与这张卡片都用同一个语言：JSON lang → 进程入口解析的语言
    if problems:
        sys.stderr.write(validate.format_problems(problems, lang))
        return EXIT_INVALID
    root = project_root_or_none(lang, not_sent=True)
    if root is None:
        return EXIT_CHANNEL
    leased_by, pane, tag = identity(root)
    try:
        sock = connect(home)
    except OSError as e:
        err(texts.t("cli.connect_failed.not_sent", lang, path=sock_path(home), error=e.strerror or e))
        start_hint(lang)
        return EXIT_CHANNEL
    sent = False
    try:
        send_request(sock, {"cmd": "ask", "payload": payload, "leased_by": leased_by, "pane": pane, "tag": tag, "timeout": args.timeout, "lang": lang,
                            "require_confirmed": require_confirmed(root)}, home=home)
        for ev in read_events(sock):
            kind = ev.get("event")
            if kind == "sent":
                sent = True
                projstate.note(slot=ev.get("slot"), confirmed=True, target=leased_by)  # 发出去了 ⇒ 这个槽位已过闸
            elif kind == "warning":
                err(texts.t("cli.reminder", lang, message=ev.get("message")))
            elif kind == "reply":
                sys.stdout.write(str(ev.get("text", "")) + "\n")
                return EXIT_REPLY
            elif kind == "timeout":
                err(texts.t("cli.ask.timeout", lang, seconds=args.timeout))
                return EXIT_TIMEOUT
            elif kind == "error":
                sent = bool(ev.get("sent", sent))
                return report_send_error(ev, lang, leased_by, sent=sent)
        err(texts.t("cli.ask.disconnected", lang) + texts.t("cli.ask.disconnected.sent" if sent else "cli.ask.disconnected.not_sent", lang))
        return EXIT_CHANNEL
    except (OSError, ValueError) as e:
        err(texts.t("cli.ask.comm_failed", lang, error=describe(e, lang)) + texts.t("cli.ask.comm_failed.sent" if sent else "cli.ask.disconnected.not_sent", lang))
        return EXIT_CHANNEL
    except KeyboardInterrupt:
        err(texts.t("cli.ask.interrupted", lang) + texts.t("cli.ask.interrupted.sent" if sent else "cli.ask.disconnected.not_sent", lang))
        return EXIT_INTERRUPTED
    finally:
        sock.close()


def _holder_state(h: dict, lang: str) -> str:
    key = "cli.ask.holder.active" if h.get("active") else "cli.ask.holder.idle"
    gate = "" if h.get("subscribed") else texts.t("cli.ask.holder.unconfirmed", lang)
    return texts.t(key, lang) + gate


def report_send_error(ev: dict, lang: str, leased_by: str, *, sent: bool) -> int:
    """ask / notify 收到 error 事件：打人读文案（先说发没发出去），按 kind 回写状态文件、列候选，返回退出码。"""
    err(texts.t("cli.ask.error_sent" if sent else "cli.ask.error_not_sent", lang, message=ev.get("message")))
    if ev.get("kind") == "unconfirmed" and ev.get("slot"):
        projstate.note(slot=ev["slot"], confirmed=False, target=leased_by)  # 租到了但没过闸：让 agent 知道该确认哪个
    if ev.get("kind") == "no_free_slot":
        for h in ev.get("holders") or []:
            err(texts.t("cli.ask.holder", lang, slot=h["slot"], holder=h["leased_by"], state=_holder_state(h, lang)))
    return EXIT_BY_KIND.get(str(ev.get("kind")), EXIT_CHANNEL)


# ---------------------------------------------------------------- notify

def cmd_notify(args) -> int:
    """单向通知：校验 → 身份 → 一问一答。发出即返回 0；不等回复，所以没有超时一说。"""
    home = Path(args.home)
    payload, problems, lang = validate.check_notify_json(sys.stdin.read(), args.lang)
    if problems:
        sys.stderr.write(validate.format_problems(problems, lang, command="notify"))
        return EXIT_INVALID
    root = project_root_or_none(lang, not_sent=True)
    if root is None:
        return EXIT_CHANNEL
    leased_by, pane, tag = identity(root)
    ev = request(home, {"cmd": "notify", "payload": payload, "leased_by": leased_by, "pane": pane, "tag": tag, "require_confirmed": require_confirmed(root)},
                 lang, not_sent=True)
    if ev is None:
        return EXIT_CHANNEL
    if ev.get("event") == "sent":
        print(texts.t("cli.notify.sent", lang, slot=ev.get("slot")))
        projstate.note(slot=ev.get("slot"), confirmed=True, target=leased_by)  # 发出去了 ⇒ 这个槽位已过闸
        return EXIT_SENT
    return report_send_error(ev, lang, leased_by, sent=bool(ev.get("sent")))


# ---------------------------------------------------------------- 一问一答的命令

def request(home: Path, req: dict, lang: str, *, not_sent: bool = False) -> dict | None:
    """发一个一问一答的命令（带上语言），返回第一条事件；连不上 daemon / 对话中途出错 / 回的不合协议都返回 None（已打印提示）。
    not_sent：这条命令会发消息（notify），失败提示里要点明「消息未发送」。"""
    try:
        with connect(home) as s:
            s.settimeout(REQUEST_TIMEOUT)
            send_request(s, {**req, "lang": lang}, home=home)
            return next(read_events(s), {"event": "error", "kind": "protocol", "sent": False, "message": texts.t("cli.no_response", lang)})
    except TimeoutError:  # 连上了但 daemon 不应答（卡在主循环外）：按通信失败报，不是「连不上」
        err(texts.t("cli.ask.comm_failed", lang, error=texts.t("cli.no_response", lang)) + (texts.t("cli.ask.disconnected.not_sent", lang) if not_sent else ""))
        return None
    except OSError as e:
        err(texts.t("cli.connect_failed.not_sent" if not_sent else "cli.connect_failed", lang, path=sock_path(home), error=e.strerror or e))
        start_hint(lang)
        return None
    except ValueError as e:  # 回的不是 JSON 对象（ProtocolError 也是它的子类）：通道故障，不是输入错，也不能是 traceback
        err(texts.t("cli.ask.comm_failed", lang, error=describe(e, lang)) + (texts.t("cli.ask.disconnected.not_sent", lang) if not_sent else ""))
        return None


def cmd_slots(args) -> int:
    lang = args.lang
    root = project_root_or_none(lang)
    if root is None:
        return EXIT_CHANNEL
    ident = identity(root)
    ev = request(Path(args.home), {"cmd": "slots", "leased_by": ident.leased_by, "pane": ident.pane}, lang)  # 带身份：顺手刷新本项目租约的窗格
    if ev is None:
        return EXIT_CHANNEL
    if ev.get("event") != "slots":
        err(str(ev.get("message")))
        return EXIT_CHANNEL
    width = max(8, *(len(texts.t(f"cli.slots.state.{k}", lang)) for k in STATE_KEYS))  # 列宽装得下该语言最长的状态词
    for slot, rec in ev["slots"].items():
        gate = texts.t("cli.slots.gate.confirmed" if rec.get("subscribed") else "cli.slots.gate.unconfirmed", lang)
        who = texts.t("cli.slots.since", lang, leased_by=rec["leased_by"], leased_at=rec["leased_at"]) if rec.get("leased_by") else ""  # 主体打完整值：可读且可复制
        pane = texts.t("cli.slots.pane", lang, pane=rec["pane"]) if rec.get("pane") else ""
        state = texts.t(f"cli.slots.state.{rec['state_key']}", lang) if rec.get("state_key") in STATE_KEYS else rec["state"]
        print(f"{slot:<7} {state:<{width}} {gate}{who}{pane}")
    return 0


def cmd_release(args) -> int:
    lang = args.lang
    if args.slot:
        # 指名释放：带上本项目身份让 daemon 核归属——别的项目的租约不能释放，要由用户去那个项目关远程模式
        root = project_root_or_none(lang)
        if root is None:
            return EXIT_CHANNEL
        req = {"cmd": "release", "slot": args.slot, "leased_by": identity(root).leased_by}
    else:
        root = project_root_or_none(lang)  # 不给槽位 = 释放本项目租的那个
        if root is None:
            return EXIT_CHANNEL
        ident = identity(root)
        req = {"cmd": "release", "leased_by": ident.leased_by, "pane": ident.pane}
    ev = request(Path(args.home), req, lang)
    if ev is None:
        return EXIT_CHANNEL
    if ev.get("event") != "released":
        err(str(ev.get("message")))
        return EXIT_BY_KIND.get(str(ev.get("kind")), EXIT_CHANNEL)
    print(texts.t("cli.released", lang, slot=ev["slot"]))
    projstate.note_released(ev["slot"], explicit=bool(args.slot))
    return 0


def offer_close_pane(lang: str) -> None:
    """自动开的确认窗格里、确认成功之后：问一句要不要关掉这个窗格（回车 / y 关，其余保留），让屏幕不被用完的窗格占着。
    只在 herdr 窗格里有意义；不在窗格里（手动跑的却带了 --close-pane）就什么都不做。
    关的是 HERDR_PANE_ID 指的窗格：herdr 给每个窗格的 shell 各自设这个变量，pane run 敲进新窗格的命令读到的是新窗格自己的 id
    （herdr 0.9.0 实测：split 出 wG:pH 后在里面 echo 得 wG:pH），不会误关开它的那个窗格。"""
    pane = os.environ.get("HERDR_PANE_ID") if os.environ.get("HERDR_ENV") else None
    if not pane:
        return
    try:
        answer = input(texts.t("cli.confirm.close_pane", lang)).strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
    if answer in ("", "y", "yes"):
        r = herdr_run([inject.HERDR, "pane", "close", pane])
        if not r.ok:
            err(texts.t("cli.confirm.pane_close_failed", lang, why=r.summary(lang)))
    else:
        print(texts.t("cli.confirm.pane_kept", lang))


def report_confirm_result(pane: str, slot: str, rc: int, lang: str) -> None:
    """把 confirm-sub 的结果注入回开窗格的那个 agent 会话（带系统事件前缀，与手机消息同一条通道）。注入失败只打一行 stderr，
    不改退出码：窗格里的人还能看到结果，agent 也还能用 slots 查。"""
    key = {0: "cli.confirm.report.confirmed", EXIT_TIMEOUT: "cli.confirm.report.timeout", EXIT_INTERRUPTED: "cli.confirm.report.cancelled"}.get(rc, "cli.confirm.report.failed")
    line = inject.SYSTEM_PREFIX + texts.t(key, lang, slot=slot, rc=rc)
    kind, _cli, why = inject.push_line(pane, line, run=herdr_run, lang=lang)
    if kind != "delivered":
        err(texts.t("cli.confirm.report_failed", lang, pane=pane, why=why or texts.t("receipt.pane_missing", lang, target=pane)))


def cmd_confirm_sub(args) -> int:
    try:
        migrate.migrate_project_state(projstate.project_root(), lang=args.lang, err=err)  # 过闸后回写状态文件之前先把旧目录改名
    except OSError:
        pass  # cwd 没了：回写那一步（note_confirmed）同样会静默跳过，这里不必报
    rc = _confirm_sub(args)
    if args.show_topic:
        return rc
    interactive = sys.stdout.isatty()
    if args.report_to and interactive:
        # 自动开的窗格里：先把结果送回开它的 agent，再谈关窗格（关了本进程就没了）。agent 自己非 TTY 跑到这里（开窗格那次调用
        # 已经返回、结果由窗格里那次回报）不重复报
        report_confirm_result(args.report_to, args.slot, rc, args.lang)
    if rc == 0 and args.close_pane and interactive:
        # 只在交互式（自动开的窗格里 stdout 就是终端）才问：agent 自己非 TTY 跑到这里时 HERDR_PANE_ID 是它自己的窗格，问了就是关它自己
        offer_close_pane(args.lang)
    return rc


def _confirm_sub(args) -> int:
    """可达性确认闸。默认两段：先显示 topic 让用户订阅、按回车后才发测试通知，等用户在通知栏点按钮。

    topic 名就是密码：默认只在 stdout 是终端时才显示。agent 代跑（stdout 被捕获）时：在 herdr 里就开一个新窗格让用户在那里
    走这两段、本进程打印窗格 id 退 0；不在 herdr 里或开不出窗格才退 4 让它转告用户。
    用户已订阅过时 agent 可以带 --subscribed 代跑（不经过显示 topic 那段）。
    """
    home, slot, lang = Path(args.home), args.slot, args.lang
    if args.show_topic:
        ev = request(home, {"cmd": "confirm-sub", "slot": slot, "show_topic": True}, lang)
        if ev is None:
            return EXIT_CHANNEL
        if ev.get("event") != "topic":
            err(str(ev.get("message")))
            return EXIT_BY_KIND.get(str(ev.get("kind")), EXIT_CHANNEL)
        print(texts.t("cli.confirm.topic", lang, slot=slot, topic=ev["topic"], url=ev["url"]))
        return 0
    if not args.subscribed and not sys.stdout.isatty():
        # agent 代跑（stdout 被捕获）：在 herdr 里就开一个新窗格让用户在那里走默认形态，本进程立即返回；开不了仍退 4 让它转告用户。
        # 开窗格之前先照 daemon 的 slots 视图核一遍：槽位不存在就按输入错退 1（与 daemon 的 unknown_slot 同款文案，不走下面
        # 会把 topic 打到被捕获的 stdout 的流程）；已过闸又没说 --again 就直接答「已确认过」——别开一个只会立刻退出的窗格
        if not herdr_available():
            err(texts.t("cli.confirm.topic_hint", lang, slot=slot))
            return EXIT_NEEDS_HUMAN
        ev = request(home, {"cmd": "slots"}, lang)
        if ev is None:
            return EXIT_CHANNEL
        if ev.get("event") != "slots":
            err(str(ev.get("message")))
            return EXIT_BY_KIND.get(str(ev.get("kind")), EXIT_CHANNEL)
        rec = ev["slots"].get(slot)
        if rec is None:
            err(texts.t("daemon.unknown_slot", lang, slot=repr(slot)))
            return EXIT_INVALID
        if rec.get("subscribed") and not args.again:
            print(texts.t("cli.confirm.already", lang, slot=slot))
            projstate.note_confirmed(slot)
            return 0
        pane = open_confirm_pane(home, slot, lang, again=args.again, timeout=args.timeout)
        if pane is None:
            err(texts.t("cli.confirm.topic_hint", lang, slot=slot))
            return EXIT_NEEDS_HUMAN
        print(texts.t("cli.confirm.pane_opened", lang, slot=slot, pane=pane))
        return 0
    try:
        sock = connect(home)
    except OSError as e:
        err(texts.t("cli.connect_failed", lang, path=sock_path(home), error=e.strerror or e))
        start_hint(lang)
        return EXIT_CHANNEL
    sent = False
    try:
        send_request(sock, {"cmd": "confirm-sub", "slot": slot, "again": args.again, "subscribed": args.subscribed, "timeout": args.timeout, "lang": lang}, home=home)
        for ev in read_events(sock):
            kind = ev.get("event")
            if kind == "already_confirmed":
                print(texts.t("cli.confirm.already", lang, slot=slot))
                projstate.note_confirmed(slot)
                return 0
            elif kind == "topic":
                if args.subscribed:
                    err(texts.t("cli.confirm.protocol", lang))  # 两端各守一道
                    return EXIT_CHANNEL
                print(texts.t("cli.confirm.topic", lang, slot=slot, topic=ev["topic"], url=ev["url"]) + "\n" + texts.t("cli.confirm.guide", lang))
                try:
                    input(texts.t("cli.confirm.enter", lang))
                except EOFError:
                    # stdin 到头（< /dev/null 之类）：没等到回车就不能发——先发再订阅正是两段式要避免的
                    err(texts.t("cli.confirm.no_enter", lang))
                    return EXIT_NEEDS_HUMAN
                # 等回车期间 daemon 可能已经收掉这条（超时 / 停止）并关了连接，真实终态在缓冲区里。发之前先探一眼：
                # socket 已可读（终态事件或 EOF 已到）就不发，让读循环去读它——Windows 上向对端已关的连接 send 会把连接
                # abort（WSAECONNABORTED），缓冲区里的终态跟着丢、之后的 recv 也失败；POSIX 上只是 EPIPE 被吞、事件还在。
                # 先探再发在三个平台上都确定；探过之后才关的那条极窄窗口仍走下面的 except。
                # 「可读 ⇒ 终态或 EOF」靠的是 daemon 在 topic 段不往这条连接发任何非终态事件（_warn_pending 与收到文字时的
                # warning 都以 msg_id 为门，sent / warning 都在 ready 之后）——那边若放宽，这里会把 ready 静默吞掉
                if not select.select([sock], [], [], 0)[0]:
                    try:
                        send_request(sock, {"ready": True})
                    except OSError:
                        pass  # 探完才关的：真实终态还在缓冲区里，继续读它，别报成通道故障
            elif kind == "sent":
                sent = True
                err(texts.t("cli.confirm.sent", lang, button=texts.t("confirm.button", lang), seconds=f"{args.timeout:g}"))  # 进度走 stderr：stdout 只在退出 0 时有内容
            elif kind == "warning":
                err(texts.t("cli.reminder", lang, message=ev.get("message")))
            elif kind == "confirmed":
                print(texts.t("cli.confirm.done", lang, slot=slot))
                projstate.note_confirmed(slot)
                if not args.report_to and sys.stdout.isatty():
                    # 用户自己在终端跑的：没有窗格会替他把结果送回 agent，给他一句可以直接发给 agent 的话
                    print(texts.t("cli.confirm.done_hint", lang, prompt=texts.t("cli.confirm.done_prompt", lang, slot=slot)))
                return 0
            elif kind == "timeout":
                err(texts.t("cli.confirm.timeout" if sent else "cli.confirm.timeout_no_enter", lang, seconds=f"{args.timeout:g}"))
                return EXIT_TIMEOUT
            elif kind == "error":
                err(str(ev.get("message")))
                return EXIT_BY_KIND.get(str(ev.get("kind")), EXIT_CHANNEL)
        err(texts.t("cli.confirm.disconnected", lang))
        return EXIT_CHANNEL
    except (OSError, ValueError) as e:
        err(texts.t("cli.confirm.comm_failed", lang, error=describe(e, lang)))
        return EXIT_CHANNEL
    except KeyboardInterrupt:
        err(texts.t("cli.confirm.interrupted", lang))
        return EXIT_INTERRUPTED
    finally:
        sock.close()


def cmd_add_slot(args) -> int:
    lang = args.lang
    ev = request(Path(args.home), {"cmd": "add-slot"}, lang)
    if ev is None:
        return EXIT_CHANNEL
    if ev.get("event") != "added":
        err(str(ev.get("message")))
        return EXIT_BY_KIND.get(str(ev.get("kind")), EXIT_CHANNEL)
    print(texts.t("cli.add_slot.done", lang, slot=ev["slot"]))
    return 0


# ---------------------------------------------------------------- away：项目级状态文件

def cmd_away(args) -> int:
    """远程交互模式开关。状态落在项目根 .ntfy-connector/state.json（不含 topic 名），给 agent 在任何会话里读。

    on 是一站式：daemon 没跑就起（herdr 里开窗格起，否则脱离会话起）→ 保证有能用的槽位（没有已过闸的就开确认窗格）→ 才写文件。
    status 以 daemon 为准校对：经 slots 反查本项目真实租着哪个槽位，与文件不一致就改写并提示；daemon 没跑就照旧读文件、标「未校对」。
    没启用过的项目不探活也不校对——校对会写文件，而没启用的项目不该被建目录。
    """
    lang = args.lang
    try:
        root = projstate.project_root()
        migrate.migrate_project_state(root, lang=lang, err=err)  # 与 project_root_or_none 同一件事：读状态文件之前先把旧目录改名
        if args.action == "on":
            return away_on(Path(args.home), root, lang)
        if args.action == "off":
            if not projstate.exists(root):
                print(texts.t("cli.away.not_enabled", lang))  # 没开过就没什么可关的，也不留目录
                return 0
            home = Path(args.home)
            ident = identity(root)
            daemon_up = probe(home) is not None  # daemon 在跑才去释放；没跑就只关开关（租约留着，文件里的 slot 也留着——它仍是事实）
            if daemon_up and projstate.load(root).get("away") is True:
                # 先通知手机、再释放：槽位一释放就可能立刻被别的项目拿走，通知会发不出去或发错地方
                _send_away_notice(home, ident, lang, title=texts.t("away.off.title", lang), body=texts.t("away.off.body", lang))
            released, lease_gone = None, False
            if daemon_up:
                ev = request(home, {"cmd": "release", "leased_by": ident.leased_by}, lang)  # 不带 pane：关模式不刷新注入窗格
                if ev is not None and ev.get("event") == "released":
                    released, lease_gone = str(ev["slot"]), True
                elif ev is not None and ev.get("kind") == "no_lease":
                    lease_gone = True
                elif ev is not None:
                    err(str(ev.get("message")))  # 释放不了（提问挂着 / 确认中 / 状态层坏了）：开关照关，租约与文件里的 slot 都留着
            fields = {"slot": None, "confirmed": None} if lease_gone else {}
            st = projstate.save(root, away=False, target=ident.leased_by, **fields)
            print(texts.t("cli.away.state.off", lang))
            if released:
                print(texts.t("cli.away.off.released", lang, slot=released))
            print(texts.t("cli.away.path", lang, path=projstate.state_path(root)))
            return 0
        enabled = projstate.exists(root)
        st, corrected, unverified = projstate.load(root) if enabled else {}, False, None
        if enabled:
            ident = identity(root)
            ev = request(Path(args.home), {"cmd": "slots", "leased_by": ident.leased_by, "pane": ident.pane}, lang)
            if ev is None:
                unverified = "cli.away.unverified"  # daemon 没跑（request 已打印连不上 + 启动方式）
            elif ev.get("event") != "slots":
                err(str(ev.get("message")))  # daemon 在跑但答不出租约（状态层坏了）：说明原因
                unverified = "cli.away.unverified.error"
            else:
                st, corrected = projstate.reconcile(root, ident.leased_by, ev["slots"])
    except OSError as e:
        err(texts.t("cli.away.io_failed", lang, error=describe(e, lang)))
        return EXIT_CHANNEL
    if args.json:
        print(json.dumps(st, ensure_ascii=False))
        return 0
    if not enabled:
        print(texts.t("cli.away.not_enabled", lang))
        return 0
    print(texts.t("cli.away.state.on" if st.get("away") else "cli.away.state.off", lang))
    if st.get("slot"):
        gate = texts.t("cli.slots.gate.confirmed" if st.get("confirmed") else "cli.slots.gate.unconfirmed", lang)
        print(texts.t("cli.away.slot", lang, slot=st["slot"], gate=gate))
    else:
        print(texts.t("cli.away.slot.none", lang))
    if st.get("target"):
        print(texts.t("cli.away.target", lang, target=st["target"]))
    if st.get("updated"):
        print(texts.t("cli.away.updated", lang, updated=st["updated"]))
    print(texts.t("cli.away.path", lang, path=projstate.state_path(root)))
    if corrected:
        print(texts.t("cli.away.corrected", lang))
    if unverified:
        print(texts.t(unverified, lang))
    return 0


def _state_dir_writable(root: Path) -> bool:
    """状态目录能不能写：已存在 ⇒ 必须是目录且可写；不存在 ⇒ 父目录可写。只探不建——之后退 3 / 4 时不能留下一个空目录让「已启用」误判。"""
    d = projstate.state_dir(root)
    if d.exists():
        return d.is_dir() and os.access(d, os.W_OK)
    return os.access(root, os.W_OK)


def _ensure_daemon(home: Path, lang: str) -> bool:
    """daemon 保障：探不到就脱离会话起一个，等它在 socket 上应答。"""
    deadline = time.monotonic() + DAEMON_START_TIMEOUT  # 整个保障过程（含第一次探活）的总预算
    if probe(home, timeout=min(PROBE_TIMEOUT, DAEMON_START_TIMEOUT)) is not None:
        return True
    proc = _spawn_daemon(home, lang)
    while (remaining := deadline - time.monotonic()) > 0:
        time.sleep(0.1)
        if proc.poll() is not None:  # 子进程已经退了：再等也不会有应答，报它的退出码
            err(texts.t("cli.detach.died", lang, rc=proc.poll(), log=home / "daemon.log"))
            return False
        if probe(home, timeout=min(PROBE_TIMEOUT, remaining)) is not None:  # 单次探活不许把总预算撑长
            return True
    err(texts.t("cli.away.daemon_failed", lang, seconds=f"{DAEMON_START_TIMEOUT:g}", log=home / "daemon.log"))
    return False


def _slot_number(slot: str) -> int:
    digits = slot.removeprefix("slot")
    return int(digits) if digits.isdigit() else 0


def _confirm_or_point(home: Path, slot: str, slots: dict, lang: str) -> bool:
    """槽位保障的收口：该槽位正在确认中就指去已开的窗格；否则在 herdr 里开确认窗格；开不了就退 4 指路 confirm-sub。返回是否可以继续写文件。"""
    if slots[slot].get("state_key") == "confirming":
        print(texts.t("cli.away.confirming", lang, slot=slot))
        return True
    pane = open_confirm_pane(home, slot, lang) if herdr_available() else None
    if pane is None:
        err(texts.t("cli.confirm.topic_hint", lang, slot=slot))
        return False
    print(texts.t("cli.away.confirm_pane", lang, slot=slot, pane=pane))
    return True


def away_on(home: Path, root: Path, lang: str) -> int:
    """一站式开启：① 状态目录可写 ② daemon 在跑 ③ 当场给本项目租一个槽位（已有就沿用；已过闸的优先，没有就租一个未过闸的并接着走确认）
    ④ 写文件。①②③ 任一没过就退 3 / 4，文件与目录都不动。
    要走的人此刻还在键盘旁：租约与过闸都在这一步落定，别拖到第一次提问才发现池满或没过闸——那时候没人能处理。"""
    if not _state_dir_writable(root):
        err(texts.t("cli.away.io_failed", lang, error=texts.t("cli.away.unwritable", lang, path=projstate.state_dir(root))))
        return EXIT_CHANNEL
    if not _ensure_daemon(home, lang):
        return EXIT_CHANNEL
    ident = identity(root)
    ev = request(home, {"cmd": "lease", "leased_by": ident.leased_by, "pane": ident.pane}, lang)
    if ev is None:
        return EXIT_CHANNEL
    if ev.get("event") != "leased":
        err(str(ev.get("message")))
        if ev.get("kind") == "no_free_slot":
            # 全部已租：列出占用情况（措辞与 ask 撞满时一致），由用户决定去哪个项目关模式、还是新建槽位
            for h in ev.get("holders") or []:
                err(texts.t("cli.ask.holder", lang, slot=h["slot"], holder=h["leased_by"], state=_holder_state(h, lang)))
        return EXIT_BY_KIND.get(str(ev.get("kind")), EXIT_CHANNEL)
    slot, subscribed = str(ev["slot"]), bool(ev.get("subscribed"))
    if subscribed:
        print(texts.t("cli.away.ready", lang, slot=slot))
    else:
        ev2 = request(home, {"cmd": "slots"}, lang)  # 只为看它是不是正在确认中（上一次开的窗格还没走完）
        slots: dict = ev2["slots"] if ev2 and ev2.get("event") == "slots" else {slot: {}}
        if not _confirm_or_point(home, slot, slots, lang):
            return EXIT_NEEDS_HUMAN  # 租约留着（人在键盘旁，跑完 confirm-sub 再 away on 就是它）；开关不写
    projstate.save(root, away=True, slot=slot, confirmed=subscribed, target=ident.leased_by)
    print(texts.t("cli.away.path", lang, path=projstate.state_path(root)))
    if subscribed:
        # 走 _confirm_or_point 那一支时还没「开成了」（手机没订阅上，通知也送不到）：不发
        _send_away_notice(home, ident, lang, title=texts.t("away.on.title", lang), body=_away_on_body(home, lang))
    _warn_if_daemon_has_no_herdr(home, lang)
    return 0


def _away_on_body(home: Path, lang: str) -> str:
    """开启通知的正文三选一：不在 herdr 里就说明只有对提问的回复能回到 agent；
    在 herdr 里但 daemon 自己视角找不到 herdr（多半装机顺序反了）就建议重启；两者都过才说消息能直接送进终端。

    probe(home) 探不到状态（daemon 瞬时没应答）时也按「找不到 herdr」处理，这里刻意不学 _warn_if_daemon_has_no_herdr
    那样遇到探不到就干脆不说话——那条只是一句可有可无的诊断，静默无妨；这条正文没有第三种「不确定」的说法可选，
    错判成「找得到」会让手机消息在用户不知情的情况下送不到，两害相权，宁可多提醒一次重启。"""
    if os.environ.get("HERDR_ENV") != "1":
        return texts.t("away.on.body.no_herdr", lang)
    status = probe(home)
    if status is not None and status.get("herdr", {}).get("bin") is not None:
        return texts.t("away.on.body.full", lang)
    return texts.t("away.on.body.daemon_no_herdr", lang)


def _send_away_notice(home: Path, ident: Identity, lang: str, *, title: str, body: str) -> None:
    """开 / 关远程模式时尽力推一条通知给手机；两处调用都只在槽位已经过闸之后才会走到这里，直接按已过闸请求。
    发不出去不改变调用方的结果：吞掉失败，只在 stderr 留一行诊断。"""
    ev = request(home, {"cmd": "notify", "payload": {"title": title, "body": body}, "leased_by": ident.leased_by,
                        "pane": ident.pane, "tag": ident.tag, "require_confirmed": True}, lang, not_sent=True)
    if ev is None or ev.get("event") != "sent":
        why = ev.get("message") if ev else texts.t("cli.no_response", lang)
        err(texts.t("cli.away.not_notified", lang, why=why))


def _warn_if_daemon_has_no_herdr(home: Path, lang: str) -> None:
    """远程模式刚开起来：这台 agent 在 herdr 里，却是 daemon 自己 PATH 上找不到 herdr（多半装机顺序反了）——
    手机消息这就送不到，让 agent 知道该重启 daemon。只在这一种组合下才值得说：不在 herdr 里没处重启，
    也没有窗格可指给它看。退出码不受影响：远程模式已经开成了，这只是一句诊断。"""
    if os.environ.get("HERDR_ENV") != "1":
        return
    status = probe(home)
    if status is not None and status.get("herdr", {}).get("bin") is None:
        err(texts.t("cli.away.daemon_no_herdr", lang))


# ---------------------------------------------------------------- daemon 的起停

def cmd_daemon(args) -> int:
    import daemon  # 只有这一支需要它（连带钥匙串与租约文件）

    home, lang = Path(args.home), args.lang
    if args.status:
        return daemon_status(home, lang)
    if args.stop:
        return daemon_stop(home, lang)
    if args.detach:
        return daemon_detach(home, lang)
    try:
        daemon.Daemon(home, log_to_stderr=True, lang=lang).run()  # daemon 自己的语言在这里解析一次，之后不再看环境
    except daemon.DaemonError as e:
        err(e.text(lang))
        return EXIT_CHANNEL
    return 0


def _stale_pid(home: Path) -> int | None:
    """pid 文件里的数（只供人看：探活不靠它）；没有 / 读不出就 None。"""
    try:
        return int((home / "daemon.pid").read_text().strip())
    except (OSError, ValueError):
        return None


def _herdr_status_line(view: dict, lang: str) -> str:
    """`daemon --status` 多打的那一行：daemon 自己视角的 herdr（它自己启动那一刻的 PATH 快照，不是本机现在的）。"""
    if view.get("bin") is None:
        return texts.t("cli.status.herdr.missing", lang)
    if view.get("reachable"):
        return texts.t("cli.status.herdr.ok", lang, bin=view["bin"])
    # herdr_view() 的自己进程内调用永远给 error 一个非空值；这条 view 是跨 IPC 收到的一份 JSON，
    # 保留 "?" 兜底防的是版本不一致的 daemon（它自己的 herdr_view 实现变了）越过这份契约传回空值
    return texts.t("cli.status.herdr.unreachable", lang, bin=view["bin"], error=view.get("error") or "?")


def daemon_status(home: Path, lang: str) -> int:
    ev = probe(home)
    if ev is None:
        pid = _stale_pid(home)
        print(texts.t("cli.status.no_socket", lang, pid=pid) if pid else texts.t("cli.status.not_running", lang))
        return 1
    if ev["subscribed"]:
        sub = texts.t("cli.status.sub.connected", lang)
    elif ev["disconnected_for"] is None:
        sub = texts.t("cli.status.sub.connecting", lang)  # 刚起来还没连上，或从没连上过
    else:
        sub = texts.t("cli.status.sub.disconnected", lang, seconds=ev["disconnected_for"])
    line = texts.t("cli.status.line", lang, pid=ev["pid"], sub=sub, pending=ev["pending"], confirming=ev.get("confirming", 0), pool=ev["pool"])
    print(line + (texts.t("cli.status.transport", lang, transport=ev["transport"]) if ev.get("transport") else ""))
    herdr_ev = ev.get("herdr")  # 旧形态 / 被换掉的 probe 返回值可能没有这个键：没有就不打这一行，不崩
    if herdr_ev:
        print(_herdr_status_line(herdr_ev, lang))
    return 0


def daemon_stop(home: Path, lang: str) -> int:
    """经 socket 的 stop 命令停 daemon（三平台同一条路，不发信号），等它把端点文件与 pid 文件都删干净。"""
    ev = probe(home)
    if ev is None:
        pid = _stale_pid(home)  # 与 --status 同口径：已 bind 但不应答（卡在初始化）不是「未运行」，也停不了它
        print(texts.t("cli.status.no_socket", lang, pid=pid) if pid else texts.t("cli.status.not_running", lang))
        return 1 if pid else 0
    pid = ev.get("pid")
    ack = request(home, {"cmd": "stop"}, lang)
    if ack is None or ack.get("event") != "stopping":
        err(texts.t("cli.stop.no_ack", lang, pid=pid, other=ack.get("message") if ack else texts.t("cli.stop.no_response", lang)))
        return 1
    deadline = time.monotonic() + STOP_TIMEOUT  # 总预算按墙钟算：每轮探活自己会等一会儿，不能按轮数计
    while time.monotonic() < deadline:
        if probe(home, timeout=0.5) is None and not sock_path(home).exists() and not (home / "daemon.pid").exists():
            print(texts.t("cli.stop.done", lang, pid=pid))
            return 0
        time.sleep(0.1)
    err(texts.t("cli.stop.timeout", lang, pid=pid))
    return 1


def probe(home: Path, *, timeout: float | None = None) -> dict | None:
    """静默探活（ipc.probe 的薄包装，缺省超时按本模块的 PROBE_TIMEOUT）：连上问一声 status，返回那条事件；
    连不上 / 答非所问 / 超时都是 None，stderr 一个字都不打（调用方拿它做分支，不是报错）。"""
    return ipc.probe(home, timeout=PROBE_TIMEOUT if timeout is None else timeout)


def _daemon_env(env: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """给 _spawn_daemon 用的一份补过 PATH 的 env；None 表示不覆盖，沿用今天的继承行为。

    只在这一种情形下补：起点这一刻在 herdr 里，且 herdr 自己把 HERDR_BIN_PATH（它自身可执行文件的完整路径）
    带进了这个 pane 的 shell，而它所在的目录还不在当前 PATH 里——这正是「daemon 比 herdr 先启动」那种装机
    顺序留下的洞：等 herdr 装好、从有它的这个 pane 里重新起 daemon 时，让新起的这个补上目录，往后就找得到了。
    """
    e = os.environ if env is None else env
    if e.get("HERDR_ENV") != "1":
        return None
    bin_path = e.get("HERDR_BIN_PATH")
    if not bin_path:
        return None
    directory = os.path.dirname(bin_path)
    if not directory:
        return None
    path = e.get("PATH", "")
    if directory in path.split(os.pathsep):
        return None
    return {**e, "PATH": f"{directory}{os.pathsep}{path}" if path else directory}


def _spawn_daemon(home: Path, lang: str) -> subprocess.Popen:
    """脱离会话 / 控制台起 daemon（怎么脱离由平台层定），stdio 全接空设备。语言用 --lang 显式带过去（调用方已解析过，
    子进程不必再看环境 / locale）。--lang 与 --home 都是顶层选项，必须放在子命令前面。"""
    return platform_.spawn_detached([sys.executable, os.path.abspath(__file__), "--lang", lang, "--home", str(home), "daemon"],
                                     env=_daemon_env())


def daemon_detach(home: Path, lang: str) -> int:
    """脱离会话起 daemon；起来后核一次它在 socket 上报出自己的 pid 才算成功。"""
    if probe(home) is not None:
        err(texts.t("cli.detach.already", lang))
        return EXIT_CHANNEL
    proc = _spawn_daemon(home, lang)
    for _ in range(50):
        time.sleep(0.1)
        if proc.poll() is not None:
            err(texts.t("cli.detach.died", lang, rc=proc.poll(), log=home / "daemon.log"))
            return EXIT_CHANNEL
        ev = probe(home)  # 判据是它在 socket 上报出自己的 pid，不是 socket 文件出现
        if ev and ev.get("pid") == proc.pid:
            print(texts.t("cli.detach.started", lang, pid=proc.pid, log=home / "daemon.log"))
            return 0
    err(texts.t("cli.detach.not_ready", lang, pid=proc.pid, log=home / "daemon.log"))
    return EXIT_CHANNEL


# ---------------------------------------------------------------- 入口

def positive_seconds_in(lang: str):
    """argparse 的 --timeout 类型：报错文案按语言。"""
    def positive_seconds(text: str) -> float:
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(texts.t("help.timeout.not_number", lang, text=repr(text))) from None
        if value <= 0:
            raise argparse.ArgumentTypeError(texts.t("help.timeout.not_positive", lang, text=text))
        return value
    return positive_seconds


def build_parser(lang: str) -> argparse.ArgumentParser:
    """--help 的文案也按进程入口解析出的语言取。"""
    def h(key: str, **fmt) -> str:
        return texts.t(f"help.{key}", lang, **fmt)

    p = argparse.ArgumentParser(prog=PROG, description=h("prog"))
    p.add_argument("--lang", choices=list(texts.LANGS), default=lang, help=h("lang"))
    p.add_argument("--home", default=str(HOME), help=h("home", home=HOME))
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask", help=h("ask"))
    a.add_argument("--timeout", type=positive_seconds_in(lang), default=DEFAULT_TIMEOUT, help=h("ask.timeout"))
    a.set_defaults(fn=cmd_ask)
    sub.add_parser("notify", help=h("notify")).set_defaults(fn=cmd_notify)
    d = sub.add_parser("daemon", help=h("daemon"))
    g = d.add_mutually_exclusive_group()
    g.add_argument("--detach", action="store_true", help=h("daemon.detach"))
    g.add_argument("--status", action="store_true", help=h("daemon.status"))
    g.add_argument("--stop", action="store_true", help=h("daemon.stop"))
    d.set_defaults(fn=cmd_daemon)
    sub.add_parser("slots", help=h("slots")).set_defaults(fn=cmd_slots)
    r = sub.add_parser("release", help=h("release"))
    r.add_argument("slot", nargs="?", help=h("release.slot"))
    r.set_defaults(fn=cmd_release)
    c = sub.add_parser("confirm-sub", help=h("confirm"))
    c.add_argument("slot", help=h("confirm.slot"))
    c.add_argument("--again", action="store_true", help=h("confirm.again"))
    c.add_argument("--subscribed", action="store_true", help=h("confirm.subscribed"))
    c.add_argument("--show-topic", action="store_true", help=h("confirm.show_topic"))
    c.add_argument("--close-pane", action="store_true", help=h("confirm.close_pane"))
    c.add_argument("--report-to", metavar="PANE", help=h("confirm.report_to"))
    c.add_argument("--timeout", type=positive_seconds_in(lang), default=CONFIRM_TIMEOUT, help=h("confirm.timeout"))
    c.set_defaults(fn=cmd_confirm_sub)
    sub.add_parser("add-slot", help=h("add_slot")).set_defaults(fn=cmd_add_slot)
    w = sub.add_parser("away", help=h("away"))
    w.add_argument("action", choices=("on", "off", "status"), help=h("away.action"))
    w.add_argument("--json", action="store_true", help=h("away.json"))
    w.set_defaults(fn=cmd_away)
    return p


def main(argv: list[str] | None = None) -> int:
    platform_.utf8_stdio()  # Windows 的管道 stdio 缺省是 ANSI 代码页：JSON 与回复里的非 ASCII 会炸
    raw_argv = sys.argv[1:] if argv is None else argv
    try:
        lang = resolve_lang(_prescan_lang(raw_argv))  # 先于一切：语言错了连 --help 都会错
    except BadEnvLang as e:
        err(texts.t("cli.bad_env_lang", texts.DEFAULT_LANG, value=str(e)))
        return EXIT_INVALID
    args = build_parser(lang).parse_args(raw_argv)
    args.lang = lang  # --lang 的非法值到不了这里（argparse 的 choices 已拦）；合法值与预扫一致
    try:
        ipc.transport(Path(args.home))  # 同 lang：传输选错了每条命令都会错，入口就拦
    except ipc.BadTransport as e:
        err(texts.t("cli.bad_env_ipc", lang, value=repr(e.value)))
        return EXIT_INVALID
    try:
        migrate.run(Path(args.home), lang=lang, err=err)  # 旧版留下的目录 / 钥匙串条目搬到新名字下；--help 到不了这里
    except migrate.LegacyDaemonRunning as e:
        err(texts.t("migrate.old_daemon_running", lang, legacy_home=e.legacy_home))
        return EXIT_NEEDS_HUMAN  # 要人去停旧 daemon
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
