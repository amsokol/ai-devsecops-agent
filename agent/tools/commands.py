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
        """A minimal environment: the run's secrets are not a command's business."""
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.scratch),
            "LC_ALL": "C",
            "NO_COLOR": "1",
        }


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
