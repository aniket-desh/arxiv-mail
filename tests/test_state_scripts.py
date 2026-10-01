from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
PUBLISH = ROOT / "scripts" / "publish-state.sh"
RESTORE = ROOT / "scripts" / "restore-state.sh"


def run(*args: str | Path, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(arg) for arg in args],
        cwd=cwd,
        check=check,
        text=True,
        capture_output=True,
        env={key: value for key, value in os.environ.items() if key != "GITHUB_TOKEN"},
    )


class StateScriptTests(unittest.TestCase):
    def test_state_branch_is_historyless_and_restorable(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            origin = root / "origin.git"
            work = root / "work"
            restored = root / "restored"
            run("git", "init", "--bare", origin, cwd=root)
            run("git", "init", work, cwd=root)
            run("git", "config", "user.name", "Test", cwd=work)
            run("git", "config", "user.email", "test@example.com", cwd=work)
            run("git", "remote", "add", "origin", origin, cwd=work)

            state = work / "state" / "state.json"
            state.parent.mkdir()
            state.write_text(json.dumps({"generation": 1}))
            run(PUBLISH, state, cwd=work)
            first = run("git", "--git-dir", origin, "rev-parse", "state", cwd=root).stdout.strip()

            state.write_text(json.dumps({"generation": 2}))
            run(PUBLISH, state, cwd=work)
            second = run("git", "--git-dir", origin, "rev-parse", "state", cwd=root).stdout.strip()
            self.assertNotEqual(first, second)
            commit = run("git", "--git-dir", origin, "cat-file", "-p", second, cwd=root).stdout
            self.assertNotIn("\nparent ", f"\n{commit}")

            run("git", "init", restored, cwd=root)
            run("git", "remote", "add", "origin", origin, cwd=restored)
            restored_state = restored / "state" / "state.json"
            run(RESTORE, restored_state, cwd=restored)
            self.assertEqual(json.loads(restored_state.read_text()), {"generation": 2})
            self.assertEqual(restored_state.stat().st_mode & 0o777, 0o600)

    def test_restore_fails_closed_when_remote_cannot_be_checked(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run("git", "init", root, cwd=root)
            run("git", "remote", "add", "origin", root / "missing.git", cwd=root)
            result = run(RESTORE, root / "state.json", cwd=root, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing to continue", result.stderr)
            self.assertFalse((root / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
