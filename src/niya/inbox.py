from collections.abc import Sequence
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


def turn_source(items: Sequence[InboundItem]) -> str:
    """A turn with anything the owner said is a chat turn; otherwise it belongs to whoever started it."""
    if any(item.speaker == "owner" for item in items):
        return "owner"
    return min(items, key=lambda item: item.ts).speaker
