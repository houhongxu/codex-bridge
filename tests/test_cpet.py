import importlib.util
import shutil
import subprocess
import sys
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("cpet", Path(__file__).resolve().parents[1] / "cpet.py")
cpet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cpet)


class PetDiagnosticsTests(unittest.TestCase):
    def test_resume_uses_cli_supported_bearer_auth_without_url_path(self):
        command = cpet.cli_arguments("codex", ["resume", "--all"], "ws://127.0.0.1:4501", auth_token_env="CPET_REMOTE_AUTH_TOKEN")
        self.assertEqual(command, ["codex", "resume", "--remote", "ws://127.0.0.1:4501",
                                  "--remote-auth-token-env", "CPET_REMOTE_AUTH_TOKEN", "--all"])

    def test_remote_cli_uses_callers_project_and_preserves_explicit_cd(self):
        self.assertEqual(cpet.cli_arguments("codex", ["resume", "abc"], "ws://relay", "/a project"),
                         ["codex", "resume", "--remote", "ws://relay", "abc"])
        self.assertEqual(cpet.cli_arguments("codex", ["hello"], "ws://relay", "/a project"),
                         ["codex", "--remote", "ws://relay", "--cd", "/a project", "hello"])
        for args in [["-C", "/explicit"], ["--cd=/explicit"], ["-C/explicit"]]:
            command = cpet.cli_arguments("codex", args, cwd="/implicit")
            self.assertNotIn("/implicit", command)
            self.assertEqual(command[-len(args):], args)

    def test_hidden_activity_and_muted_tasks_are_reported_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".codex/.codex-global-state.json"
            path.parent.mkdir()
            path.write_text(json.dumps({
                "electron-avatar-overlay-open": True,
                "electron-persisted-atom-state": {
                    cpet.PET_ACTIVITY_KEY: False,
                    cpet.PET_MUTED_KEY: ["local:local:example"],
                },
                "unrelated": {"preserve": "exactly"},
            }))
            original = path.read_bytes()
            result = cpet.pet_settings(directory)
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
                result = cpet.pet_settings(directory)
                self.assertFalse(result["settings_readable"])
                self.assertIsNone(result["activity_visible"])

    def test_install_keeps_aliases_and_uses_independent_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = cpet.Bridge(directory)
            bridge.app = Path("/Applications/ChatGPT.app")
            original = "# user configuration\nexport EXAMPLE=1\n"
            (Path(directory) / ".zshrc").write_text(original)
            with patch("builtins.print"), patch.object(cpet, "run"):
                bridge.install()
                first = (Path(directory) / ".zshrc").read_text()
                bridge.install()
            self.assertTrue(bridge.bin.is_symlink())
            self.assertEqual(bridge.bin.resolve(), (bridge.runtime / "cpet.py").resolve())
            self.assertNotEqual(bridge.bin.resolve(), Path(cpet.__file__).resolve())
            self.assertEqual((bridge.runtime / "desktop_bridge.py").read_bytes(),
                             (Path(cpet.__file__).parent / "desktop_bridge.py").read_bytes())
            self.assertTrue(first.startswith(original))
            self.assertEqual(first, (Path(directory) / ".zshrc").read_text())

    def test_runtime_survives_source_checkout_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "checkout"
            source.mkdir()
            for name in ["cpet.py", "desktop_bridge.py", "question_sync.py", "requirements.txt"]:
                shutil.copy2(Path(cpet.__file__).parent / name, source / name)
            bridge = cpet.Bridge(root / "home")
            bridge.app = Path("/Applications/ChatGPT.app")
            with patch.object(cpet, "__file__", str(source / "cpet.py")), patch.object(cpet, "run"), patch("builtins.print"):
                bridge.install()
            shutil.rmtree(source)
            self.assertTrue(bridge.bin.exists())
            self.assertTrue((bridge.runtime / "question_sync.py").is_file())
            # Import the actual installed file in a fresh process, after removing
            # its source checkout; this also works in the Linux unit-test job.
            code = "import runpy, sys; assert runpy.run_path(sys.argv[1])['__version__']"
            subprocess.run([sys.executable, "-c", code, str(bridge.bin)], check=True)

    def test_failed_dependency_install_preserves_working_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = cpet.Bridge(directory)
            bridge.runtime.mkdir(parents=True)
            old = b"# previous installed version\n"
            installed = bridge.runtime / "cpet.py"
            installed.write_bytes(old)
            with patch.object(cpet, "run", side_effect=cpet.BridgeError("dependency installation failed")):
                with self.assertRaises(cpet.BridgeError):
                    bridge.install_runtime()
            self.assertEqual(installed.read_bytes(), old)


if __name__ == "__main__":
    unittest.main()
