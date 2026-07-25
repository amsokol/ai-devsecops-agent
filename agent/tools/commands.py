"""`run_command`: an allowlisted binary with arguments, and nothing else.

There is no shell — no pipes, no redirection, no chaining. That removes a large attack surface and
makes every call reproducible from the manifest. When a procedure genuinely needs a pipeline, the
right answer is a tool for that need, not a shell.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agent.tools.ceiling import Grants

MAX_OUTPUT_CHARS = 20_000
DEFAULT_TIMEOUT_SECONDS = 120
STOP_GRACE_SECONDS = 5
TRUNCATION_MARKER = "\n… truncated\n"

FALLBACK_PATH = "/usr/local/bin:/usr/bin:/bin"

WHERE_THE_TOOLCHAIN_IS = (
    "RUSTUP_HOME",
    "GOROOT",
    "JAVA_HOME",
    "PYENV_ROOT",
    "NVM_DIR",
    "SDKMAN_DIR",
)
"""Variables that say where an installed toolchain lives, passed through when the agent has them.

These name directories of compilers, not of people: no credential is kept in any of them. Without
them a build tool that keeps its toolchains under a home directory cannot find them — `cargo` says
"no default toolchain is configured" and every Rust verification fails for a reason that has nothing
to do with the change being verified. That is not a safe default; it is a check that can only say
no."""


class NotPermitted(Exception):
    """The binary is outside what this run may execute. Not a reason to try another spelling."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True, slots=True)
class CommandRunner:
    """Runs commands for one task: inside the repository, or in a scratch directory it may write to.

    The scratch directory exists so that a probe which insists on writing files can run without any
    chance of its output reaching a commit — it is discarded with the task.
    """

    grants: Grants
    workdir: Path
    scratch: Path
    tools: Path = Path(".agent/tools")
    """Where a command may download what it needs — the crate registry, the module cache, wheels.
    Shared between runs on purpose: without it every verification starts by fetching the world."""
    timeout: int = DEFAULT_TIMEOUT_SECONDS

    def run(
        self,
        command: tuple[str, ...],
        *,
        in_scratch: bool = False,
        timeout: int | None = None,
    ) -> CommandResult:
        if not command:
            raise ValueError("a command needs at least a binary")
        binary = command[0]
        if not self.grants.allows_binary(binary):
            raise NotPermitted(
                f"{binary!r} is not permitted for this run. Binaries are declared by ecosystem "
                "documents and granted within the agent's ceiling."
            )
        located = shutil.which(binary)
        if located is None:
            raise FileNotFoundError(f"{binary!r} is permitted but not installed")
        cwd = self.scratch if in_scratch else self.workdir
        cwd.mkdir(parents=True, exist_ok=True)
        # Its own session, so stopping the command stops what it spawned. No input at all, so a
        # command waiting on stdin fails instead of holding the run until the budget runs out.
        process = subprocess.Popen(  # noqa: S603 - allowlisted binary, no shell
            [located, *command[1:]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=self._environment(),
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout or self.timeout)
        except subprocess.TimeoutExpired:
            _stop(process)
            return CommandResult(command, None, "", "", timed_out=True)
        return CommandResult(
            command=command,
            exit_code=process.returncode,
            stdout=_truncate(stdout),
            stderr=_truncate(stderr),
            timed_out=False,
        )

    def _environment(self) -> dict[str, str]:
        """Where the toolchain is and where it may write, and nothing about who the agent is.

        Built rather than inherited, because a command may be running code that arrived in the
        change under review: no token, no registry login, no key reaches it, and `HOME` points at a
        directory that dies with the task rather than one holding `.netrc`, `.ssh` and a logged-in
        CLI.

        What it does get is the toolchain the product's own CI would have. The first live fix proved
        the difference: with a home directory of its own and a fixed `PATH`, `cargo` could not find
        a single toolchain, so the verification that decides whether a fix ships failed on every
        Rust repository regardless of the fix. Downloads go to the agent's own cache for the same
        reason the home directory is replaced — a crate registry in somebody's home may hold their
        publishing token, and nothing here needs to read it.
        """
        caches = {
            "CARGO_HOME": self.tools / "cargo",
            "GOPATH": self.tools / "go",
            "GOCACHE": self.tools / "go" / "build",
            "UV_CACHE_DIR": self.tools / "uv",
            "PIP_CACHE_DIR": self.tools / "pip",
            "npm_config_cache": self.tools / "npm",
        }
        for path in caches.values():
            path.mkdir(parents=True, exist_ok=True)
        environment = {
            "PATH": os.environ.get("PATH") or FALLBACK_PATH,
            "HOME": str(self.scratch),
            "LC_ALL": "C",
            "NO_COLOR": "1",
        } | {name: str(path) for name, path in caches.items()}
        for name in WHERE_THE_TOOLCHAIN_IS:
            found = os.environ.get(name)
            if found:
                environment[name] = found
        if "RUSTUP_HOME" not in environment:
            # rustup's own default is `$HOME/.rustup`, and this command's home is a scratch
            # directory that never saw an installer. Naming the real one is what makes `cargo` work.
            installed = Path("~/.rustup").expanduser()
            if installed.is_dir():
                environment["RUSTUP_HOME"] = str(installed)
        return environment


def _stop(process: subprocess.Popen[str]) -> None:
    """End an overrunning command, and give up on it rather than on the run.

    A sandbox can refuse the signal. Abandoning one stray process is a smaller problem than a review
    that dies with an unrelated `PermissionError` and reports nothing about the code.
    """
    for pipe in (process.stdout, process.stderr):
        if pipe is not None:
            with contextlib.suppress(OSError):
                pipe.close()
    for number in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(process.pid), number)
        except OSError:
            return
        try:
            process.wait(timeout=STOP_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            continue


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + TRUNCATION_MARKER
