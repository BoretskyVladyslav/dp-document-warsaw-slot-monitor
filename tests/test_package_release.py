from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import ModuleType


def _load_package_release() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "package_release.py"
    spec = importlib.util.spec_from_file_location("package_release", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


package_release = _load_package_release()


class PackageReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._write("README.md", "hello")
        self._write(".env.example", "ADMIN_IDS=8015085175\n")
        self._write(".env", "BOT_TOKEN=secret\nADMIN_IDS=1\n")
        self._write("setup.bat", "@echo off\n")
        self._write("START_BOT.bat", "@echo off\n")
        self._write("src/main.py", "print('ok')\n")
        self._write(".venv/lib/site.py", "ignored\n")
        self._write(".git/config", "ignored\n")
        self._write("src/__pycache__/main.cpython-314.pyc", "cache")
        self._write("data/monitor.db", "sqlite")
        self._write("state.sqlite3", "sqlite")
        self._write("CDP_Profile/Default/Cookies", "cookies")
        self._write("debug.log", "log")
        self._write(".cursor/rules/async-python.mdc", "rule")
        self._write("dp_document_bot.zip", "old zip")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, relative: str, contents: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    def test_zip_includes_example_env_and_excludes_local_artifacts(self) -> None:
        output = self.root / "out" / "dp_document_bot.zip"
        package_release.build_release_zip(self.root, output)

        with zipfile.ZipFile(output) as archive:
            names = set(archive.namelist())

        self.assertIn("dp_document_bot/README.md", names)
        self.assertIn("dp_document_bot/.env.example", names)
        self.assertIn("dp_document_bot/src/main.py", names)
        self.assertIn("dp_document_bot/setup.bat", names)
        self.assertIn("dp_document_bot/START_BOT.bat", names)
        self.assertNotIn("dp_document_bot/.env", names)
        self.assertNotIn("dp_document_bot/.venv/lib/site.py", names)
        self.assertNotIn("dp_document_bot/.git/config", names)
        self.assertNotIn("dp_document_bot/src/__pycache__/main.cpython-314.pyc", names)
        self.assertNotIn("dp_document_bot/data/monitor.db", names)
        self.assertNotIn("dp_document_bot/state.sqlite3", names)
        self.assertNotIn("dp_document_bot/CDP_Profile/Default/Cookies", names)
        self.assertNotIn("dp_document_bot/debug.log", names)
        self.assertNotIn("dp_document_bot/.cursor/rules/async-python.mdc", names)
        self.assertNotIn("dp_document_bot/dp_document_bot.zip", names)
        self.assertTrue(all(name.startswith("dp_document_bot/") for name in names))


if __name__ == "__main__":
    unittest.main()
