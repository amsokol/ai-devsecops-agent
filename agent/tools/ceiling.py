"""What the agent is willing to execute and reach, and how an ecosystem asks for it.

An ecosystem document declares the binaries and hosts its procedures need. A declaration is a
request, not a permission: adding a line to a knowledge document cannot widen what the agent may
run. Anything requested outside the ceiling is refused at startup, so the gap is visible instead of
surfacing as a mysterious failure halfway through a review.

Requirements are read from prose here, which is exactly what the overlay deliberately avoids — the
difference is the failure mode. A reworded overlay line silently changes a verdict; a reworded
`Requirements` line grants nothing, the fact becomes `not-permitted`, and the run says so and
refuses. Loud and fail-closed is acceptable where silent and fail-open is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from agent.errors import ConfigError
from agent.library import Document, Library, load_yaml_mapping

_SECTION = re.compile(r"^##\s+Requirements\s*$")
_ANY_HEADING = re.compile(r"^#{1,6}\s+")
_CODE = re.compile(r"`([^`]+)`")


@dataclass(frozen=True, slots=True)
class Requirements:
    """What one ecosystem document asks for."""

    ecosystem: str
    binaries: frozenset[str]
    hosts: frozenset[str]

    @classmethod
    def of(cls, document: Document) -> Requirements:
        binaries: set[str] = set()
        hosts: set[str] = set()
        for label, tokens in _bullets(document.body()):
            if label == "binaries":
                binaries |= tokens
            elif label == "hosts":
                hosts |= tokens
        return cls(ecosystem=document.id, binaries=frozenset(binaries), hosts=frozenset(hosts))


@dataclass(frozen=True, slots=True)
class Ceiling:
    """The agent's own limit. Products that need more replace the configuration directory."""

    binaries: frozenset[str]
    hosts: frozenset[str]

    @classmethod
    def read(cls, directory: Path) -> Ceiling:
        raw = load_yaml_mapping(directory / "ceiling.yaml")
        return cls(
            binaries=frozenset(str(item) for item in raw.get("binaries") or ()),
            hosts=frozenset(str(item) for item in raw.get("hosts") or ()),
        )

    def allows_binary(self, binary: str) -> bool:
        return binary in self.binaries

    def allows_host(self, host: str) -> bool:
        host = host.lower()
        for allowed in self.hosts:
            allowed = allowed.lower()
            if allowed.startswith("*."):
                if host == allowed[2:] or host.endswith(allowed[1:]):
                    return True
            elif host == allowed:
                return True
        return False


@dataclass(frozen=True, slots=True)
class Grants:
    """What this run may actually use, after every request met the ceiling."""

    binaries: frozenset[str]
    hosts: frozenset[str]

    def allows_binary(self, binary: str) -> bool:
        return binary in self.binaries

    def allows_host(self, host: str) -> bool:
        return host.lower() in {allowed.lower() for allowed in self.hosts}


def grant(*, library: Library, ecosystems: tuple[str, ...], ceiling: Ceiling) -> Grants:
    """Grant what the enabled ecosystems declared, refusing anything above the ceiling."""
    binaries: set[str] = set()
    hosts: set[str] = set()
    refused: list[str] = []
    for ecosystem in ecosystems:
        requirements = Requirements.of(library.get(ecosystem))
        for binary in sorted(requirements.binaries):
            if ceiling.allows_binary(binary):
                binaries.add(binary)
            else:
                refused.append(f"{ecosystem} requires binary {binary!r}")
        for host in sorted(requirements.hosts):
            if ceiling.allows_host(host):
                hosts.add(host)
            else:
                refused.append(f"{ecosystem} requires host {host!r}")
    if refused:
        listing = "; ".join(refused)
        raise ConfigError(
            f"outside the agent's ceiling: {listing}. Widen the ceiling deliberately with "
            "--config-dir, or disable the ecosystem; a knowledge document cannot grant this itself."
        )
    return Grants(binaries=frozenset(binaries), hosts=frozenset(hosts))


def _bullets(body: str) -> list[tuple[str, set[str]]]:
    """Logical bullets of the `Requirements` section, as (label, code-span tokens)."""
    collected: list[tuple[str, set[str]]] = []
    inside = False
    current: str | None = None
    for line in body.splitlines():
        if _SECTION.match(line):
            inside = True
            continue
        if inside and _ANY_HEADING.match(line):
            break
        if not inside:
            continue
        if line.startswith("- "):
            if current is not None:
                collected.append(_labelled(current))
            current = line[2:]
        elif current is not None and line.startswith(" "):
            current += " " + line.strip()
        elif not line.strip():
            continue
    if current is not None:
        collected.append(_labelled(current))
    return [entry for entry in collected if entry[0]]


def _labelled(bullet: str) -> tuple[str, set[str]]:
    label, separator, rest = bullet.partition(":")
    if not separator:
        return "", set()
    return label.strip().lower(), set(_CODE.findall(rest))
