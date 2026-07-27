"""Deterministic census of pins in GitHub Actions workflows and composite actions.

The model must not invent which `uses:` and `image:` lines exist. Two live runs over one tree
examined different subsets and closed or raised issues for the wrong reason. This module walks the
files the ecosystem declares and returns every third-party pin once, by package name.

`action_publish_time` answers when a concrete action tag was published from the GitHub Release API
only — never from a committer date, which predates the release and falsely clears quarantine.
"""

from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agent.tools.network import HttpClient

WORKFLOWS = Path(".github/workflows")
ACTIONS = Path(".github/actions")


@dataclass(frozen=True, slots=True)
class ActionPin:
    """One third-party action or container image pin found in the tree."""

    package: str
    """Stable identity for coverage: `owner/name` for actions, image name without tag for images."""
    reference: str
    """The `@ref` or image tag/digest as written."""
    path: str
    """Repository-relative file that declared it."""
    kind: str
    """`action` or `image`."""

    def as_json(self) -> dict[str, str]:
        return {
            "package": self.package,
            "reference": self.reference,
            "path": self.path,
            "kind": self.kind,
        }


def list_action_pins(root: Path) -> tuple[ActionPin, ...]:
    """Every third-party `uses:` and container `image:` under `.github/`, ordered and unique.

    Local `uses: ./…` are skipped: they have no registry version. The same package on two jobs is
    one pin for coverage purposes — the package name is what findings and evidence subjects use.
    """
    found: dict[str, ActionPin] = {}
    for path in _yaml_files(root):
        relative = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError:
            continue
        if not isinstance(document, dict):
            continue
        for pin in _pins_in(document, path=relative):
            found.setdefault(pin.package, pin)
    return tuple(sorted(found.values(), key=lambda item: (item.kind, item.package, item.path)))


def packages(pins: tuple[ActionPin, ...]) -> frozenset[str]:
    return frozenset(pin.package for pin in pins)


def _yaml_files(root: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    workflows = root / WORKFLOWS
    if workflows.is_dir():
        paths += sorted(
            path
            for path in workflows.rglob("*")
            if path.is_file() and path.suffix.lower() in {".yml", ".yaml"}
        )
    actions = root / ACTIONS
    if actions.is_dir():
        paths += sorted(path for path in actions.rglob("action.yml") if path.is_file())
        paths += sorted(path for path in actions.rglob("action.yaml") if path.is_file())
    return tuple(paths)


def _pins_in(node: Any, *, path: str) -> list[ActionPin]:
    pins: list[ActionPin] = []
    if isinstance(node, dict):
        uses = node.get("uses")
        if isinstance(uses, str):
            pin = _action_pin(uses, path=path)
            if pin is not None:
                pins.append(pin)
        image = node.get("image")
        if isinstance(image, str):
            pin = _image_pin(image, path=path)
            if pin is not None:
                pins.append(pin)
        container = node.get("container")
        if isinstance(container, str):
            pin = _image_pin(container, path=path)
            if pin is not None:
                pins.append(pin)
        for value in node.values():
            pins += _pins_in(value, path=path)
    elif isinstance(node, list):
        for item in node:
            pins += _pins_in(item, path=path)
    return pins


def _action_pin(uses: str, *, path: str) -> ActionPin | None:
    value = uses.strip()
    if not value or value.startswith(("./", ".\\")):
        return None
    if value.startswith("docker://"):
        return _image_pin(value.removeprefix("docker://"), path=path)
    if "@" not in value:
        return None
    package, _, reference = value.partition("@")
    package, reference = package.strip(), reference.strip()
    if not package or not reference or "/" not in package:
        return None
    return ActionPin(package=package, reference=reference, path=path, kind="action")


def _image_pin(image: str, *, path: str) -> ActionPin | None:
    value = image.strip()
    if not value:
        return None
    name, reference = _split_image(value)
    name = name.strip()
    if not name:
        return None
    return ActionPin(
        package=name,
        reference=reference.strip() or "latest",
        path=path,
        kind="image",
    )


def _split_image(image: str) -> tuple[str, str]:
    """Split `name:tag` or `name@digest`, leaving registry host:port alone."""
    if "@" in image:
        name, _, digest = image.partition("@")
        return name, digest
    if ":" not in image:
        return image, "latest"
    # One colon: either name:tag or host:port (no tag). host:port has no slash after the colon.
    if image.count(":") == 1:
        left, _, right = image.partition(":")
        if "/" in right:
            return image, "latest"
        return left, right
    # host:port/name:tag — tag is after the last colon when the left side still has a slash.
    before, _, tag = image.rpartition(":")
    if "/" in before and "/" not in tag:
        return before, tag
    return image, "latest"


@dataclass(frozen=True, slots=True)
class ActionReleaseTime:
    """Publication time of a GitHub Actions tag, from the Release API only."""

    package: str
    tag: str
    published_at: str | None
    """ISO timestamp when a Release exists; `None` when the tag has no Release."""
    url: str
    found: bool

    def as_json(self) -> dict[str, object]:
        return {
            "package": self.package,
            "tag": self.tag,
            "published_at": self.published_at,
            "url": self.url,
            "found": self.found,
        }


def action_publish_time(http: HttpClient, package: str, tag: str) -> ActionReleaseTime:
    """GitHub Release `published_at` (else `created_at`) for `owner/name@tag`.

    Committer dates are not consulted: they predate the Release and falsely clear quarantine (live
    miss on `actions/checkout@v7.0.1`: commit 2026-07-17 vs release 2026-07-20). When there is no
    Release for the tag, `found` is false — treat as unverified, not as a commit clock.
    """
    name = package.strip()
    version = tag.strip().removeprefix("refs/tags/")
    if not name or "/" not in name or not version:
        raise ValueError("package must be owner/name and tag must be non-empty")
    owner, _, repo = name.partition("/")
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{version}"
    try:
        response = http.get(url)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return ActionReleaseTime(
                package=name, tag=version, published_at=None, url=url, found=False
            )
        raise
    try:
        payload = json.loads(response.body)
    except ValueError as error:
        raise ValueError(f"{url} did not return JSON") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{url} did not return a release object")
    published = payload.get("published_at") or payload.get("created_at")
    if not isinstance(published, str) or not published.strip():
        return ActionReleaseTime(package=name, tag=version, published_at=None, url=url, found=False)
    return ActionReleaseTime(
        package=name,
        tag=version,
        published_at=published.strip(),
        url=url,
        found=True,
    )
