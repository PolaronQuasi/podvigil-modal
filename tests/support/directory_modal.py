"""Local directory fixture serving the read-back side of a terminated Sandbox."""

from pathlib import Path
from podvigil.modal_backend.errors import LifecycleError


class DirectoryModal:
    def __init__(self, root, exit_code):
        self.root, self.exit_code = Path(root), exit_code
        self.deleted = False
        self.reads = []
        self.polls = 0

    def poll(self, job):
        self.polls += 1
        return self.exit_code

    def files(self, volume_id):
        files = {}
        for path in self.root.rglob("*"):
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise LifecycleError("Unsupported fixture file type")
            if path.is_file():
                files[path.relative_to(self.root).as_posix()] = path.stat().st_size
        return files

    def read_file(self, volume_id, name):
        self.reads.append(name)
        yield (self.root / name).read_bytes()

    def delete_volume(self, volume_id):
        self.deleted = True

    def volume_exists(self, volume_id):
        return not self.deleted
