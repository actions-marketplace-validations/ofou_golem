"""The frozen licence: what any tool may be, read, import, and spend.

The licence is read once per run and its sha256 is printed before and after.
Nothing in Golem writes it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

REQUIRED = (
    "name",
    "shapes",
    "access",
    "forbidden_imports",
    "forbidden_calls",
    "sandbox",
    "budget",
    "models",
)


@dataclass(frozen=True)
class Licence:
    path: Path
    raw: bytes
    data: dict

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()

    def budget(self, key: str):
        return self.data["budget"][key]

    def model(self, role: str) -> str:
        return self.data["models"][role]


def load(path: Path) -> Licence:
    """Read the licence. Whole-line // comments are allowed; the sha256 covers the raw bytes, comments included."""
    raw = Path(path).read_bytes()
    text = "\n".join(
        line
        for line in raw.decode("utf-8").splitlines()
        if not line.lstrip().startswith("//")
    )
    data = json.loads(text)
    missing = [key for key in REQUIRED if key not in data]
    if missing:
        raise ValueError(f"licence {path} is missing {', '.join(missing)}")
    return Licence(path=Path(path), raw=raw, data=data)


def unchanged(licence: Licence) -> bool:
    return licence.path.read_bytes() == licence.raw


def violations(manifest: dict, licence: Licence) -> list[str]:
    """Reasons a manifest asks for more than the licence grants. Empty means inside it."""
    found: list[str] = []
    shape = manifest.get("shape")
    if shape not in licence.data["shapes"]:
        found.append(
            f"new authority: shape {shape!r} is outside the licence {licence.data['shapes']}"
        )
    access = manifest.get("access")
    if access not in licence.data["access"]:
        found.append(
            f"new authority: access {access!r} is outside the licence {licence.data['access']}"
        )
    return found
