"""`compare_versions`: ordering of two version strings, and the step between them.

A model must never order versions by reasoning: the answer is exact, and a model's answer is neither
reproducible nor auditable. This tool answers only about the strings. Whether a move counts as major
*for policy purposes* — a floating action pin, a raised toolchain floor — is judgement and lives in
the library.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_SEMVER = re.compile(
    r"^v?(?P<major>\d+)(?:\.(?P<minor>\d+))?(?:\.(?P<patch>\d+))?"
    r"(?:[-.](?P<pre>[0-9A-Za-z.-]+?))?(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)
_PEP440 = re.compile(
    r"^v?(?:(?P<epoch>\d+)!)?(?P<release>\d+(?:\.\d+)*)"
    r"(?P<pre>(?:[-_.]?(?:a|b|c|rc|alpha|beta|pre|preview)[-_.]?\d*))?"
    r"(?P<post>(?:[-_.]?(?:post|rev|r)[-_.]?\d*|-\d+))?"
    r"(?P<dev>[-_.]?dev[-_.]?\d*)?(?:\+[0-9A-Za-z.]+)?$",
    re.IGNORECASE,
)

# Ecosystems whose versions are PEP 440 rather than semantic versions.
_PEP440_ECOSYSTEMS = frozenset({"ecosystems/python-uv", "ecosystems/python-pip-compile"})


class Step(StrEnum):
    """The semantic-version relationship between two comparable versions."""

    SAME = "same"
    MAJOR = "major"
    MINOR = "minor"
    PATCH = "patch"
    PRERELEASE = "prerelease"


@dataclass(frozen=True, slots=True)
class Comparison:
    """`order` is -1, 0 or 1 from the left version's point of view; `None` means unordered."""

    order: int | None
    step: Step | None

    @property
    def unordered(self) -> bool:
        return self.order is None


UNORDERED = Comparison(order=None, step=None)


def compare_versions(ecosystem: str, left: str, right: str) -> Comparison:
    """Order two versions within one ecosystem's scheme.

    When the strings are not comparable, the answer is `UNORDERED`. A caller must then treat the
    question as unverified rather than guessing a direction — guessing is how a downgrade gets
    proposed as an upgrade.
    """
    parse = _parse_pep440 if ecosystem in _PEP440_ECOSYSTEMS else _parse_semver
    first, second = parse(left), parse(right)
    if first is None or second is None:
        return UNORDERED
    order = (first > second) - (first < second)
    return Comparison(order=order, step=_step(first, second))


@dataclass(frozen=True, slots=True, order=True)
class _Parsed:
    """Comparable form: release numbers, then a prerelease marker, then a trailing counter.

    `pre_rank` is 0 for a prerelease and 1 for a final release, so 1.0.0-rc1 sorts below 1.0.0.
    """

    release: tuple[int, ...]
    pre_rank: int
    pre: tuple[str | int, ...]
    tail: int


def _parse_semver(raw: str) -> _Parsed | None:
    match = _SEMVER.match(raw.strip())
    if match is None:
        return None
    release = tuple(int(match.group(name) or 0) for name in ("major", "minor", "patch"))
    pre = match.group("pre")
    return _Parsed(
        release=release,
        pre_rank=0 if pre else 1,
        pre=_identifiers(pre) if pre else (),
        tail=0,
    )


def _parse_pep440(raw: str) -> _Parsed | None:
    match = _PEP440.match(raw.strip())
    if match is None:
        return None
    release = tuple(int(part) for part in match.group("release").split("."))
    while len(release) < 3:
        release += (0,)
    pre, dev = match.group("pre"), match.group("dev")
    post = match.group("post")
    return _Parsed(
        release=release,
        pre_rank=0 if (pre or dev) else 1,
        pre=_identifiers(pre or dev or ""),
        tail=_trailing_number(post),
    )


def _identifiers(raw: str) -> tuple[str | int, ...]:
    parts: list[str | int] = []
    for part in re.split(r"[.\-_]", raw.strip(".-_")):
        if not part:
            continue
        parts.append(int(part) if part.isdigit() else part)
    return tuple(parts)


def _trailing_number(raw: str | None) -> int:
    if not raw:
        return 0
    digits = re.findall(r"\d+", raw)
    return int(digits[-1]) if digits else 1


def _step(first: _Parsed, second: _Parsed) -> Step:
    if first.release == second.release:
        if (
            first.pre_rank == second.pre_rank
            and first.pre == second.pre
            and first.tail == second.tail
        ):
            return Step.SAME
        return Step.PRERELEASE
    for position, name in enumerate((Step.MAJOR, Step.MINOR, Step.PATCH)):
        if first.release[position] != second.release[position]:
            return name
    return Step.PATCH
