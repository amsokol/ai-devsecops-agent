"""Deterministic GitHub Actions pin census."""

from __future__ import annotations

from pathlib import Path

from agent.tools.actions import list_action_pins, packages


def test_list_action_pins_reads_uses_and_images(tmp_path: Path) -> None:
    workflow = tmp_path / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "ci.yml").write_text(
        """
name: CI
jobs:
  build:
    runs-on: ubuntu-latest
    container:
      image: eclipse-temurin:25-jdk
    steps:
      - uses: actions/checkout@v7
      - uses: ./local-action
      - uses: dtolnay/rust-toolchain@stable
      - uses: actions/checkout@v7
""",
        encoding="utf-8",
    )
    pins = list_action_pins(tmp_path)
    names = packages(pins)
    assert names == frozenset(
        {"actions/checkout", "dtolnay/rust-toolchain", "eclipse-temurin"}
    )
    assert "./local-action" not in names
    by_package = {pin.package: pin for pin in pins}
    assert by_package["actions/checkout"].reference == "v7"
    assert by_package["eclipse-temurin"].reference == "25-jdk"
    assert by_package["eclipse-temurin"].kind == "image"


def test_list_action_pins_reads_composite_actions(tmp_path: Path) -> None:
    action = tmp_path / ".github" / "actions" / "setup"
    action.mkdir(parents=True)
    (action / "action.yml").write_text(
        """
name: setup
runs:
  using: composite
  steps:
    - uses: actions/setup-go@v5
""",
        encoding="utf-8",
    )
    pins = list_action_pins(tmp_path)
    assert packages(pins) == frozenset({"actions/setup-go"})


def test_action_publish_time_reads_release_not_commit() -> None:
    from agent.tools.actions import action_publish_time
    from agent.tools.network import Response

    class FakeHttp:
        def get(self, url: str) -> Response:
            assert "releases/tags/v7.0.1" in url
            return Response(
                url=url,
                status=200,
                headers={},
                body='{"published_at":"2026-07-20T15:10:05Z","created_at":"2026-07-20T15:00:00Z"}',
                truncated=False,
            )

    answer = action_publish_time(FakeHttp(), "actions/checkout", "v7.0.1")  # type: ignore[arg-type]
    assert answer.found is True
    assert answer.published_at == "2026-07-20T15:10:05Z"


def test_action_publish_time_missing_release_is_not_found() -> None:
    import io
    import urllib.error

    from agent.tools.actions import action_publish_time

    class FakeHttp:
        def get(self, url: str) -> object:
            raise urllib.error.HTTPError(url, 404, "Not Found", hdrs={}, fp=io.BytesIO())

    answer = action_publish_time(FakeHttp(), "actions/checkout", "no-such-tag")  # type: ignore[arg-type]
    assert answer.found is False
    assert answer.published_at is None
