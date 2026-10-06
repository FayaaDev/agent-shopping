import os
from pathlib import Path
import tempfile
import unittest
import subprocess
from unittest.mock import patch

import voice_local


class LocalLauncherTests(unittest.TestCase):
    def test_reused_pid_cannot_target_an_unrelated_process(self):
        with patch.object(voice_local.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "/usr/bin/unrelated")):
            self.assertFalse(voice_local.alive(12345))
        with patch.object(voice_local.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "/usr/bin/python /project/voice_local.py")):
            self.assertTrue(voice_local.alive(12345))

    def test_background_child_does_not_mistake_its_own_pid_for_another_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "node_modules/.bin/wrangler"
            binary.parent.mkdir(parents=True)
            binary.touch()
            pid = root / ".local.pid"
            pid.write_text(str(os.getpid()))
            with patch.object(voice_local, "APP", root), patch.object(voice_local, "PID", pid), \
                    patch.object(voice_local, "available", return_value=False), \
                    patch.object(voice_local, "run") as run, patch("sys.argv", ["voice_local.py"]):
                voice_local.main()
            run.assert_called_once()

    def test_generated_secrets_are_stable_private_and_environment_takes_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(voice_local, "APP", root), patch.object(voice_local, "ROOT", root), \
                    patch.dict(os.environ, {"OPENAI_MODEL": "test-model"}, clear=True):
                first = voice_local.configuration()
                second = voice_local.configuration()
            self.assertEqual(first["VOICE_BRIDGE_TOKEN"], second["VOICE_BRIDGE_TOKEN"])
            self.assertEqual(first["LOCAL_SESSION_SECRET"], second["LOCAL_SESSION_SECRET"])
            self.assertNotEqual(first["VOICE_BRIDGE_TOKEN"], first["LOCAL_SESSION_SECRET"])
            self.assertEqual(second["OPENAI_MODEL"], "test-model")
            self.assertEqual((root / ".dev.vars").stat().st_mode & 0o777, 0o600)

    def test_new_speech_key_in_env_local_is_not_shadowed_by_generated_empty_value(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(voice_local, "APP", root), patch.object(voice_local, "ROOT", root), \
                    patch.dict(os.environ, {}, clear=True):
                voice_local.configuration()
                (root / ".env.local").write_text("ELEVENLABS_API_KEY=test-only-key\n")
                self.assertEqual(voice_local.configuration()["ELEVENLABS_API_KEY"], "test-only-key")


if __name__ == "__main__":
    unittest.main()
