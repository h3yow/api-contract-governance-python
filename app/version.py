"""语义版本解析与比较（主.次.补丁，均为非负整数）。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import ContractError, ErrorCode

_VERSION_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"
)


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    def as_tuple(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)


def parse_version(text: str) -> Version:
    """解析 X.Y.Z；非法格式抛 INVALID_CONTRACT，原因可区分。"""
    if not isinstance(text, str):
        raise ContractError(
            ErrorCode.INVALID_CONTRACT,
            "版本号必须是字符串",
            {"field": "version", "reason": "not_a_string"},
        )
    match = _VERSION_RE.match(text)
    if match is None:
        raise ContractError(
            ErrorCode.INVALID_CONTRACT,
            "版本号必须符合主.次.补丁（均为非负整数）格式",
            {"field": "version", "value": text},
        )
    return Version(*(int(part) for part in match.groups()))
