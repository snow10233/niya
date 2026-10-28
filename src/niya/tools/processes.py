"""Background processes started by the terminal tool.

The `process` tool's action set follows NousResearch/hermes-agent (MIT)
tools/process_registry.py@222465d84; the registry itself is a small asyncio
rewrite. Processes live in niya's cgroup, so a niya restart ends them; long
running services belong in systemd.
"""

import asyncio
import itertools
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from niya.tools.ansi_strip import strip_ansi
from niya.tools.shell import ShellSession, kill_group

MAX_BUFFER_CHARS = 200_000
FINISHED_TTL_S = 30 * 60


@dataclass
class BackgroundProcess:
    id: str
    command: str
    cwd: str
    started_at: float
    notify: bool
    log_path: Path
    proc: asyncio.subprocess.Process
    buffer: str = ""
    buffer_start: int = 0
    total: int = 0
    read_offset: int = 0
    exit_code: int | None = None
    finished_at: float | None = None
    pump: asyncio.Task | None = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self.exit_code is None

    def append(self, text: str) -> None:
        self.buffer += text
        self.total += len(text)
        overflow = len(self.buffer) - MAX_BUFFER_CHARS
        if overflow > 0:
            self.buffer = self.buffer[overflow:]
            self.buffer_start += overflow

    def unread(self) -> str:
        start = max(self.read_offset, self.buffer_start)
        skipped = start - self.read_offset
        text = self.buffer[start - self.buffer_start :]
        self.read_offset = self.total
        return (f"…（{skipped} 字元已超出緩衝區）…\n" if skipped else "") + text

    def tail_lines(self, lines: int) -> str:
        return "\n".join(self.buffer.splitlines()[-lines:])

    def status(self, now: float) -> str:
        if self.running:
            return f"執行中，已 {int(now - self.started_at)} 秒"
        return f"已結束（exit {self.exit_code}）"


class ProcessRegistry:
    def __init__(self, log_dir: Path, *, now: Callable[[], float], on_exit: Callable[[BackgroundProcess], None] | None = None, max_running: int = 16) -> None:
        self.log_dir = log_dir
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.now = now
        self.on_exit = on_exit
        self.max_running = max_running
        self._procs: dict[str, BackgroundProcess] = {}
        self._ids = itertools.count(1)

    async def start(self, command: str, *, shell: ShellSession, cwd: str | None = None, notify: bool = False) -> BackgroundProcess:
        self._prune()
        if sum(p.running for p in self._procs.values()) >= self.max_running:
            raise RuntimeError(f"背景程序已達上限（{self.max_running} 個），請先結束不需要的程序。")
        workdir = cwd or shell.cwd
        script = "\n".join(
            [
                f"source {shlex.quote(str(shell.snapshot))} >/dev/null 2>&1 || true" if shell.snapshot.exists() else "true",
                f"builtin cd -- {shlex.quote(workdir)} || exit 126",
                f"eval {shlex.quote(command)}",
            ]
        )
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", script, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=shell.env, start_new_session=True,
        )
        pid = f"p{next(self._ids)}"
        bg = BackgroundProcess(id=pid, command=command, cwd=workdir, started_at=self.now(), notify=notify, log_path=self.log_dir / f"{pid}-{proc.pid}.log", proc=proc)
        bg.pump = asyncio.create_task(self._pump(bg))
        self._procs[pid] = bg
        return bg

    async def _pump(self, bg: BackgroundProcess) -> None:
        assert bg.proc.stdout is not None
        with bg.log_path.open("a", encoding="utf-8") as log:
            while chunk := await bg.proc.stdout.read(65536):
                text = strip_ansi(chunk.decode("utf-8", errors="replace"))
                bg.append(text)
                log.write(text)
                log.flush()
        bg.exit_code = await bg.proc.wait()
        bg.finished_at = self.now()
        if bg.notify and self.on_exit:
            self.on_exit(bg)

    def get(self, pid: str) -> BackgroundProcess:
        if pid not in self._procs:
            raise KeyError(f"沒有這個背景程序：{pid}")
        return self._procs[pid]

    def list(self) -> list[BackgroundProcess]:
        self._prune()
        return list(self._procs.values())

    async def wait(self, pid: str, timeout: float) -> bool:
        bg = self.get(pid)
        if bg.pump is None:
            return not bg.running
        try:
            await asyncio.wait_for(asyncio.shield(bg.pump), timeout)
        except TimeoutError:
            return False
        return True

    async def kill(self, pid: str) -> None:
        bg = self.get(pid)
        if bg.running:
            await kill_group(bg.proc)
            if bg.pump:
                try:
                    await asyncio.wait_for(asyncio.shield(bg.pump), 3)
                except TimeoutError:
                    pass

    async def write(self, pid: str, text: str) -> None:
        bg = self.get(pid)
        if not bg.running or bg.proc.stdin is None:
            raise RuntimeError(f"{pid} 已經結束，無法寫入。")
        bg.proc.stdin.write(text.encode())
        await bg.proc.stdin.drain()

    async def shutdown(self) -> None:
        for bg in list(self._procs.values()):
            if bg.running:
                await kill_group(bg.proc, grace=1.0)

    def _prune(self) -> None:
        now = self.now()
        for pid, bg in list(self._procs.items()):
            if bg.finished_at is not None and now - bg.finished_at > FINISHED_TTL_S:
                del self._procs[pid]
