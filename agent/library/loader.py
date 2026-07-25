"""Loading the knowledge library: identity, index, slices, digest.

The agent reads `INDEX.md` in full and loads bodies on demand. Nothing here interprets knowledge;
this module only decides which documents a task is given.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any, Self

import yaml

from agent.errors import ConfigError

SUPPORTED_CONTRACT_VERSIONS = frozenset({2})
"""Which result contracts this agent can read. A set rather than a number so one agent can serve two
library versions through a migration. Nothing is pinned to `1` any more, and accepting a contract
this validator does not enforce would be worse than refusing it."""

# Kinds a task's knowledge slice may grow into by following links. See `Library.closure`.
FOLLOWED_KINDS = frozenset({"policy", "scm"})

_KINDS = frozenset({"playbook", "capability", "policy", "ecosystem", "scm", "overlay"})
_ROW = re.compile(r"^\|\s*`(?P<id>[^`]+)`\s*\|(?P<rest>.*)\|\s*$")
_LINK = re.compile(r"\]\((?P<target>[^)#]+\.md)(?:#[^)]*)?\)")
_HEADER_FIELD = re.compile(r"^(?P<key>[a-z_]+):\s*(?P<value>.*)$")
_EMPTY_CELL = "—"


def _parse_version(raw: str, *, where: str) -> tuple[int, ...]:
    parts = raw.strip().removeprefix("v").split("+")[0].split("-")[0].split(".")
    try:
        return tuple(int(part) for part in parts)
    except ValueError:
        raise ConfigError(f"{where}: {raw!r} is not a version of the form X.Y.Z") from None


@dataclass(frozen=True, slots=True)
class Identity:
    """What the library says about itself, from `library.yaml`."""

    version: str
    contract_version: int
    min_agent_version: str

    @classmethod
    def read(cls, root: Path) -> Self:
        path = root / "library.yaml"
        if not path.is_file():
            raise ConfigError(
                f"{path} is missing: the directory does not look like a knowledge library"
            )
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: expected a mapping at the top level")
        missing = sorted({"version", "contract_version", "min_agent_version"} - raw.keys())
        if missing:
            raise ConfigError(f"{path}: missing {', '.join(missing)}")
        contract = raw["contract_version"]
        if not isinstance(contract, int):
            raise ConfigError(f"{path}: contract_version must be an integer, got {contract!r}")
        return cls(
            version=str(raw["version"]),
            contract_version=contract,
            min_agent_version=str(raw["min_agent_version"]),
        )

    def check_compatible(self, agent_version: str) -> None:
        """Refuse to start on a mismatch, rather than behaving oddly mid-review."""
        if self.contract_version not in SUPPORTED_CONTRACT_VERSIONS:
            supported = ", ".join(str(v) for v in sorted(SUPPORTED_CONTRACT_VERSIONS))
            raise ConfigError(
                f"library contract version {self.contract_version} is not supported by this agent "
                f"(supported: {supported})"
            )
        required = _parse_version(self.min_agent_version, where="library.yaml min_agent_version")
        running = _parse_version(agent_version, where="agent version")
        if running < required:
            raise ConfigError(
                f"library {self.version} requires agent >= {self.min_agent_version}, "
                f"running {agent_version}"
            )


@dataclass(frozen=True, slots=True)
class Document:
    """One knowledge document, as listed in the index. The body is read on demand."""

    id: str
    kind: str
    summary: str
    applies_to: tuple[str, ...]
    path: Path

    def matches_path(self, changed: str) -> bool:
        """Whether a changed repository path is covered by this document's `applies_to`."""
        candidate = changed.strip("/")
        for entry in self.applies_to:
            marker = entry.strip("/")
            if candidate == marker or candidate.endswith("/" + marker):
                return True
            if candidate.startswith(marker + "/"):
                return True
        return False

    def body(self) -> str:
        """The prose, without the header. This is what a subagent is given."""
        text = self.path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            return text
        end = text.find("\n---\n", 4)
        if end == -1:
            raise ConfigError(f"{self.path}: header is not terminated")
        header = _parse_header(text[4:end])
        if header.get("id") != self.id:
            raise ConfigError(
                f"{self.path}: header id {header.get('id')!r} does not match index id {self.id!r}"
            )
        return text[end + len("\n---\n") :].lstrip("\n")


def _parse_header(block: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in block.splitlines():
        if not line.strip():
            continue
        match = _HEADER_FIELD.match(line)
        if match:
            fields[match.group("key")] = match.group("value").strip()
    return fields


def _parse_applies_to(cell: str) -> tuple[str, ...]:
    if cell.strip() in {"", _EMPTY_CELL}:
        return ()
    return tuple(item.strip().strip("`") for item in cell.split(",") if item.strip())


class Library:
    """A loaded library: its identity, its index, and the bodies it can hand out."""

    def __init__(self, root: Path, identity: Identity, documents: dict[str, Document]) -> None:
        self.root = root
        self.identity = identity
        self._documents = documents

    @classmethod
    def load(cls, root: Path, *, agent_version: str) -> Self:
        root = root.resolve()
        if not root.is_dir():
            raise ConfigError(f"library path {root} is not a directory")
        identity = Identity.read(root)
        identity.check_compatible(agent_version)
        documents = _read_index(root)
        return cls(root, identity, documents)

    def __contains__(self, doc_id: str) -> bool:
        return doc_id in self._documents

    def __len__(self) -> int:
        return len(self._documents)

    def get(self, doc_id: str) -> Document:
        try:
            return self._documents[doc_id]
        except KeyError:
            raise ConfigError(f"{doc_id!r} is not in the library index") from None

    def by_kind(self, kind: str) -> tuple[Document, ...]:
        return tuple(doc for doc in self._documents.values() if doc.kind == kind)

    def ecosystems_for_paths(
        self, paths: tuple[str, ...], enabled: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Which enabled ecosystems a set of changed paths touches, in index order."""
        matched = [
            doc.id
            for doc in self.by_kind("ecosystem")
            if doc.id in enabled and any(doc.matches_path(path) for path in paths)
        ]
        return tuple(matched)

    def known_paths(self, enabled: tuple[str, ...]) -> tuple[str, ...]:
        """Every `applies_to` marker of the enabled ecosystems, for classifying changed paths."""
        markers: list[str] = []
        for doc in self.by_kind("ecosystem"):
            if doc.id in enabled:
                markers.extend(doc.applies_to)
        return tuple(dict.fromkeys(markers))

    def links(self, doc_id: str) -> tuple[str, ...]:
        """Documents this one points at, by index id. Links to non-indexed files are ignored."""
        doc = self.get(doc_id)
        targets: list[str] = []
        for match in _LINK.finditer(doc.body()):
            target = (doc.path.parent / match.group("target")).resolve()
            try:
                relative = target.relative_to(self.root)
            except ValueError:
                continue
            linked = relative.as_posix().removesuffix(".md")
            if linked in self._documents and linked not in targets:
                targets.append(linked)
        return tuple(targets)

    def closure(self, roots: tuple[str, ...]) -> tuple[str, ...]:
        """The knowledge a task is given: its roots plus the rules those roots invoke.

        Which documents travel along is decided by the library's own links, so a policy referenced
        by a capability arrives automatically and the agent needs no list of its own.

        Only `FOLLOWED_KINDS` are pulled in. The other kinds are *selected* — a playbook by the
        trigger, an ecosystem by the overlay and the diff — and following a link into them would
        drag most of the library into every task: a maintenance playbook mentions the review
        playbook, an ecosystem document mentions its sibling, and prose cross-references are written
        for a human reading the library, not as a dependency graph.
        """
        order: list[str] = []
        queue = list(roots)
        while queue:
            current = queue.pop(0)
            if current in order or current not in self._documents:
                continue
            order.append(current)
            queue.extend(
                linked
                for linked in self.links(current)
                if self._documents[linked].kind in FOLLOWED_KINDS
            )
        return tuple(order)

    @cached_property
    def digest(self) -> str:
        """Content digest of the knowledge itself: identity, index, and every indexed document.

        Deliberately not a digest of the directory. A library is also a repository, with a README, a
        licence and its own tooling, none of which reaches a run; hashing it would give a checkout
        and an unpacked artefact different digests for identical knowledge, and the pin would then
        work only in CI. Document ids are hashed with the bodies, so a rename is a change.
        """
        entries = [
            _entry("library.yaml", (self.root / "library.yaml").read_bytes()),
            _entry("INDEX.md", (self.root / "INDEX.md").read_bytes()),
        ]
        entries += [
            _entry(document.id, document.path.read_bytes())
            for document in sorted(self._documents.values(), key=lambda item: item.id)
        ]
        return "sha256:" + hashlib.sha256("".join(entries).encode("utf-8")).hexdigest()

    def check_pinned(self, *, version: str | None, digest: str | None) -> None:
        if version is not None and version != self.identity.version:
            raise ConfigError(
                f"library is pinned to {version} but the loaded library is {self.identity.version}"
            )
        if digest is not None and digest != self.digest:
            raise ConfigError(
                f"library digest mismatch: pinned {digest}, loaded {self.digest}. "
                "The artefact was modified or rebuilt; refusing to run on unverified knowledge."
            )


def _entry(name: str, data: bytes) -> str:
    return f"{name}\0{hashlib.sha256(data).hexdigest()}\n"


def _read_index(root: Path) -> dict[str, Document]:
    index = root / "INDEX.md"
    if not index.is_file():
        raise ConfigError(f"{index} is missing: the library was not built with an index")
    documents: dict[str, Document] = {}
    for number, line in enumerate(index.read_text(encoding="utf-8").splitlines(), start=1):
        match = _ROW.match(line)
        if match is None:
            continue
        cells = [cell.strip() for cell in match.group("rest").split("|")]
        if len(cells) != 3:
            raise ConfigError(f"{index}:{number}: expected four columns, got {len(cells) + 1}")
        doc_id, (kind, summary, applies_to) = match.group("id"), cells
        if kind not in _KINDS:
            raise ConfigError(f"{index}:{number}: unknown kind {kind!r}")
        if doc_id in documents:
            raise ConfigError(f"{index}:{number}: duplicate id {doc_id!r}")
        path = root / f"{doc_id}.md"
        if not path.is_file():
            raise ConfigError(f"{index}:{number}: {doc_id!r} is indexed but {path} does not exist")
        documents[doc_id] = Document(
            id=doc_id,
            kind=kind,
            summary=summary,
            applies_to=_parse_applies_to(applies_to),
            path=path,
        )
    if not documents:
        raise ConfigError(f"{index}: no documents found; the index table could not be read")
    return documents


def load_yaml_mapping(path: Path) -> dict[str, Any]:
    """Read a YAML file that must contain a mapping, with errors naming the file."""
    if not path.is_file():
        raise ConfigError(f"{path} is missing")
    return parse_yaml_mapping(path.read_text(encoding="utf-8"), named=str(path))


def parse_yaml_mapping(text: str, *, named: str) -> dict[str, Any]:
    """The same, for YAML that came from somewhere other than a file — a committed tree, say.

    `named` is what the error messages call it, because "expected a mapping" without a location is a
    message that sends somebody looking through every configuration file they have.
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ConfigError(f"{named}: {error}") from None
    if raw is None:
        raise ConfigError(f"{named} is empty")
    if not isinstance(raw, dict):
        raise ConfigError(f"{named}: expected a mapping at the top level, got {type(raw).__name__}")
    return raw
