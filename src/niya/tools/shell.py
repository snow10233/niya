"""Run shell commands with state that persists across calls.

Each command runs in a fresh `bash -c`; exported variables, functions and
aliases are carried over through a snapshot file, and the working directory
through a marker printed after the command. The approach (and the reason for
not waiting on grandchildren that keep stdout open) follows
NousResearch/hermes-agent (MIT) tools/environments/base.py@222465d84,
rewritten for asyncio.
"""

import asyncio
import os
import secrets
import shlex
import signal
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from niya.tools.ansi_strip import strip_ansi

MAX_CAPTURE_CHARS = 200_000
DRAIN_GRACE_S = 0.2
SCRUBBED_ENV = ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", "CONDA_PREFIX", "UV_PROJECT_ENVIRONMENT", "NIYA_CONFIG")
AGENT_ENV = {"NIYA_AGENT": "1", "PAGER": "cat", "GIT_PAGER": "cat", "TERM": "dumb", "GIT_TERMINAL_PROMPT": "0", "DEBIAN_FRONTEND": "noninteractive"}


@dataclass(frozen=True)
class ShellResult:
    output: str
    exit_code: int
    timed_out: bool
    cwd: str


class BoundedOutput:
    """Keeps the first and last half of a stream once it grows past the limit."""

    def __init__(self, limit: int = MAX_CAPTURE_CHARS) -> None:
        self.limit = limit
        self.head: list[str] = []
        self.head_len = 0
        self.tail = ""
        self.total = 0

    def add(self, text: str) -> None:
        self.total += len(text)
        room = self.limit // 2 - self.head_len
        if room > 0:
            self.head.append(text[:room])
            self.head_len += len(text[:room])
            text = text[room:]
        if text:
            self.tail = (self.tail + text)[-(self.limit // 2) :]

    def text(self) -> str:
        head = "".join(self.head)
        omitted = self.total - len(head) - len(self.tail)
        if omitted > 0:
            return f"{head}\n…（輸出過長，中間省略 {omitted} 字元）…\n{self.tail}"
        return head + self.tail


def clean_env(base: Mapping[str, str], secret_names: Iterable[str] = ()) -> dict[str, str]:
    blocked = set(secret_names) | set(SCRUBBED_ENV)
    env = {k: v for k, v in base.items() if k not in blocked}
    if venv := base.get("VIRTUAL_ENV"):
        # niya's own virtualenv stays out of the shell: `python3` and `pip` there are the system's.
        own = str(Path(venv) / "bin")
        env["PATH"] = os.pathsep.join(part for part in env.get("PATH", "").split(os.pathsep) if part and part != own)
    env.update(AGENT_ENV)
    return env


async def kill_group(proc: asyncio.subprocess.Process, grace: float = 2.0) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), grace)
            return
        except TimeoutError:
            continue


async def _pump(stream: asyncio.StreamReader, sink: BoundedOutput) -> None:
    while chunk := await stream.read(65536):
        sink.add(chunk.decode("utf-8", errors="replace"))


async def collect_output(proc: asyncio.subprocess.Process, sink: BoundedOutput) -> None:
    """Read stdout until bash exits, then drain briefly.

    Background children (`sleep 60 &`) inherit stdout and keep the pipe open,
    and asyncio's `Process.wait()` only returns once every pipe is closed, so
    watch `returncode` (set as soon as bash exits) instead of waiting for EOF.
    """
    assert proc.stdout is not None
    pump = asyncio.ensure_future(_pump(proc.stdout, sink))
    try:
        while proc.returncode is None and not pump.done():
            await asyncio.wait({pump}, timeout=0.05)
        if pump.done():
            pump.result()
            await proc.wait()
        else:
            await asyncio.wait({pump}, timeout=DRAIN_GRACE_S)
            if not pump.done():
                # A background child still holds stdout. bash has exited, so closing our end
                # only releases the pipe (the child gets SIGPIPE if it writes to it again).
                transport = getattr(proc, "_transport", None)
                if transport is not None:
                    transport.close()
    finally:
        if not pump.done():
            pump.cancel()


class ShellSession:
    def __init__(self, state_dir: Path, *, cwd: str | None = None, env: Mapping[str, str] | None = None, secret_names: Iterable[str] = ()) -> None:
        self.state_dir = state_dir
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        token = secrets.token_hex(4)
        self.snapshot = state_dir / f"shell-{token}.sh"
        self.marker = f"__NIYA_CWD_{token}__"
        self.cwd = cwd or os.path.expanduser("~")
        self.secret_names = tuple(secret_names)
        self.env = clean_env(env if env is not None else os.environ, self.secret_names)
        self._ready = False

    def _dump_snapshot(self) -> str:
        quoted = shlex.quote(str(self.snapshot))
        unset = " ".join(shlex.quote(n) for n in self.secret_names)
        return (
            f"__niya_tmp=$(mktemp {quoted}.XXXXXX) && {{ "
            f"( unset {unset} 2>/dev/null; export -p ) > \"$__niya_tmp\"; "
            "__niya_fns=$(declare -F | awk '{print $3}' | grep -vE '^_') || true; "
            "[ -n \"$__niya_fns\" ] && declare -f $__niya_fns >> \"$__niya_tmp\"; "
            "alias -p >> \"$__niya_tmp\"; "
            "echo 'shopt -s expand_aliases; set +e; set +u' >> \"$__niya_tmp\"; "
            f"mv -f \"$__niya_tmp\" {quoted}; }} 2>/dev/null || rm -f \"$__niya_tmp\" 2>/dev/null"
        )

    def wrap(self, command: str, cwd: str) -> str:
        parts = []
        if self._ready:
            parts.append(f"source {shlex.quote(str(self.snapshot))} >/dev/null 2>&1 || true")
        parts += [
            f"builtin cd -- {shlex.quote(cwd)} || exit 126",
            f"eval {shlex.quote(command)}",
            "__niya_ec=$?",
            "umask 077",
            self._dump_snapshot(),
            f"printf '\\n{self.marker}%s{self.marker}\\n' \"$(pwd -P)\"",
            "exit $__niya_ec",
        ]
        return "\n".join(parts)

    async def _bootstrap(self) -> None:
        """Capture the login environment (PATH from /etc/profile and friends) once."""
        script = f"umask 077\n{self._dump_snapshot()}\n"
        proc = await asyncio.create_subprocess_exec(
            "bash", "-l", "-c", script, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, env=self.env, start_new_session=True,
        )
        try:
            await asyncio.wait_for(proc.wait(), 15)
        except TimeoutError:
            await kill_group(proc)
        self._ready = self.snapshot.exists()

    async def run(self, command: str, *, timeout: float, cwd: str | None = None, stdin: str | None = None) -> ShellResult:
        if not self._ready:
            await self._bootstrap()
        workdir = cwd or self.cwd
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", self.wrap(command, workdir),
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=self.env, start_new_session=True,
        )
        if stdin is not None and proc.stdin:
            proc.stdin.write(stdin.encode())
            await proc.stdin.drain()
            proc.stdin.close()
        sink = BoundedOutput()
        timed_out = False
        try:
            await asyncio.wait_for(collect_output(proc, sink), timeout)
        except TimeoutError:
            timed_out = True
            await kill_group(proc)
        except asyncio.CancelledError:
            await kill_group(proc)
            raise
        output = strip_ansi(sink.text())
        output, new_cwd = self._extract_cwd(output)
        if new_cwd and cwd is None:
            self.cwd = new_cwd
        code = 124 if timed_out else (proc.returncode if proc.returncode is not None else -1)
        return ShellResult(output=output, exit_code=code, timed_out=timed_out, cwd=new_cwd or workdir)

    def _extract_cwd(self, output: str) -> tuple[str, str | None]:
        last = output.rfind(self.marker)
        if last == -1:
            return output, None
        first = output.rfind(self.marker, max(0, last - 4096), last)
        if first == -1:
            return output, None
        path = output[first + len(self.marker) : last].strip()
        line_start = output.rfind("\n", 0, first)
        line_start = first if line_start == -1 else line_start
        line_end = output.find("\n", last + len(self.marker))
        line_end = len(output) if line_end == -1 else line_end + 1
        return output[:line_start] + output[line_end:], path or None

    def close(self) -> None:
        self.snapshot.unlink(missing_ok=True)
