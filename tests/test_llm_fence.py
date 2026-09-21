"""The fence: nothing in FillerAI reaches for the network on its own.

This is the most important test in the language-model work and the one least
likely to be missed if it were deleted, so it is worth saying why it exists.

The README promises that nothing here talks to a network, and that promise is
what lets FillerAI run inside the locked-down environment where the real form
lives. ``fillerai/llm/`` breaks it deliberately and only when asked. The risk
is not that somebody calls it on purpose; it is that somebody adds
``from .llm import rules`` to the top of a module for convenience, and a
package that needed no key yesterday needs one today, quietly, for every
command including the ones that have nothing to do with a model.

So the fence is a test, not a convention.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: Every module a person could plausibly import without wanting a network.
CORE_MODULES = (
    "fillerai",
    "fillerai.cli",
    "fillerai.auth",
    "fillerai.db",
    "fillerai.dbstore",
    "fillerai.infer",
    "fillerai.schema",
    "fillerai.store",
    "fillerai.extract.html_form",
    "fillerai.extract.spec",
    "fillerai.generate.dataset",
    "fillerai.simulate.run",
    "fillerai.train",
    "fillerai.train.model",
    "fillerai.train.algos",
    "fillerai.web.server",
)


class TestFence(unittest.TestCase):
    def test_no_core_module_imports_the_llm_package(self):
        """Importing the whole project must not pull in fillerai.llm."""
        # A subprocess, because this test file's siblings may well have
        # imported the package already and a check inside this interpreter
        # would pass or fail on test ordering.
        program = (
            "import sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "import importlib\n"
            f"for name in {CORE_MODULES!r}:\n"
            "    importlib.import_module(name)\n"
            "leaked = sorted(m for m in sys.modules if m.startswith('fillerai.llm'))\n"
            "print(','.join(leaked))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        leaked = result.stdout.strip()
        self.assertEqual(
            leaked, "",
            "fillerai.llm was imported by a core module: "
            f"{leaked}. Move the import inside the function that needs it.",
        )

    def test_the_llm_package_imports_without_a_key(self):
        """Nothing is read from the environment at import time."""
        module = importlib.import_module("fillerai.llm")
        self.assertTrue(hasattr(module, "Client"))

    def test_the_package_still_declares_no_dependencies(self):
        """urllib is the whole client; nothing was quietly added."""
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("dependencies = []", text)
        self.assertIn('"fillerai.llm"', text,
                      "fillerai.llm must be listed so it actually ships")

    def test_no_llm_module_is_imported_at_module_scope_anywhere_in_core(self):
        """A cheap textual backstop, for the case the import is conditional."""
        offenders = []
        for path in (ROOT / "fillerai").rglob("*.py"):
            if "llm" in path.parts:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if line.startswith(("import ", "from ")) and ".llm" in line:
                    offenders.append(f"{path.relative_to(ROOT)}:{number}")
        self.assertEqual(offenders, [], f"module-scope llm imports: {offenders}")


if __name__ == "__main__":
    unittest.main()
