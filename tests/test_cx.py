import importlib.util
import io
import shutil
import subprocess
import sys
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location("cx", Path(__file__).resolve().parents[1] / "cx.py")
cx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cx)


def make_bundle(root, relative="Contents/Resources/codex-cli/bin/codex", name="ChatGPT.app"):
    app = Path(root) / name
    executable = app / relative
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.touch()
    return app, executable


class BundleDiscoveryTests(unittest.TestCase):
    def test_new_and_legacy_layouts_support_detection_install_and_launch_paths(self):
        for relative in ["Contents/Resources/codex-cli/bin/codex", "Contents/Resources/codex"]:
            for name in ["ChatGPT.app", "Codex.app"]:
                with self.subTest(relative=relative, name=name), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    app, executable = make_bundle(root, relative, name)
                    with patch.object(cx, "APPLICATION_PATHS", (root / "ChatGPT.app", root / "Codex.app")), \
                            patch.object(cx, "run") as run, patch("builtins.print"):
                        bridge = cx.Bridge(root / "home")
                        self.assertEqual(bridge.app, app)
                        self.assertEqual(bridge.cli, str(executable))
                        self.assertEqual(bridge.server_config()["ProgramArguments"][0], str(executable))
                        self.assertEqual(cx.cli_arguments(bridge.cli, ["--version"])[0], str(executable))
                        bridge.install()
                    self.assertTrue(bridge.bin.is_symlink())
                    self.assertFalse(any("launchctl" in str(call) or "/usr/bin/open" in str(call)
                                         for call in run.call_args_list))

    def test_current_layout_takes_precedence_and_falls_back_after_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            app, current = make_bundle(directory)
            _, legacy = make_bundle(directory, "Contents/Resources/codex")
            bridge = cx.Bridge(Path(directory) / "home")
            bridge.app = app
            self.assertEqual(bridge.cli, str(current))
            current.unlink()
            self.assertEqual(bridge.cli, str(legacy))

    def test_missing_binary_is_rejected_before_install_or_service_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            apps = (root / "ChatGPT.app", root / "Codex.app")
            (apps[0] / "Contents/Resources/codex-cli/bin/codex").mkdir(parents=True)
            with patch.object(cx, "APPLICATION_PATHS", apps), patch.object(cx, "run") as run:
                bridge = cx.Bridge(root / "home")
                self.assertIsNone(bridge.app)
                for operation in [lambda: bridge.cli, bridge.install, bridge.start_server]:
                    with self.assertRaises(cx.BridgeError):
                        operation()
                run.assert_not_called()
                self.assertFalse(bridge.root.exists())

    def test_healthy_loaded_server_keeps_existing_plist_and_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app, _ = make_bundle(root)
            bridge = cx.Bridge(root / "home")
            bridge.app = app
            bridge.root.mkdir(parents=True)
            original = b"existing legacy server configuration\n"
            bridge.server_plist.write_bytes(original)
            with patch.object(bridge, "loaded", return_value=True), \
                    patch.object(bridge, "healthy", return_value=True), patch.object(cx, "run") as run:
                bridge.start_server()
                run.assert_not_called()
            self.assertEqual(bridge.server_plist.read_bytes(), original)


class DesktopConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bridge = cx.Bridge(self.temp.name)
        self.bridge.app, _ = make_bundle(self.temp.name)
        (self.bridge.app / "Contents/Info.plist").write_bytes(cx.plistlib.dumps({"CFBundleExecutable": "ChatGPT"}))
        self.main = str(self.bridge.app / "Contents/MacOS/ChatGPT")
        self.processes = subprocess.CompletedProcess([], 0,
            f"123 {self.main}\n124 {self.bridge.app}/Contents/Resources/codex app-server\n", "")

    def probe(self, output="", code=0, error=""):
        sockets = subprocess.CompletedProcess([], code, output, error)
        with patch.object(cx, "run", side_effect=[self.processes, sockets]) as run:
            state = self.bridge.desktop_connection()
        self.assertIn("123", run.call_args_list[1].args[0])
        self.assertNotIn("124", run.call_args_list[1].args[0])
        return state

    def test_only_main_desktop_outgoing_established_socket_counts(self):
        self.assertEqual(self.probe("p123\nn127.0.0.1:63254->127.0.0.1:4500\nTST=ESTABLISHED\n"), "connected")
        for output in ("p124\nn127.0.0.1:63254->127.0.0.1:4500\n",
                       "p123\nn127.0.0.1:4500->127.0.0.1:63254\n",
                       "p123\nn127.0.0.1:63254->127.0.0.1:4501\n"):
            with self.subTest(output=output):
                self.assertEqual(self.probe(output), "disconnected")
        self.assertEqual(self.probe(code=1), "disconnected")

    def test_no_running_app_and_probe_failures_are_distinct(self):
        with patch.object(cx, "run", return_value=subprocess.CompletedProcess([], 0, "999 unrelated\n", "")) as run:
            self.assertEqual(self.bridge.desktop_connection(), "not_running")
            self.assertEqual(run.call_count, 1)
        for result in (subprocess.CompletedProcess([], 1, "", "operation not permitted"),
                       subprocess.CompletedProcess([], 0, "", "permission denied")):
            with self.subTest(result=result), patch.object(cx, "run", return_value=result):
                self.assertEqual(self.bridge.desktop_connection(), "unknown")
        self.assertEqual(self.probe(code=1, error="permission denied"), "unknown")
        self.assertEqual(self.probe(code=2), "unknown")
        with patch.object(cx, "run", side_effect=cx.BridgeError("probe timed out")):
            self.assertEqual(self.bridge.desktop_connection(), "unknown")
        (self.bridge.app / "Contents/Info.plist").unlink()
        self.assertEqual(self.bridge.desktop_connection(), "unknown")

    def test_brief_startup_wait_accepts_late_connection_and_connected_returns_immediately(self):
        with patch.object(self.bridge, "_desktop_connection", side_effect=["not_running", "disconnected", "connected"]), \
                patch.object(cx.time, "sleep") as sleep:
            self.assertEqual(self.bridge.desktop_connection(wait=2), "connected")
            self.assertEqual(sleep.call_count, 2)
        for state in ("connected", "unknown"):
            with self.subTest(state=state), patch.object(self.bridge, "_desktop_connection", return_value=state), \
                    patch.object(cx.time, "sleep") as sleep:
                self.assertEqual(self.bridge.desktop_connection(wait=2), state)
                sleep.assert_not_called()
        with patch.object(self.bridge, "_desktop_connection", return_value="disconnected"), \
                patch.object(cx.time, "monotonic", side_effect=[0, 3]), patch.object(cx.time, "sleep") as sleep:
            self.assertEqual(self.bridge.desktop_connection(wait=2), "disconnected")
            sleep.assert_not_called()

    def test_quiet_on_warns_once_without_blocking_or_changing_launch(self):
        for state in ("connected", "disconnected", "not_running", "unknown"):
            with self.subTest(state=state), patch.object(self.bridge, "start_server"), \
                    patch.object(self.bridge, "set_desktop_env"), patch.object(cx, "run") as run, \
                    patch.object(self.bridge, "desktop_connection", return_value=state) as connection, \
                    patch.object(cx.sys, "stderr", new_callable=io.StringIO) as stderr, \
                    patch.object(cx.sys, "stdout", new_callable=io.StringIO) as stdout:
                self.bridge.on(quiet=True)
                connection.assert_called_once_with(wait=2)
                run.assert_called_once_with(["/usr/bin/open", "-g", "--env", f"{cx.ENV_KEY}={cx.ENDPOINT}",
                                             "-a", str(self.bridge.app)])
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue().count("提示："), int(state != "connected"))
                if state == "disconnected":
                    self.assertIn("完全退出桌面（不是关闭窗口），然后运行 cx on", stderr.getvalue())
                if state == "unknown":
                    self.assertIn("无法确认", stderr.getvalue())
                    self.assertNotIn("当前桌面未接入", stderr.getvalue())

    def test_status_separates_observed_connection_from_next_launch_config(self):
        for state in ("connected", "disconnected", "not_running", "unknown"):
            with self.subTest(state=state), patch.object(self.bridge, "loaded", return_value=True), \
                    patch.object(self.bridge, "healthy", return_value=True), \
                    patch.object(self.bridge, "launch_env", return_value=cx.ENDPOINT), \
                    patch.object(self.bridge, "desktop_connection", return_value=state):
                data = self.bridge.status()
                self.assertTrue(data["desktop_next_launch_configured"])
                self.assertEqual(data["desktop_connection"], state)
                self.assertEqual(data["desktop_connected"], None if state == "unknown" else state == "connected")
                self.assertIn("last_desktop_attachment", data)


class PetDiagnosticsTests(unittest.TestCase):
    def test_resume_uses_cli_supported_bearer_auth_without_url_path(self):
        command = cx.cli_arguments("codex", ["resume", "--all"], "ws://127.0.0.1:4501", auth_token_env="CB_REMOTE_AUTH_TOKEN")
        self.assertEqual(command, ["codex", "resume", "--remote", "ws://127.0.0.1:4501",
                                  "--remote-auth-token-env", "CB_REMOTE_AUTH_TOKEN", "--all"])

    def test_remote_cli_uses_callers_project_and_preserves_explicit_cd(self):
        self.assertEqual(cx.cli_arguments("codex", ["resume", "abc"], "ws://relay", "/a project"),
                         ["codex", "resume", "--remote", "ws://relay", "abc"])
        self.assertEqual(cx.cli_arguments("codex", ["hello"], "ws://relay", "/a project"),
                         ["codex", "--remote", "ws://relay", "--cd", "/a project", "hello"])
        for args in [["-C", "/explicit"], ["--cd=/explicit"], ["-C/explicit"]]:
            command = cx.cli_arguments("codex", args, cwd="/implicit")
            self.assertNotIn("/implicit", command)
            self.assertEqual(command[-len(args):], args)

    def test_relative_cd_is_resolved_once_against_invoking_directory(self):
        caller = "/work/project A"
        expected = "/work/项目 B/subdir"
        cases = [
            (["--cd", "../项目 B/subdir"], ["--cd", expected]),
            (["--cd=../项目 B/subdir"], ["--cd=" + expected]),
            (["-C", "../项目 B/subdir"], ["-C", expected]),
            (["-C../项目 B/subdir"], ["-C" + expected]),
        ]
        for extra, normalized in cases:
            with self.subTest(extra=extra):
                command = cx.cli_arguments("codex", extra, "ws://relay", caller)
                self.assertEqual(command, ["codex", "--remote", "ws://relay", *normalized])

    def test_prompt_literals_after_separator_do_not_disable_caller_cwd(self):
        for literal in ["--cd", "--cd=prompt", "-Cprompt", "--remote", "--remote=prompt"]:
            with self.subTest(literal=literal):
                command = cx.cli_arguments("codex", ["--", literal], "ws://relay", "/project A")
                self.assertEqual(command, ["codex", "--remote", "ws://relay", "--cd", "/project A",
                                           "--", literal])
        command = cx.cli_arguments("codex", ["--cd", "--remote"], "ws://relay", "/project A")
        self.assertEqual(command, ["codex", "--remote", "ws://relay", "--cd", "/project A/--remote"])

    def test_missing_or_empty_cd_is_rejected_before_starting_services(self):
        for extra in [["--cd"], ["-C"], ["--cd="], ["--cd", ""]]:
            with self.subTest(extra=extra), self.assertRaises(cx.BridgeError):
                cx.cli_arguments("codex", extra, cwd="/project A")

    def test_resume_and_fork_keep_saved_workspace_unless_explicitly_overridden(self):
        self.assertEqual(cx.cli_arguments("codex", ["resume", "thread-c"], "ws://relay", "/project A"),
                         ["codex", "resume", "--remote", "ws://relay", "thread-c"])
        self.assertEqual(cx.cli_arguments("codex", ["fork", "thread-c"], "ws://relay", "/project A"),
                         ["codex", "fork", "--remote", "ws://relay", "thread-c"])
        self.assertEqual(cx.cli_arguments("codex", ["resume", "-C", "../project B", "thread-c"],
                                          "ws://relay", "/project A"),
                         ["codex", "resume", "--remote", "ws://relay", "-C", "/project B", "thread-c"])

    def test_symlink_and_missing_paths_are_not_canonicalized_or_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            caller = root / "项目 A"
            target = root / "项目 B"
            caller.mkdir()
            target.mkdir()
            link = root / "linked project"
            link.symlink_to(target, target_is_directory=True)
            command = cx.cli_arguments("codex", ["--cd", "../linked project"], cwd=str(caller))
            self.assertEqual(command[-1], str(link))
            missing = cx.cli_arguments("codex", ["--cd", "../missing"], cwd=str(caller))
            self.assertEqual(missing[-1], str(root / "missing"))

    def test_getcwd_failure_never_falls_back_to_home_or_runtime(self):
        bridge = Mock()
        with patch.object(cx.os, "getcwd", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                cx.launch_cli(bridge, [])
        bridge.on.assert_not_called()

    def test_cli_subprocess_uses_the_captured_invocation_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "runtime"
            caller = root / "project A"
            (project / ".venv/bin").mkdir(parents=True)
            (project / ".venv/bin/python").touch()
            caller.mkdir()
            bridge = Mock(cli="codex", root=root, app=Path("/Applications/Codex.app"))
            relay = Mock()
            relay.stdout.readline.return_value = json.dumps({
                "endpoint": "ws://127.0.0.1:49123", "auth_token": "test-token"}) + "\n"
            child = Mock()
            child.wait.return_value = 0
            with patch.object(cx, "__file__", str(project / "cx.py")), \
                    patch.object(cx.os, "getcwd", return_value=str(caller)), \
                    patch.object(cx.select, "select", return_value=([relay.stdout], [], [])), \
                    patch.object(cx.subprocess, "Popen", side_effect=[relay, child]) as popen:
                self.assertEqual(cx.launch_cli(bridge, []), 0)
            command = popen.call_args_list[1]
            self.assertEqual(command.kwargs["cwd"], str(caller))
            self.assertIn(str(caller), command.args[0])

    def test_hidden_activity_and_muted_tasks_are_reported_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".codex/.codex-global-state.json"
            path.parent.mkdir()
            path.write_text(json.dumps({
                "electron-avatar-overlay-open": True,
                "electron-persisted-atom-state": {
                    cx.PET_ACTIVITY_KEY: False,
                    cx.PET_MUTED_KEY: ["local:local:example"],
                },
                "unrelated": {"preserve": "exactly"},
            }))
            original = path.read_bytes()
            result = cx.pet_settings(directory)
            self.assertFalse(result["activity_visible"])
            self.assertTrue(result["overlay_open"])
            self.assertEqual(result["muted_task_count"], 1)
            self.assertEqual(path.read_bytes(), original)

    def test_missing_or_invalid_state_does_not_claim_pet_is_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".codex/.codex-global-state.json"
            path.parent.mkdir()
            for content in [None, "{", "[]", '{"electron-persisted-atom-state": null}']:
                if content is not None:
                    path.write_text(content)
                result = cx.pet_settings(directory)
                self.assertFalse(result["settings_readable"])
                self.assertIsNone(result["activity_visible"])

    def test_install_keeps_aliases_and_uses_independent_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = cx.Bridge(directory)
            bridge.app, _ = make_bundle(directory)
            original = "# user configuration\nexport EXAMPLE=1\n"
            (Path(directory) / ".zshrc").write_text(original)
            with patch("builtins.print"), patch.object(cx, "run"):
                bridge.install()
                first = (Path(directory) / ".zshrc").read_text()
                bridge.install()
            self.assertTrue(bridge.bin.is_symlink())
            self.assertEqual(bridge.bin.resolve(), (bridge.runtime / "cx.py").resolve())
            self.assertNotEqual(bridge.bin.resolve(), Path(cx.__file__).resolve())
            self.assertEqual((bridge.runtime / "desktop_bridge.py").read_bytes(),
                             (Path(cx.__file__).parent / "desktop_bridge.py").read_bytes())
            self.assertTrue(first.startswith(original))
            self.assertEqual(first, (Path(directory) / ".zshrc").read_text())

    def test_runtime_survives_source_checkout_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "checkout"
            source.mkdir()
            for name in ["cx.py", "desktop_bridge.py", "question_sync.py", "requirements.txt"]:
                shutil.copy2(Path(cx.__file__).parent / name, source / name)
            bridge = cx.Bridge(root / "home")
            bridge.app, _ = make_bundle(root)
            with patch.object(cx, "__file__", str(source / "cx.py")), patch.object(cx, "run"), patch("builtins.print"):
                bridge.install()
            shutil.rmtree(source)
            self.assertTrue(bridge.bin.exists())
            self.assertTrue((bridge.runtime / "question_sync.py").is_file())
            # Import the actual installed file in a fresh process, after removing
            # its source checkout; this also works in the Linux unit-test job.
            code = ("import json, runpy, sys; m=runpy.run_path(sys.argv[1]); "
                    "print(json.dumps(m['cli_arguments']('codex', [], 'ws://relay', sys.argv[2])))")
            project = root / "installed entry project"
            project.mkdir()
            result = subprocess.run([sys.executable, "-c", code, str(bridge.bin), str(project)],
                                    check=True, text=True, capture_output=True)
            self.assertEqual(json.loads(result.stdout),
                             ["codex", "--remote", "ws://relay", "--cd", str(project)])

    def test_failed_dependency_install_preserves_working_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = cx.Bridge(directory)
            bridge.runtime.mkdir(parents=True)
            old = b"# previous installed version\n"
            installed = bridge.runtime / "cx.py"
            installed.write_bytes(old)
            with patch.object(cx, "run", side_effect=cx.BridgeError("dependency installation failed")):
                with self.assertRaises(cx.BridgeError):
                    bridge.install_runtime()
            self.assertEqual(installed.read_bytes(), old)




class CommandRoutingTests(unittest.TestCase):
    def test_cli_default_resume_and_explicit_escape(self):
        for args, expected in [([], []), (['resume', 'example'], ['resume', 'example']),
                               (['cli', 'status'], ['status']), (['cli', '--help'], ['--help']),
                               (['cli', '--version'], ['--version'])]:
            with self.subTest(args=args), patch.object(cx.sys, 'platform', 'darwin'), \
                    patch.object(cx.sys, 'argv', ['cx', *args]), patch.object(cx, 'Bridge') as bridge, \
                    patch.object(cx, 'launch_cli', return_value=0) as launch:
                with self.assertRaises(SystemExit) as exit_info:
                    cx.main()
                self.assertEqual(exit_info.exception.code, 0)
                launch.assert_called_once_with(bridge.return_value, expected)

    def test_management_and_help_never_launch_cli(self):
        for args in [['off'], ['status', '--json'], ['--help'], ['--version']]:
            with self.subTest(args=args), patch.object(cx.sys, 'platform', 'darwin'), \
                    patch.object(cx.sys, 'argv', ['cx', *args]), patch.object(cx, 'Bridge') as bridge, \
                    patch.object(cx, 'launch_cli') as launch, patch('builtins.print'):
                bridge.return_value.status.return_value = {}
                if args[0].startswith('--'):
                    with self.assertRaises(SystemExit) as exit_info:
                        cx.main()
                    self.assertEqual(exit_info.exception.code, 0)
                else:
                    cx.main()
                    getattr(bridge.return_value, args[0]).assert_called_once()
                launch.assert_not_called()

    def test_upgrade_removes_old_aliases_link_and_updates_next_login(self):
        import plistlib
        with tempfile.TemporaryDirectory() as directory:
            bridge = cx.Bridge(directory)
            bridge.app, _ = make_bundle(directory)
            old = bridge.home / '.local/bin/codex-pet'
            old.parent.mkdir(parents=True)
            bridge.runtime.mkdir(parents=True)
            (bridge.runtime / 'cpet.py').write_text('# old runtime\n')
            old.symlink_to(bridge.runtime / 'cpet.py')
            (bridge.home / '.zshrc').write_text(
                '# user\n' + cx.BEGIN + '\nalias cx="old cx"\nalias cpet=old\n' + cx.END + '\n')
            bridge.login_plist.parent.mkdir(parents=True)
            bridge.login_plist.write_bytes(plistlib.dumps({
                'Label': cx.LOGIN_LABEL, 'ProgramArguments': ['python3', str(old), 'login-start']}))
            with patch.object(cx, 'run') as run, patch('builtins.print'):
                bridge.install()
                bridge.install()
            aliases = (bridge.home / '.zshrc').read_text()
            self.assertIn('alias cx=', aliases)
            self.assertNotIn('alias cb=', aliases)
            self.assertNotIn('alias cpet=', aliases)
            self.assertFalse(old.is_symlink())
            self.assertEqual(bridge.bin.name, 'cx')
            self.assertEqual(bridge.bin.resolve(), (bridge.runtime / 'cx.py').resolve())
            config = plistlib.loads(bridge.login_plist.read_bytes())
            self.assertEqual(config['ProgramArguments'], ['python3', str(bridge.bin), 'login-start'])
            self.assertFalse(any('launchctl' in str(call) for call in run.call_args_list))

    def test_install_refuses_user_cx_alias_even_outside_old_managed_block(self):
        original = 'alias cx=mine\n' + cx.BEGIN + '\nalias cx=old\n' + cx.END + '\n'
        with self.assertRaises(cx.BridgeError):
            cx.alias_text(original, '/example/cx')

    def test_cb_upgrade_migrates_alias_link_and_login_without_restarting(self):
        import plistlib
        with tempfile.TemporaryDirectory() as directory:
            bridge = cx.Bridge(directory)
            bridge.app, _ = make_bundle(directory)
            old = bridge.home / '.local/bin/cb'
            old.parent.mkdir(parents=True)
            bridge.runtime.mkdir(parents=True)
            script = bridge.runtime / 'cb.py'
            script.write_text('# old runtime\n')
            old.symlink_to(script)
            original = '# user setting\nexport CB_QUESTION_SYNC=0\n'
            (bridge.home / '.zshrc').write_text(
                original + cx.BEGIN + '\nalias cb=old\n' + cx.END + '\n')
            bridge.login_plist.parent.mkdir(parents=True)
            bridge.login_plist.write_bytes(plistlib.dumps({
                'Label': cx.LOGIN_LABEL, 'ProgramArguments': ['python3', str(old), 'login-start']}))
            with patch.object(cx, 'run') as run, patch('builtins.print'):
                bridge.install()
                bridge.install()
            aliases = (bridge.home / '.zshrc').read_text()
            self.assertTrue(aliases.startswith(original))
            self.assertEqual(aliases.count('alias cx='), 1)
            self.assertNotIn('alias cb=', aliases)
            self.assertFalse(old.is_symlink())
            self.assertEqual(script.read_text(), '# old runtime\n')
            self.assertEqual(bridge.bin.resolve(), (bridge.runtime / 'cx.py').resolve())
            config = plistlib.loads(bridge.login_plist.read_bytes())
            self.assertEqual(config['ProgramArguments'], ['python3', str(bridge.bin), 'login-start'])
            self.assertFalse(any('launchctl' in str(call) for call in run.call_args_list))

    def test_upgrade_preserves_user_owned_cb_file_or_unrelated_symlink(self):
        for symlink in [False, True]:
            with self.subTest(symlink=symlink), tempfile.TemporaryDirectory() as directory:
                bridge = cx.Bridge(directory)
                bridge.app, _ = make_bundle(directory)
                old = bridge.home / '.local/bin/cb'
                old.parent.mkdir(parents=True)
                if symlink:
                    target = bridge.home / 'user-script.py'
                    target.write_text('user command\n')
                    old.symlink_to(target)
                else:
                    old.write_text('user command\n')
                with patch.object(cx, 'run'), patch('builtins.print'):
                    bridge.install()
                self.assertEqual(old.is_symlink(), symlink)
                self.assertEqual(old.read_text(), 'user command\n')

    def test_dependency_failure_keeps_previous_cb_installation_usable(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = cx.Bridge(directory)
            bridge.app, _ = make_bundle(directory)
            old = bridge.home / '.local/bin/cb'
            old.parent.mkdir(parents=True)
            bridge.runtime.mkdir(parents=True)
            script = bridge.runtime / 'cb.py'
            script.write_text('# old runtime\n')
            old.symlink_to(script)
            original = cx.BEGIN + '\nalias cb=old\n' + cx.END + '\n'
            (bridge.home / '.zshrc').write_text(original)
            with patch.object(cx, 'run', side_effect=cx.BridgeError('dependency installation failed')):
                with self.assertRaises(cx.BridgeError):
                    bridge.install()
            self.assertEqual((bridge.home / '.zshrc').read_text(), original)
            self.assertEqual(old.resolve(), script.resolve())
            self.assertFalse(bridge.bin.is_symlink())

    def test_install_preserves_unrelated_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = cx.Bridge(directory)
            bridge.app, _ = make_bundle(directory)
            bridge.bin.parent.mkdir(parents=True)
            bridge.bin.write_text('user command\n')
            with patch.object(bridge, 'install_runtime') as install:
                with self.assertRaises(cx.BridgeError):
                    bridge.install()
                install.assert_not_called()
            self.assertEqual(bridge.bin.read_text(), 'user command\n')


if __name__ == "__main__":
    unittest.main()
