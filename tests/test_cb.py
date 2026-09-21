import importlib.util
import shutil
import subprocess
import sys
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location("cb", Path(__file__).resolve().parents[1] / "cb.py")
cb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cb)


class PetDiagnosticsTests(unittest.TestCase):
    def test_resume_uses_cli_supported_bearer_auth_without_url_path(self):
        command = cb.cli_arguments("codex", ["resume", "--all"], "ws://127.0.0.1:4501", auth_token_env="CB_REMOTE_AUTH_TOKEN")
        self.assertEqual(command, ["codex", "resume", "--remote", "ws://127.0.0.1:4501",
                                  "--remote-auth-token-env", "CB_REMOTE_AUTH_TOKEN", "--all"])

    def test_remote_cli_uses_callers_project_and_preserves_explicit_cd(self):
        self.assertEqual(cb.cli_arguments("codex", ["resume", "abc"], "ws://relay", "/a project"),
                         ["codex", "resume", "--remote", "ws://relay", "abc"])
        self.assertEqual(cb.cli_arguments("codex", ["hello"], "ws://relay", "/a project"),
                         ["codex", "--remote", "ws://relay", "--cd", "/a project", "hello"])
        for args in [["-C", "/explicit"], ["--cd=/explicit"], ["-C/explicit"]]:
            command = cb.cli_arguments("codex", args, cwd="/implicit")
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
                command = cb.cli_arguments("codex", extra, "ws://relay", caller)
                self.assertEqual(command, ["codex", "--remote", "ws://relay", *normalized])

    def test_prompt_literals_after_separator_do_not_disable_caller_cwd(self):
        for literal in ["--cd", "--cd=prompt", "-Cprompt", "--remote", "--remote=prompt"]:
            with self.subTest(literal=literal):
                command = cb.cli_arguments("codex", ["--", literal], "ws://relay", "/project A")
                self.assertEqual(command, ["codex", "--remote", "ws://relay", "--cd", "/project A",
                                           "--", literal])
        command = cb.cli_arguments("codex", ["--cd", "--remote"], "ws://relay", "/project A")
        self.assertEqual(command, ["codex", "--remote", "ws://relay", "--cd", "/project A/--remote"])

    def test_missing_or_empty_cd_is_rejected_before_starting_services(self):
        for extra in [["--cd"], ["-C"], ["--cd="], ["--cd", ""]]:
            with self.subTest(extra=extra), self.assertRaises(cb.BridgeError):
                cb.cli_arguments("codex", extra, cwd="/project A")

    def test_resume_and_fork_keep_saved_workspace_unless_explicitly_overridden(self):
        self.assertEqual(cb.cli_arguments("codex", ["resume", "thread-c"], "ws://relay", "/project A"),
                         ["codex", "resume", "--remote", "ws://relay", "thread-c"])
        self.assertEqual(cb.cli_arguments("codex", ["fork", "thread-c"], "ws://relay", "/project A"),
                         ["codex", "fork", "--remote", "ws://relay", "thread-c"])
        self.assertEqual(cb.cli_arguments("codex", ["resume", "-C", "../project B", "thread-c"],
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
            command = cb.cli_arguments("codex", ["--cd", "../linked project"], cwd=str(caller))
            self.assertEqual(command[-1], str(link))
            missing = cb.cli_arguments("codex", ["--cd", "../missing"], cwd=str(caller))
            self.assertEqual(missing[-1], str(root / "missing"))

    def test_getcwd_failure_never_falls_back_to_home_or_runtime(self):
        bridge = Mock()
        with patch.object(cb.os, "getcwd", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                cb.launch_cli(bridge, [])
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
            with patch.object(cb, "__file__", str(project / "cb.py")), \
                    patch.object(cb.os, "getcwd", return_value=str(caller)), \
                    patch.object(cb.select, "select", return_value=([relay.stdout], [], [])), \
                    patch.object(cb.subprocess, "Popen", side_effect=[relay, child]) as popen:
                self.assertEqual(cb.launch_cli(bridge, []), 0)
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
                    cb.PET_ACTIVITY_KEY: False,
                    cb.PET_MUTED_KEY: ["local:local:example"],
                },
                "unrelated": {"preserve": "exactly"},
            }))
            original = path.read_bytes()
            result = cb.pet_settings(directory)
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
                result = cb.pet_settings(directory)
                self.assertFalse(result["settings_readable"])
                self.assertIsNone(result["activity_visible"])

    def test_install_keeps_aliases_and_uses_independent_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = cb.Bridge(directory)
            bridge.app = Path("/Applications/ChatGPT.app")
            original = "# user configuration\nexport EXAMPLE=1\n"
            (Path(directory) / ".zshrc").write_text(original)
            with patch("builtins.print"), patch.object(cb, "run"):
                bridge.install()
                first = (Path(directory) / ".zshrc").read_text()
                bridge.install()
            self.assertTrue(bridge.bin.is_symlink())
            self.assertEqual(bridge.bin.resolve(), (bridge.runtime / "cb.py").resolve())
            self.assertNotEqual(bridge.bin.resolve(), Path(cb.__file__).resolve())
            self.assertEqual((bridge.runtime / "desktop_bridge.py").read_bytes(),
                             (Path(cb.__file__).parent / "desktop_bridge.py").read_bytes())
            self.assertTrue(first.startswith(original))
            self.assertEqual(first, (Path(directory) / ".zshrc").read_text())

    def test_runtime_survives_source_checkout_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "checkout"
            source.mkdir()
            for name in ["cb.py", "desktop_bridge.py", "question_sync.py", "requirements.txt"]:
                shutil.copy2(Path(cb.__file__).parent / name, source / name)
            bridge = cb.Bridge(root / "home")
            bridge.app = Path("/Applications/ChatGPT.app")
            with patch.object(cb, "__file__", str(source / "cb.py")), patch.object(cb, "run"), patch("builtins.print"):
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
            bridge = cb.Bridge(directory)
            bridge.runtime.mkdir(parents=True)
            old = b"# previous installed version\n"
            installed = bridge.runtime / "cb.py"
            installed.write_bytes(old)
            with patch.object(cb, "run", side_effect=cb.BridgeError("dependency installation failed")):
                with self.assertRaises(cb.BridgeError):
                    bridge.install_runtime()
            self.assertEqual(installed.read_bytes(), old)




class CommandRoutingTests(unittest.TestCase):
    def test_cli_default_resume_and_explicit_escape(self):
        for args, expected in [([], []), (['resume', 'example'], ['resume', 'example']),
                               (['cli', 'status'], ['status']), (['cli', '--help'], ['--help']),
                               (['cli', '--version'], ['--version'])]:
            with self.subTest(args=args), patch.object(cb.sys, 'platform', 'darwin'), \
                    patch.object(cb.sys, 'argv', ['cb', *args]), patch.object(cb, 'Bridge') as bridge, \
                    patch.object(cb, 'launch_cli', return_value=0) as launch:
                with self.assertRaises(SystemExit) as exit_info:
                    cb.main()
                self.assertEqual(exit_info.exception.code, 0)
                launch.assert_called_once_with(bridge.return_value, expected)

    def test_management_and_help_never_launch_cli(self):
        for args in [['off'], ['status', '--json'], ['--help'], ['--version']]:
            with self.subTest(args=args), patch.object(cb.sys, 'platform', 'darwin'), \
                    patch.object(cb.sys, 'argv', ['cb', *args]), patch.object(cb, 'Bridge') as bridge, \
                    patch.object(cb, 'launch_cli') as launch, patch('builtins.print'):
                bridge.return_value.status.return_value = {}
                if args[0].startswith('--'):
                    with self.assertRaises(SystemExit) as exit_info:
                        cb.main()
                    self.assertEqual(exit_info.exception.code, 0)
                else:
                    cb.main()
                    getattr(bridge.return_value, args[0]).assert_called_once()
                launch.assert_not_called()

    def test_upgrade_removes_old_aliases_link_and_updates_next_login(self):
        import plistlib
        with tempfile.TemporaryDirectory() as directory:
            bridge = cb.Bridge(directory)
            bridge.app = Path('/Applications/ChatGPT.app')
            old = bridge.home / '.local/bin/codex-pet'
            old.parent.mkdir(parents=True)
            bridge.runtime.mkdir(parents=True)
            (bridge.runtime / 'cpet.py').write_text('# old runtime\n')
            old.symlink_to(bridge.runtime / 'cpet.py')
            (bridge.home / '.zshrc').write_text(
                '# user\n' + cb.BEGIN + '\nalias cx="old cx"\nalias cpet=old\n' + cb.END + '\n')
            bridge.login_plist.parent.mkdir(parents=True)
            bridge.login_plist.write_bytes(plistlib.dumps({
                'Label': cb.LOGIN_LABEL, 'ProgramArguments': ['python3', str(old), 'login-start']}))
            with patch.object(cb, 'run') as run, patch('builtins.print'):
                bridge.install()
                bridge.install()
            aliases = (bridge.home / '.zshrc').read_text()
            self.assertIn('alias cb=', aliases)
            self.assertNotIn('alias cx=', aliases)
            self.assertNotIn('alias cpet=', aliases)
            self.assertFalse(old.is_symlink())
            self.assertEqual(bridge.bin.name, 'cb')
            self.assertEqual(bridge.bin.resolve(), (bridge.runtime / 'cb.py').resolve())
            config = plistlib.loads(bridge.login_plist.read_bytes())
            self.assertEqual(config['ProgramArguments'], ['python3', str(bridge.bin), 'login-start'])
            self.assertFalse(any('launchctl' in str(call) for call in run.call_args_list))

    def test_install_refuses_user_cb_alias_even_outside_old_managed_block(self):
        original = 'alias cb=mine\n' + cb.BEGIN + '\nalias cx=old\n' + cb.END + '\n'
        with self.assertRaises(cb.BridgeError):
            cb.alias_text(original, '/example/cb')

    def test_install_preserves_unrelated_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = cb.Bridge(directory)
            bridge.app = Path('/Applications/ChatGPT.app')
            bridge.bin.parent.mkdir(parents=True)
            bridge.bin.write_text('user command\n')
            with patch.object(bridge, 'install_runtime') as install:
                with self.assertRaises(cb.BridgeError):
                    bridge.install()
                install.assert_not_called()
            self.assertEqual(bridge.bin.read_text(), 'user command\n')


if __name__ == "__main__":
    unittest.main()
