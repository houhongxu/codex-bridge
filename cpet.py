#!/usr/bin/env python3
"""Manage a local shared Codex App Server and desktop connection on macOS."""

import argparse
import datetime
import json
import os
from pathlib import Path
import plistlib
import re
import select
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

__version__ = "0.1.0-alpha.1"

ENDPOINT = "ws://127.0.0.1:4500"
ENV_KEY = "CODEX_APP_SERVER_WS_URL"
SERVER_LABEL = "local.codex-cli-bridge.server"
LOGIN_LABEL = "local.codex-cli-bridge.login"
BEGIN = "# >>> codex-cli-bridge >>>"
END = "# <<< codex-cli-bridge <<<"
PET_ACTIVITY_KEY = "avatar-overlay-activity-pills-visible"
PET_MUTED_KEY = "avatar-overlay-muted-notification-ids-v1"


class BridgeError(Exception):
    pass


def run(args, check=True, timeout=15):
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BridgeError(str(exc)) from exc
    if check and result.returncode:
        raise BridgeError(f"{shlex.join(map(str, args))}\n{result.stderr.strip() or result.stdout.strip()}")
    return result


def atomic_write(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp.open("xb") as stream:
            os.chmod(temp, mode)
            stream.write(data)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def alias_text(original, executable):
    block = (f"{BEGIN}\n"
             f"alias cx={shlex.quote(shlex.quote(str(executable)) + ' cx')}\n"
             f"alias cpet={shlex.quote(shlex.quote(str(executable)))}\n"
             f"{END}\n")
    pattern = re.compile(r"(?m)^" + re.escape(BEGIN) + r"\n.*?^" + re.escape(END) + r"\n?", re.S)
    if BEGIN in original or END in original:
        if len(pattern.findall(original)) != 1:
            raise BridgeError(".zshrc 中的 codex-cli-bridge 标记不完整，未修改文件。")
        return pattern.sub(lambda _: block, original)
    if re.search(r"(?m)^\s*(?:alias\s+(?:cx|cpet)=|(?:function\s+)?(?:cx|cpet)\s*\(\))", original):
        raise BridgeError(".zshrc 已定义 cx 或 cpet，未覆盖现有命令。")
    return original + ("\n" if original and not original.endswith("\n") else "") + "\n" + block


def cli_arguments(cli, extra, endpoint=ENDPOINT, cwd=None, auth_token_env=None):
    if any(a == "--remote" or a.startswith("--remote=") for a in extra):
        raise BridgeError("cx 已固定连接本机共享后台；连接其他地址请直接使用 codex。")
    if any(a == "--remote-auth-token-env" or a.startswith("--remote-auth-token-env=") for a in extra):
        raise BridgeError("cx 自动管理本机转接凭据；连接其他后台请直接使用 codex。")
    remote = ["--remote", endpoint]
    if auth_token_env:
        remote += ["--remote-auth-token-env", auth_token_env]
    location = [] if cwd is None or any(a == "--cd" or a.startswith("--cd=") or a.startswith("-C") for a in extra) else ["--cd", cwd]
    if extra and extra[0] in ("resume", "fork"):
        # Resumed and forked tasks retain their existing workspace unless the
        # user explicitly supplies -C/--cd, just like the ordinary CLI.
        return [cli, extra[0], *remote, *extra[1:]]
    return [cli, *remote, *location, *extra]


def launch_cli(bridge, extra):
    # Preserve the caller's project; the remote server has its own service cwd.
    cwd = os.getcwd()
    if any(a in ("--help", "-h", "--version", "-V") for a in extra):
        os.execv(bridge.cli, cli_arguments(bridge.cli, extra, cwd=cwd))
    project = Path(__file__).resolve().parent
    runtime = project / ".venv/bin/python"
    if not runtime.exists():
        raise BridgeError(f"缺少转接依赖，请先运行 {project / 'install.sh'}。")
    cli_arguments(bridge.cli, extra, cwd=cwd)  # Validate before starting anything.
    bridge.on(quiet=True)
    with (bridge.root / "desktop-relay.stderr.log").open("ab") as log:
        relay = subprocess.Popen(
            [str(runtime), str(project / "desktop_bridge.py"), "--upstream", ENDPOINT, "--app", str(bridge.app)],
            stdout=subprocess.PIPE, stderr=log, text=True, start_new_session=True,
        )
        try:
            if not select.select([relay.stdout], [], [], 10)[0]:
                raise BridgeError("桌面转接启动超时，请查看 desktop-relay.stderr.log。")
            line = relay.stdout.readline()
            if not line:
                raise BridgeError("桌面转接启动失败，请查看 desktop-relay.stderr.log。")
            ready = json.loads(line)
            endpoint = ready["endpoint"]
            environment = os.environ.copy()
            environment["CPET_REMOTE_AUTH_TOKEN"] = ready["auth_token"]
            for key in ("NO_PROXY", "no_proxy"):
                environment[key] = ",".join(filter(None, [environment.get(key), "127.0.0.1", "localhost"]))
            command = cli_arguments(bridge.cli, extra, endpoint, cwd, "CPET_REMOTE_AUTH_TOKEN")
            child = subprocess.Popen(command, env=environment)
            while True:
                try:
                    return child.wait()
                except KeyboardInterrupt:
                    # The foreground CLI receives the same terminal signal.
                    continue
        finally:
            relay.terminate()
            try:
                relay.wait(timeout=5)
            except subprocess.TimeoutExpired:
                relay.kill()
                relay.wait()


def pet_settings(home):
    """Read persisted UI preferences; never rewrite the running app's state."""
    path = Path(home) / ".codex/.codex-global-state.json"
    try:
        state = json.loads(path.read_text())
        atoms = state.get("electron-persisted-atom-state", {})
        if not isinstance(atoms, dict):
            raise ValueError("invalid persisted settings")
        visible = atoms.get(PET_ACTIVITY_KEY, True)
        opened = state.get("electron-avatar-overlay-open")
        muted = atoms.get(PET_MUTED_KEY, [])
        if type(visible) is not bool or not isinstance(muted, list):
            raise ValueError("invalid pet settings")
        return {
            "settings_readable": True,
            "overlay_open": opened if type(opened) is bool else None,
            "activity_visible": visible,
            "muted_task_count": len(muted),
        }
    except (OSError, ValueError, AttributeError):
        return {
            "settings_readable": False,
            "overlay_open": None,
            "activity_visible": None,
            "muted_task_count": None,
        }


class Bridge:
    def __init__(self, home=None):
        self.home = Path(home or Path.home())
        self.root = self.home / "Library/Application Support/Codex CLI Bridge"
        self.runtime = self.root / "runtime"
        self.bin = self.home / ".local/bin/codex-pet"
        self.server_plist = self.root / "server.plist"
        self.login_plist = self.home / f"Library/LaunchAgents/{LOGIN_LABEL}.plist"
        self.backup_env = self.root / "previous-launch-environment.json"
        self.domain = f"gui/{os.getuid()}"
        self.app = next((p for p in [Path("/Applications/ChatGPT.app"), Path("/Applications/Codex.app")]
                         if (p / "Contents/Resources/codex").is_file()), None)

    def prepare(self):
        if self.app is None:
            raise BridgeError("找不到 /Applications/ChatGPT.app 或 Codex.app 中的 Codex。")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    @property
    def cli(self):
        if self.app is None:
            raise BridgeError("找不到桌面应用自带的 Codex。")
        return str(self.app / "Contents/Resources/codex")

    def loaded(self, label):
        return run(["/bin/launchctl", "print", f"{self.domain}/{label}"], check=False).returncode == 0

    @staticmethod
    def healthy():
        # A proxy configured in the shell must not intercept the local probe.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open("http://127.0.0.1:4500/readyz", timeout=0.5) as response:
                return response.status == 200
        except (OSError, urllib.error.URLError):
            return False

    @staticmethod
    def port_busy():
        try:
            with socket.create_connection(("127.0.0.1", 4500), timeout=0.3):
                return True
        except OSError:
            return False

    def server_config(self):
        node = shutil.which("node")
        paths = [str(self.home / ".local/bin")]
        if node:
            paths.append(str(Path(node).parent))
        paths += ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        return {
            "Label": SERVER_LABEL,
            "ProgramArguments": [self.cli, "app-server", "--listen", ENDPOINT],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "WorkingDirectory": str(self.root),
            "EnvironmentVariables": {"PATH": ":".join(dict.fromkeys(paths)), "RUST_LOG": "warn"},
            "StandardOutPath": str(self.root / "server.stdout.log"),
            "StandardErrorPath": str(self.root / "server.stderr.log"),
            "ProcessType": "Background",
        }

    def start_server(self):
        self.prepare()
        if self.loaded(SERVER_LABEL):
            if self.healthy():
                return
        else:
            if self.port_busy():
                raise BridgeError("4500 端口被其他进程占用；未终止或接管它。请先查明占用者。")
            atomic_write(self.server_plist, plistlib.dumps(self.server_config()))
            result = run(["/bin/launchctl", "bootstrap", self.domain, str(self.server_plist)], check=False)
            if result.returncode and not self.loaded(SERVER_LABEL):
                raise BridgeError(result.stderr.strip() or "launchd 启动失败。")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.healthy():
                return
            time.sleep(0.25)
        raise BridgeError(f"共享后台未就绪。请查看 {self.root / 'server.stderr.log'}")

    @staticmethod
    def launch_env():
        result = run(["/bin/launchctl", "getenv", ENV_KEY], check=False)
        return result.stdout.rstrip("\n") or None

    def set_desktop_env(self):
        self.prepare()
        if not self.backup_env.exists():
            previous = self.launch_env()
            try:
                fd = os.open(self.backup_env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "w") as stream:
                    json.dump({"previous": previous}, stream)
        run(["/bin/launchctl", "setenv", ENV_KEY, ENDPOINT])

    def restore_desktop_env(self):
        if not self.backup_env.exists():
            return
        previous = json.loads(self.backup_env.read_text())["previous"]
        # Preserve a subsequent change made by the user or another tool.
        if self.launch_env() == ENDPOINT:
            if previous is None:
                run(["/bin/launchctl", "unsetenv", ENV_KEY])
            else:
                run(["/bin/launchctl", "setenv", ENV_KEY, previous])
        self.backup_env.unlink()

    def on(self, quiet=False):
        self.start_server()
        self.set_desktop_env()
        # LaunchServices reuses an existing app instance. Never quit or restart it.
        run(["/usr/bin/open", "-g", "--env", f"{ENV_KEY}={ENDPOINT}",
             "-a", str(self.app)])
        if not quiet:
            print(f"共享后台已就绪：{ENDPOINT}")
            print("桌面应用已打开。若它在接入前就已运行，请在任务结束后退出并重开一次。")

    def off(self):
        if self.loaded(SERVER_LABEL):
            run(["/bin/launchctl", "bootout", f"{self.domain}/{SERVER_LABEL}"])
        self.restore_desktop_env()
        print("共享后台已关闭，桌面应用下次启动时恢复原连接设置。")
        if self.login_plist.exists():
            print("登录自启仍开启；要取消它，运行 cpet disable。")

    def enable(self):
        if not self.bin.exists():
            raise BridgeError("请先运行 install。")
        self.on(quiet=True)
        config = {
            "Label": LOGIN_LABEL,
            "ProgramArguments": [sys.executable, str(self.bin), "login-start"],
            "RunAtLoad": True,
            "StandardOutPath": str(self.root / "login.stdout.log"),
            "StandardErrorPath": str(self.root / "login.stderr.log"),
            "ProcessType": "Interactive",
        }
        data = plistlib.dumps(config)
        changed = not self.login_plist.exists() or self.login_plist.read_bytes() != data
        loaded = self.loaded(LOGIN_LABEL)
        if changed and loaded:
            run(["/bin/launchctl", "bootout", f"{self.domain}/{LOGIN_LABEL}"])
            loaded = False
        if changed:
            atomic_write(self.login_plist, data)
        if not loaded:
            run(["/bin/launchctl", "bootstrap", self.domain, str(self.login_plist)])
        print("登录自启已开启：登录 macOS 后启动共享后台并打开桌面应用。")

    def disable(self):
        if self.loaded(LOGIN_LABEL):
            run(["/bin/launchctl", "bootout", f"{self.domain}/{LOGIN_LABEL}"])
        self.login_plist.unlink(missing_ok=True)
        print("登录自启已关闭；当前后台和任务继续运行。需要停止时运行 cpet off。")

    def status(self):
        owned = self.loaded(SERVER_LABEL)
        healthy = self.healthy()
        data = {
            "version": __version__,
            "runtime_directory": str(self.runtime),
            "endpoint": ENDPOINT,
            "managed_server_loaded": owned,
            "server_ready": owned and healthy,
            "autostart_enabled": self.login_plist.exists(),
            "login_agent_loaded": self.loaded(LOGIN_LABEL),
            "desktop_next_launch_configured": self.launch_env() == ENDPOINT,
            "desktop_pet_sync": "not_verified",
            "pet": pet_settings(self.home),
            "last_desktop_attachment": None,
            "app": str(self.app) if self.app else None,
            "log_directory": str(self.root),
        }
        try:
            files = sorted((self.root / "attachments").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            if files:
                data["last_desktop_attachment"] = json.loads(files[0].read_text())
        except (OSError, ValueError):
            pass
        return data

    def install_runtime(self):
        """Install an independent runtime; never point commands at the checkout."""
        source = Path(__file__).resolve().parent
        files = ["cpet.py", "desktop_bridge.py", "requirements.txt"]
        if (source / "LICENSE").exists():
            files.append("LICENSE")
        # Read everything before touching a working installation.
        payloads = {name: (source / name).read_bytes() for name in files}
        self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        python = self.runtime / ".venv/bin/python"
        if not python.exists():
            run([sys.executable, "-m", "venv", str(self.runtime / ".venv")], timeout=120)
        run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
             "--index-url", "https://pypi.org/simple", "-r", str(source / "requirements.txt")], timeout=180)
        run([str(python), "-c", "import websockets.asyncio.client, websockets.asyncio.server"])
        existing = [name for name, data in payloads.items()
                    if (self.runtime / name).exists() and (self.runtime / name).read_bytes() != data]
        if existing:
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup = self.root / "runtime-backups" / stamp
            backup.mkdir(parents=True, mode=0o700)
            for name in files:
                if (self.runtime / name).exists():
                    shutil.copy2(self.runtime / name, backup / name)
        for name, data in payloads.items():
            target = self.runtime / name
            if not target.exists() or target.read_bytes() != data:
                atomic_write(target, data, 0o700 if name == "cpet.py" else 0o600)
        return self.runtime / "cpet.py"

    def install(self):
        self.prepare()
        rc = self.home / ".zshrc"
        # Follow a user's existing dotfile symlink without replacing the symlink.
        target_rc = rc.resolve() if rc.is_symlink() else rc
        original = target_rc.read_text() if target_rc.exists() else ""
        updated = alias_text(original, self.bin)
        source = self.install_runtime()
        if source != self.bin.resolve():
            # This stable installed copy survives checkout moves and removal.
            self.bin.parent.mkdir(parents=True, exist_ok=True)
            temp_link = self.bin.with_name(f".{self.bin.name}.{os.getpid()}.tmp")
            try:
                temp_link.symlink_to(source)
                os.replace(temp_link, self.bin)
            finally:
                temp_link.unlink(missing_ok=True)
        if updated != original:
            if target_rc.exists():
                stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                backup = self.root / f"zshrc.{stamp}.backup"
                shutil.copy2(target_rc, backup)
                print(f".zshrc 已备份：{backup}")
            mode = target_rc.stat().st_mode & 0o777 if target_rc.exists() else 0o600
            atomic_write(target_rc, updated.encode(), mode)
        print(f"已安装：{self.bin}")
        print(f"独立运行目录：{self.runtime}（移动源码仓库不会影响命令）")
        print("新终端可使用 cx 和 cpet。当前终端请运行 source ~/.zshrc。")

    def uninstall(self):
        self.disable()
        self.off()
        rc = self.home / ".zshrc"
        if rc.exists():
            target = rc.resolve()
            original = target.read_text()
            pattern = re.compile(r"(?m)^" + re.escape(BEGIN) + r"\n.*?^" + re.escape(END) + r"\n?", re.S)
            updated, count = pattern.subn("", original)
            if count == 1:
                atomic_write(target, updated.encode(), target.stat().st_mode & 0o777)
        self.server_plist.unlink(missing_ok=True)
        self.bin.unlink(missing_ok=True)
        print(f"已卸载命令、别名和自启；运行副本、备份与日志保留在 {self.root}")


def main():
    if sys.platform != "darwin":
        raise BridgeError("此脚本只用于 macOS。")
    bridge = Bridge()
    args = sys.argv[1:]
    if args and args[0] == "cx":
        sys.exit(launch_cli(bridge, args[1:]))
    parser = argparse.ArgumentParser(description="Codex CLI 与桌面应用共享后台管理")
    parser.add_argument("--version", action="version", version=f"cpet {__version__}")
    parser.add_argument("action", choices=["on", "off", "enable", "disable", "status", "install", "uninstall", "login-start"])
    parser.add_argument("--json", action="store_true", help="status 以 JSON 输出")
    parser.add_argument("--enable", action="store_true", help="install 后启用登录自启")
    options = parser.parse_args(args)
    if options.action == "status":
        info = bridge.status()
        if options.json:
            print(json.dumps(info, indent=2, ensure_ascii=False))
        else:
            print("共享后台：" + ("运行中" if info["server_ready"] else "未就绪"))
            print("登录自启：" + ("已开启" if info["autostart_enabled"] else "已关闭"))
            print("桌面下次启动连接：" + ("已配置" if info["desktop_next_launch_configured"] else "未配置"))
            pet = info["pet"]
            labels = {True: "已展开", False: "已隐藏", None: "无法读取"}
            print("宠物活动气泡：" + labels[pet["activity_visible"]])
            if pet["activity_visible"] is False:
                print("  请点击宠物旁带数字的“显示活动”按钮展开气泡。")
            if pet["overlay_open"] is False:
                print("宠物窗口：已关闭，请在桌面应用中打开宠物。")
            if pet["muted_task_count"]:
                print(f"宠物静音任务：{pet['muted_task_count']} 个；可在任务菜单中取消静音。")
            attachment = info["last_desktop_attachment"]
            if isinstance(attachment, dict):
                labels = {"subscription_confirmed": "桌面日志已确认", "opened_unconfirmed": "已打开任务，日志尚未确认", "open_failed": "打开桌面任务失败"}
                print("最近一次任务订阅：" + labels.get(attachment.get("status"), "未知"))
                print("  检查时间：" + str(attachment.get("checked_at", "未知")))
            else:
                print("最近一次任务订阅：暂无记录；新版 cx 会自动接入桌面任务。")
            print("宠物同步：仍需用运行中的 cx 任务验证；连接成功不代表气泡已显示。")
            print("显示规则：运行中、等待处理、失败或有未读回复的任务；已读空闲任务不显示。")
            print("日志目录：" + info["log_directory"])
    elif options.action == "login-start":
        bridge.on(quiet=True)
    else:
        getattr(bridge, options.action)()
        if options.action == "install" and options.enable:
            bridge.enable()


if __name__ == "__main__":
    try:
        main()
    except (BridgeError, OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        sys.exit(1)
