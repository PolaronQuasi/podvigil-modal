"""Stamp distributions without modifying source files or changing the package version."""

from pathlib import Path
import json
import sys

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from podvigil.identity import identity


def stamp(destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(identity(ROOT), sort_keys=True) + "\n", encoding="utf-8")


class Build(build_py):
    def run(self):
        super().run()
        stamp(Path(self.build_lib) / "podvigil" / "_build_info.json")


class Source(sdist):
    def make_release_tree(self, base_dir, files):
        super().make_release_tree(base_dir, files)
        stamp(Path(base_dir) / "src" / "podvigil" / "_build_info.json")


setup(cmdclass={"build_py": Build, "sdist": Source})
