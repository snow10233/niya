import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...


class RealClock:
    def now(self) -> float:
        return time.time()


class FakeClock:
    def __init__(self, start: float) -> None:
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def set(self, ts: float) -> None:
        self._now = float(ts)
