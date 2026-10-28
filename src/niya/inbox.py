from dataclasses import dataclass, field
from typing import Any

SPEAKERS = ("owner", "alarm", "system", "process")


@dataclass(frozen=True)
class InboundItem:
    speaker: str
    content: str
    ts: float
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.speaker not in SPEAKERS:
            raise ValueError(f"未知的說話者：{self.speaker}")
