"""Portable path grammar for privacy tests; no files or real user paths are read.

The matrix varies platform, privacy root and nesting independently. Pure paths
give every case a structural oracle (anchor/root/parts), regardless of the host
OS. The same cases exercise claim-text and version-metadata boundaries.
"""
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath


@dataclass(frozen=True)
class PrivatePathCase:
    key: str
    path: str
    family: str
    platform: str
    depth: int


def private_paths():
    cases = []
    for family in ("home", "Users", "mnt", "etc"):
        for depth in (1, 3):
            segments = ("DEMO-PROFILE",) + ("DEMO-ARCHIVE",) * (depth - 1)
            value = PurePosixPath("/", family, *segments, "synthetic-proof.txt")
            assert value.is_absolute() and value.parts[1] == family
            assert len(value.parts) == depth + 3
            cases.append(PrivatePathCase(f"posix-{family}-depth{depth}", str(value), family, "posix", depth))
    for drive in ("C:", "Z:"):
        for depth in (1, 3):
            segments = ("DEMO-PROFILE",) + ("DEMO-ARCHIVE",) * (depth - 1)
            value = PureWindowsPath(drive + "/", "Users", *segments, "synthetic-proof.txt")
            assert value.is_absolute() and value.drive == drive
            assert len(value.parts) == depth + 3
            cases.append(PrivatePathCase(f"windows-{drive[0]}-depth{depth}", str(value), "Users", "windows", depth))
    return tuple(cases)
