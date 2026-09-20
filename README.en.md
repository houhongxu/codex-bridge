# cpet

**Bring Codex CLI tasks to the desktop pet.** A local shared App Server manager and task-subscription relay for macOS.

[![CI](https://github.com/houhongxu/cpet/actions/workflows/ci.yml/badge.svg)](https://github.com/houhongxu/cpet/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

[简体中文](README.md) · English · [Architecture](docs/architecture.md) · [Troubleshooting](docs/troubleshooting.md)

> Experimental: **0.1.0-alpha.3**. This is a community project, not affiliated with or endorsed by OpenAI. Desktop deep links, external-server configuration and log formats are version-dependent. Attaching a task can navigate the desktop to that task.

CLI naming of an unmaterialized paginated thread first asks the backend to persist its history, so manually opening the named empty task in Desktop can recover it. No placeholder message is sent. If persistence cannot be confirmed, send the first message in CLI and retry naming.

## What it does

`cx` connects the CLI and desktop to the same local Codex App Server. A per-CLI relay opens eligible tasks in the desktop when needed, allowing the desktop to subscribe and its existing pet UI to display task activity. Confirmed subscriptions are reused across turns; reconnection or unsubscription invalidates them. Ephemeral and unclassified tasks do not trigger navigation.

A reachable server, a log-confirmed subscription and a visible pet activity pill are separate outcomes. cpet does not install or unlock the native pet.

Empty new threads attach after the first input is forwarded and their local history becomes readable. Waiting for history or desktop subscription does not block CLI messages or approvals. Existing persisted threads can attach on resume/fork.

## Requirements

- macOS and zsh for automatic aliases.
- Python 3.9+ with `venv`; a supported Python 3.11+ is recommended.
- `/Applications/ChatGPT.app` or `/Applications/Codex.app` with a bundled `Contents/Resources/codex` supporting `app-server --listen`, `--remote` and `--remote-auth-token-env`.
- An authenticated desktop app with the native pet feature, plus a free `127.0.0.1:4500`.
- Network access to install the pinned `websockets==15.0.1` dependency from PyPI.

See the [tested compatibility scope](docs/compatibility.md). Windows, Linux desktop integration and other vendors' CLIs are not supported.

## Install and run

```sh
mkdir -p ~/workspace
git clone https://github.com/houhongxu/cpet.git ~/workspace/cpet
cd ~/workspace/cpet
./install.sh
source ~/.zshrc
cpet --version
cpet on
cpet status
```

If the desktop was already running before `cpet on`, finish its tasks, quit it, then run `cpet on` again. The external-server setting takes effect at desktop startup; cpet never forcibly restarts it.

```sh
cd /path/to/your/project
cx
# Select an existing task across project directories:
cx resume --all
```

Enable the pet in the desktop and check an active task. First attachment or recovery may navigate the app; subsequent turns reuse the subscription. `--all` expands the resume picker scope; it does not run every task.

Optional login startup: `cpet enable`. Disable login startup without stopping current work: `cpet disable`.

## Async question synchronization

Since `0.1.0-alpha.2`, new `cx` sessions adapt **live async questions received after connecting**. Answering in Desktop dismisses the matching CLI question; answering in the CLI submits the Desktop-compatible answer to the same task. No official CLI or desktop files are patched.

The CLI uses its standard question dialog, including its `Esc` interruption behavior. Historical question text is retained, but old dialogs are not recreated; answer outstanding historical questions in Desktop. Known duplicate or late answers are suppressed, but simultaneous submissions from two clients are not atomically coordinated.

If cpet cannot confirm an answer, check Desktop before replying again: it never retries automatically. To restore native question passthrough, run `CPET_QUESTION_SYNC=0 cx resume --all`. Restart old CLI sessions after upgrading. See [tested versions and limits](docs/compatibility.md).

Since `0.1.0-alpha.3`, recognized answer envelopes appear as readable question/answer text in live CLI messages and restored history, using the labels `问题：` (Question) and `你的回答：` (Your answer). IDs and JSON remain intact in backend storage and submitted messages. Only the CLI-facing copy is formatted; ordinary text, quoted examples and unknown formats pass through. `CPET_QUESTION_SYNC=0` also disables this formatting.

## Installed files are independent of the checkout

The installer copies code into `~/Library/Application Support/Codex CLI Bridge/runtime/` and creates its own `.venv` there. `~/.local/bin/codex-pet` points to that installed copy. Moving or deleting the source checkout does not break installed commands. Re-run `./install.sh` to deploy source updates.

The installer backs up modified `.zshrc` files and refuses conflicting `cx` / `cpet` definitions outside its managed block. It does not start the service or enable login startup by default. Set `CPET_PYTHON=/path/to/python3` to choose the interpreter.

## Commands

| Command | Purpose |
| --- | --- |
| `cx`, `cx resume --all`, `cx fork` | Start, resume or fork an interactive task |
| `cpet on` | Prepare the server and desktop connection |
| `cpet off` | Stop the shared server; interrupts tasks using it |
| `cpet enable` / `disable` | Enable / disable login startup |
| `cpet status` / `status --json` | Read diagnostics; output messages are currently Chinese |
| `cpet --version` | Show the installed version |
| `cpet uninstall` | Remove service configuration, aliases and command; retain installed files and logs |

New tasks use the terminal's working directory. Resume/fork retain the existing task directory unless explicitly overridden. `cx` is not a general wrapper for every Codex subcommand.

## Update or uninstall

Installing updated scripts does not restart the shared server or desktop. Existing `cx` processes keep their old code. Finish active work before restarting those CLI sessions or stopping the server.

```sh
cd /path/to/cpet
git pull --ff-only
./install.sh
```

Restart old CLI sessions with `cx resume --all` to use updated relay code. Source edits alone do not change the installed runtime.

Run `cpet uninstall`, quit/reopen the desktop, and open a new terminal to restore ordinary usage. Source files, installed runtime, backups and logs are retained. `cpet off` alone does not disable login startup.

## Limitations and security

- Desktop navigation remains necessary for first attachment or recovery.
- Native desktop logging/configuration may change after app updates.
- A launchd server can differ from the terminal or built-in desktop server in proxy settings, macOS directory permissions and desktop-only tool context.
- The fixed shared server has no additional cpet authentication; temporary per-CLI relays require a random Bearer token. Keep all listeners on loopback. See [SECURITY](SECURITY.md).
- Muted tasks, hidden activity pills, or read/idle tasks may not appear on the pet.
- Custom `CODEX_HOME`, alternative app locations, concurrent desktop instances and remote hosts are unverified.

## Development

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
python3 scripts/check_repository.py
sh -n install.sh
```

Tests use temporary homes and local WebSocket fixtures, without real model requests or launchd changes. Linux CI checks portable logic, not macOS integration. Read [CONTRIBUTING](CONTRIBUTING.md), [CHANGELOG](CHANGELOG.md), and [the roadmap](docs/roadmap.md). Detailed maintenance documents currently use Chinese; concise English reports and contributions are welcome.

## License

[MIT](LICENSE), copyright 2026 houhongxu. This repository includes cpet code only, not OpenAI applications, models or pet assets.
