"""How full the context is. niya never compresses its context: past the soft line it goes to sleep (for now:
a new session) after the turn; past the hard line it stops calling tools mid-turn and wraps up."""

from collections.abc import Iterable

SOFT = 0.75
HARD = 0.90
CONTEXT_FULL_NOTE = "context 快滿了，先停下工具，直接向主人交代目前的進度和還沒做完的事。"
CONTEXT_FULL_TOOL_TEXT = "錯誤：context 快滿了，這個呼叫沒有執行。請直接向主人交代目前的進度和還沒做完的事。"


def estimate_tokens(text: str) -> int:
    """Deliberately generous: a CJK character is about one token, other text about three characters a token."""
    cjk = sum(1 for ch in text if ord(ch) >= 0x2E80)
    return cjk + (len(text) - cjk + 2) // 3


def estimate_all(texts: Iterable[str | None]) -> int:
    return sum(estimate_tokens(t) for t in texts if t)


def over(tokens: int, limit: int, line: float) -> bool:
    return limit > 0 and tokens >= line * limit


def percent(tokens: int, limit: int) -> int:
    return round(100 * tokens / limit) if limit else 0
