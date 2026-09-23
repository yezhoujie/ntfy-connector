# Changelog

All notable changes to this repository are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/);
each release is a git tag `vX.Y.Z`. A skill is installed from the repository by content, so a version is
only a tag you can pin (`npx skills add 'yezhoujie/ntfy-connector#v0.2.0'`).

## ntfy-connector

Versions before 0.2.0 were released inside the shared `agent-remote-communication-skills` monorepo, tagged
`agent-ntfy/vX.Y.Z` (the earliest three, `v0.1.0`–`v0.1.2`, predate a second skill sharing that repository
and were tagged bare `vX.Y.Z`). This repository carries that history forward under one uniform scheme,
`vX.Y.Z`; the entries below are unchanged from how they were written at the time.

### [0.3.0] - 2026-09-23

#### Added

- Switching remote mode on or off now sends one notification to the phone. The "on" one says what the
  channel can actually do right now: what you type in the topic reaches the terminal, or — when this machine
  has no herdr, or the daemon cannot find it — what will not get through and what to do about it. The "off"
  one goes out *before* the slot is released, so it cannot be lost to another project taking the slot.
- `daemon --status` prints the daemon's own view of herdr: the path it resolved, or that it found nothing on
  its PATH, with the two commands that fix it. `away on` from inside herdr warns on stderr when the daemon
  cannot see herdr, without changing its exit code.

#### Changed

- Every command the CLI tells a human to run is now the full `python3 "<skill dir>/scripts/ntfy_connector.py" …`
  form instead of the bare name `ntfy-connector`, which is on nobody's PATH. The READMEs drop the alias step
  and give the install locations instead. Passages that quote `--help` output verbatim keep the bare name:
  that is what the program prints.
- `away on` now starts the daemon detached everywhere, inside herdr as well — no pane is opened for it. The
  pane it used to run in served no purpose once remote mode was up, and closing it killed the daemon. Watch
  it with `daemon --status`, stop it with `daemon --stop`. The reachability check still opens its own pane:
  it shows the topic, waits for Enter and for the button on the phone, and closes itself afterwards.
- A message that could not be injected because the daemon cannot find the herdr executable now says so and
  tells you to restart it from a herdr pane, instead of only reporting that herdr is missing.
- `daemon --detach` started from inside herdr adds herdr's own directory to the daemon's PATH, so the case
  above does not arise for daemons started that way.

### [0.2.0] - 2026-09-20

#### Changed

- The skill's repository split out of the shared `agent-remote-communication-skills` monorepo into its own
  repository, `ntfy-connector`. Install with `npx skills add yezhoujie/ntfy-connector` (`--skill agent-ntfy`
  still works but is no longer required — this repository ships only that one skill). The Claude Code plugin
  marketplace entry point is unchanged (`claude plugin marketplace add yezhoujie/agent-remote-communication-skills`
  then `claude plugin install agent-ntfy@agent-remote-communication-skills`): that marketplace is maintained
  in the index repository, with this skill's content taken from here.
- The CLI's self-name and its entry script are `ntfy-connector` and `scripts/ntfy_connector.py` (were
  `agent-ntfy` and `scripts/agent_ntfy.py`); every message the CLI prints about itself follows — `--help`
  usage, the stderr prefix, the line a hand-run `confirm-sub` prints to paste back to the agent.
- Layout: the installable copy lives at `skill/agent-ntfy/` (the skill itself keeps the name `agent-ntfy`),
  the development copy at `src/`, kept byte-identical by `scripts/sync-skill.py`; tests are at `tests/` at
  the repository root.

#### BREAKING

| aspect | 0.1.x (agent-ntfy) | 0.2.0 (ntfy-connector) |
|---|---|---|
| entry script | `scripts/agent_ntfy.py` | `scripts/ntfy_connector.py` |
| environment variable prefix | `AGENT_NTFY_*` | `NTFY_CONNECTOR_*` |
| user directory | `~/.agent-ntfy` | `~/.ntfy-connector` |
| per-project directory | `<project root>/.agent-ntfy/` | `<project root>/.ntfy-connector/` |
| keychain service / account | `AGENT_NTFY_TOPICS` / `agent-ntfy` | `NTFY_CONNECTOR_TOPICS` / `ntfy-connector` |
| phone → agent injection prefix | `[agent-ntfy remote] ` | `[ntfy-connector remote] ` |
| system-event marker | `[agent-ntfy] ` | `[ntfy-connector] ` |
| button control marker | `__agent-ntfy:…__` | `__ntfy-connector:…__` |
| default topic prefix | `agent-ntfy` | `ntfy-connector` |
| logger names (`logging.getLogger`; not part of the `daemon.log` line format) | `agent-ntfy`, `agent-ntfy.daemon` / `.inject` / `.platform` | `ntfy-connector`, `ntfy-connector.daemon` / `.inject` / `.platform` |

#### Added

- On first run, any `ntfy-connector` subcommand other than `--help` migrates a 0.1.x installation
  automatically and idempotently: the user directory `~/.agent-ntfy` is renamed to `~/.ntfy-connector`
  (always that one fixed location, never wherever `--home` / `NTFY_CONNECTOR_HOME` points), the macOS
  keychain's topic-pool item is copied to its new service and account name and the old one deleted, and
  each project's `.agent-ntfy/` is renamed to `.ntfy-connector/` the first time a command resolves that
  project. Environment variables are not migrated — the old names are never read, not even for their
  value — only warned about, with the new name to set instead. A guard refuses to touch anything while a
  0.1.x daemon is still listening on the old user directory.

#### Docs

- README (both languages) gained a section on upgrading from agent-ntfy 0.1.x: what the automatic
  migration does and does not do, and which environment variables and standing-rule markers need renaming
  by hand.
- `references/daemon.md`'s environment-variable table: `NTFY_CONNECTOR_TOPIC_PREFIX`'s documented default
  was `agent-ntfy`; the code's default is `ntfy-connector`.

#### Fixed

- The setext guard added in 0.1.3 missed CRLF text: splitting the field on `\n` leaves a trailing
  `\r` on the `---` / `===` line, which the pattern did not match, so such a line in the middle of a
  CRLF body could still turn the line above it into a heading and lose the rule. Only a line at the
  very end of the field was caught. The pattern now allows the trailing `\r`.

### [0.1.3] - 2026-09-16

#### Added

- The repository now doubles as a Claude Code plugin marketplace (`.claude-plugin/marketplace.json`), so this
  skill can also be installed with `claude plugin marketplace add yezhoujie/agent-remote-communication-skills`
  followed by `claude plugin install agent-ntfy@agent-remote-communication-skills`. `npx skills add` is
  unaffected.

#### Changed

- The tests moved out of the skill directory to `tests/ntfy/` at the repository root, so what
  `npx skills add` installs no longer carries them — 420 kB less out of 878 kB (48%). The move itself
  changes nothing that ships: `SKILL.md`, both READMEs, `references/` and `examples/` are untouched,
  and `scripts/` only by the two fixes below. The tests now run from the repository root
  (`AGENT_NTFY_OFFLINE=1 python3 -m unittest discover -s tests/ntfy -t . -v`).

#### Fixed

- The CLI usage text did not say what `ask --timeout` defaults to; it now says 43200 s (12 hours),
  which is `DEFAULT_TIMEOUT`. `confirm-sub --timeout` keeps its documented 600 s.
- A line inside a user-supplied field that holds only `---` or `===` right under a non-blank line no
  longer turns that line into a heading. Markdown reads such a line as a setext underline, so on the
  ntfy Android app the last line of a pasted commit message (`Co-Authored-By: …`) came out as a large
  heading and the rule meant to follow it was gone. Rendering now puts a blank line above it, leaving
  it a horizontal rule: up to three leading spaces still count as an underline, four make it a code
  block and are left alone, an existing blank line is not doubled, and single-line values (an option
  `id`, `recommend`) are unchanged.

### [0.1.2] - 2026-09-14

#### Added

- `confirm-sub --report-to <pane>`: a confirmation pane opened by the CLI sends its result (confirmed / timed
  out / interrupted / failed) back into the agent's session as one line prefixed `[agent-ntfy] `, so the
  agent no longer has to poll `slots`. Set automatically on auto-opened panes.
- A `confirm-sub` run by hand ends with a line to paste into the agent (`agent-ntfy: slotN is confirmed; …`)
  and tells the user the terminal window can be closed — the only way the result reaches an agent without herdr.

#### Changed

- `away on` leases a slot for the project on the spot (a confirmed idle slot if any, else the lowest
  unconfirmed idle one, which it then sends through the reachability check) instead of waiting for the first
  `ask`: the lease and the check are settled while the human is still at the keyboard, and `state.json` shows
  the slot right away. `away off` releases the project's slot. The daemon gained a `lease` command for this.
- A lease belongs to its project until that project releases it. When every slot is leased, `ask` / `notify`
  / `away on` no longer list "idle slots you could take over"; they print the occupancy (holder, idle or
  question pending, confirmed or not, one line per slot) for the user to decide between turning remote mode
  off in one of those projects and `add-slot`. `release <slot>` now carries the caller's project identity and
  refuses another project's slot (`not_yours`, rc 4); to free a lease whose project directory is gone, run it
  with `AGENT_NTFY_TARGET=<that holder>`. The state layer's `replace` (never reachable from the CLI) is gone.
- `away off` clears `slot` / `confirmed` in `state.json` only when the lease was actually released (or there
  was none); when the release is refused or the daemon is not running the file keeps saying what the daemon's
  lease says. It also no longer re-targets the injection pane.
- Restart the daemon after upgrading: `away on` now needs the daemon's `lease` command, which an older daemon
  does not know (stop it with its own CLI: `agent-ntfy daemon --stop`).

### [0.1.1] - 2026-09-14

#### Added

- `--lang zh|en` on the command line (a top-level option, before the subcommand). The wording language is now
  resolved once per process as `--lang` > `AGENT_NTFY_LANG` > the system locale (`LC_ALL` / `LC_MESSAGES` /
  `LANG` starting with `zh`, or a Chinese Windows locale, means `zh`) > `en`, so a machine with a Chinese locale
  gets Chinese wording without setting anything. Panes and daemons the CLI starts for you receive the resolved
  language via `--lang` (no more dependence on the POSIX `env` tool).
- The confirmation pane that `away on` / `confirm-sub` opens for you asks `Close this pane? [Y/n]` after a
  successful check and closes itself on Enter (`--close-pane`, set only on auto-opened panes; never asked
  after a failure, and never when stdout is not a terminal).
- README §2.2: iPhone users use the ntfy web app added to the home screen — the iOS App Store app receives
  notifications but has no reply box.
- `examples/remote-mode-rule.md` / `remote-mode-rule.zh-CN.md`: a ready-made standing rule for the agent
  (session start, the human leaving, decisions via `ask`, `notify` only for answers and major events, the
  human returning, teams of sessions), and README §13 on how to keep the skill in force for the whole session.

#### Changed

- The confirmation guide and the "tell the user" text warn not to close the pane before pressing Enter and
  tapping the button — closing it cancels the check.
- The receipt for a phone message on a slot whose lease has no pane names the commands that register one
  (`ask`, `notify`, `slots`, `release` without argument, `away on`, `away status`) instead of "any command".

#### Fixed

- One-shot commands (`notify`, `slots`, `release`, `add-slot`, `daemon --status|--stop`) give up after 60 s
  with "no response from the daemon" (exit 3) when the daemon accepted the connection but never answered,
  instead of hanging until Ctrl-C.
- The leases file is written through the same private atomic-write path as the topic pool file, so a leftover
  temporary file with wide permissions can no longer be renamed into place as is.

### [0.1.0] - 2026-09-13

First tagged release. Earlier commits were never versioned; the entries below describe what changed
relative to those untagged versions.

#### Added

- `notify` subcommand: a one-way notification to the same phone (`{"title", "body", "lang"?}` on stdin,
  Markdown body, no button, no waiting). Allowed while a question is pending; exit codes 0 sent · 1 invalid
  input · 3 channel failure · 4 a human must act. Anything the user sends from the phone afterwards counts as
  the reply to the pending question first.
- `away on` is a one-stop setup: it starts the daemon if none answers (inside herdr in a new pane, otherwise
  detached), makes sure a confirmed slot exists (starting the reachability check in a new pane when needed),
  then writes the per-project state file. `away status` checks the state file against the daemon's leases and corrects it.
- `confirm-sub` run by an agent inside herdr (stdout not a terminal) opens a pane for the user to do the
  check in, prints the pane id and exits 0 — the topic name stays in that pane.
- Linux and Windows: a loopback-TCP IPC transport (`AGENT_NTFY_IPC`, default on Windows) next to the Unix
  socket; the topic pool can live in a 0600 file or a Windows DPAPI-encrypted file (`AGENT_NTFY_STORE`);
  daemon `--detach` / `--status` / `--stop` no longer rely on POSIX-only calls. Unit tests run on GitHub
  Actions for ubuntu / windows / macos × Python 3.10 / 3.13, plus pyright. No end-to-end test on Linux or
  Windows yet.
- `daemon --status` shows the IPC transport in use.
- A phone message for a slot whose lease has no pane registered (the commands were run outside herdr) gets a
  receipt saying so, with `Release slot` / `Ignore` buttons, instead of a generic "not inside herdr" text.
- When the project root cannot be determined (the current directory was deleted), `ask` / `notify` / `slots` /
  `release` / `away` print a readable error and exit 3 instead of a traceback.
- Cards are rendered as Markdown: bold section labels, numbered options, a `---` rule before the closing
  hint.
- `LICENSE` (MIT) and this changelog.

#### Changed

- Messages sent from the phone while no question is pending are injected into the agent's session with the
  prefix `[agent-ntfy remote] ` in front (previously the text arrived without any marker); the text itself is
  unchanged.
- A slot is leased by the **project** (the git toplevel of the current directory, otherwise the directory
  itself) instead of by the herdr pane, so changing panes or sessions inside one project no longer burns a
  new slot. The lease additionally records the herdr pane from which the project last ran `ask`, `notify`, `slots`,
  `release` (no argument), `away on` or `away status`; phone messages are injected there. The `[tag]` in card titles is the project directory name. `release` with no argument
  releases the current project's slot. `AGENT_NTFY_TARGET` still overrides the identity.
- While remote mode is on (`away on`), `ask` and `notify` only use confirmed slots; they exit 4 with the
  candidates instead of leasing an unconfirmed one.
- `daemon --status` and `--stop` talk to the daemon over its socket (no signals). A daemon
  started by an earlier version does not understand the stop command: stop it with that version's CLI or
  `kill -TERM <pid>` before upgrading (see the README, "Versions and upgrading").
- Leases created by earlier versions (holder is a pane id) are kept and can only be released by hand
  (`release <slot>`). An earlier daemon ignores the new request fields, so the daemon must be restarted after
  upgrading.
- `daemon --detach`'s "not ready within 5 s" message no longer guesses at a keychain prompt; CLI wording and
  error texts are platform-neutral (e.g. the Unix-socket path-length hint no longer quotes a macOS-only
  number).

#### Fixed

- Option numbers on the card: the ntfy Android app renders Markdown ordered lists as bullets, so the options
  are now written with an escaped period (`1\.`) and separated by blank lines — CommonMark renders an escaped
  period as a plain `1.`, `2.` instead of a list.
- Windows: closing the subscription stream no longer waits for the stream timeout when the reader thread is
  blocked; `confirm-sub` no longer reports a channel failure (exit 3) when the daemon timed out while the
  user was still being asked to press Enter.
- A missing `security` command (the keychain exists only on macOS) is reported as such, with the
  `AGENT_NTFY_STORE` alternatives, instead of as a socket error.

[0.3.0]: https://github.com/yezhoujie/ntfy-connector/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/yezhoujie/ntfy-connector/compare/v0.1.3...v0.2.0
[0.1.3]: https://github.com/yezhoujie/ntfy-connector/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/yezhoujie/ntfy-connector/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/yezhoujie/ntfy-connector/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/yezhoujie/ntfy-connector/releases/tag/v0.1.0
