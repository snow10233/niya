import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from niya.core.media import data_url

WEEKDAYS = "一二三四五六日"
SPEAKER_LABELS = {"owner": "user", "niya": "niya", "alarm": "alarm", "system": "system", "process": "process"}
_ECHOED_PREFIX = re.compile(r"^\s*\[[^\]\n]{8,40}\]\s*niya\s*[:：]\s*")


class RenderableMessage(Protocol):
    id: int
    turn_id: str | None
    role: str
    speaker: str | None
    content: str | None
    api_content: str | None
    tool_calls: list[dict[str, Any]] | None
    tool_call_id: str | None
    extra: dict[str, Any] | None
    ts: float


def format_ts(ts: float, tz: ZoneInfo) -> str:
    dt = datetime.fromtimestamp(ts, tz)
    return f"{dt:%Y-%m-%d} ({WEEKDAYS[dt.weekday()]}) {dt:%H:%M}"


def render_line(ts: float, speaker: str, content: str, tz: ZoneInfo) -> str:
    return f"[{format_ts(ts, tz)}] {SPEAKER_LABELS[speaker]}: {content}"


def strip_echoed_prefix(text: str) -> str:
    """Models that see timestamped history sometimes start their reply with one; drop it."""
    return _ECHOED_PREFIX.sub("", text, count=1)


def images_of(message: RenderableMessage) -> list[str]:
    return list((message.extra or {}).get("images") or ())


def with_images(text: str, paths: Sequence[str], load_image: Callable[[str], str | None]) -> str | list[dict[str, Any]]:
    """Plain text, or text plus image parts when there are pictures to look at."""
    if not paths:
        return text
    urls = [(path, load_image(path)) for path in paths]
    gone = [path for path, url in urls if url is None]
    if gone:
        text += "\n" + "\n".join(f"（圖片已經不在了：{path}）" for path in gone)
    return [{"type": "text", "text": text}, *({"type": "image_url", "image_url": {"url": url}} for _, url in urls if url)]


def render_history(
    messages: Sequence[RenderableMessage],
    tz: ZoneInfo,
    recall_blocks: Mapping[str, str] | None = None,
    load_image: Callable[[str], str | None] = data_url,
) -> list[dict[str, Any]]:
    """Turn stored rows into API messages.

    Output depends only on stored fields (and the image files they point to), so re-rendering the same
    history is byte-identical and appending rows never changes earlier messages; that is what keeps the
    provider prefix cache warm.
    """
    recall_blocks = recall_blocks or {}
    out: list[dict[str, Any]] = []
    seen_turns: set[str | None] = set()
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.role == "user":
            j = i + 1
            while j < len(messages) and messages[j].role == "user" and messages[j].turn_id == msg.turn_id:
                j += 1
            group = sorted(messages[i:j], key=lambda m: (m.ts, m.id))
            lines = [render_line(m.ts, m.speaker or "owner", m.content or "", tz) for m in group]
            block = None if msg.turn_id in seen_turns else recall_blocks.get(msg.turn_id or "")
            seen_turns.add(msg.turn_id)
            text = "\n".join(([block] if block else []) + lines)
            out.append({"role": "user", "content": with_images(text, [p for m in group for p in images_of(m)], load_image)})
            i = j
            continue
        if msg.role == "assistant":
            if msg.content or msg.tool_calls:
                api: dict[str, Any] = {"role": "assistant", "content": render_line(msg.ts, "niya", msg.content, tz) if msg.content else None}
                if msg.tool_calls:
                    api["tool_calls"] = msg.tool_calls
                if msg.extra and msg.extra.get("reasoning_details"):
                    api["reasoning_details"] = msg.extra["reasoning_details"]
                out.append(api)
        elif msg.role == "tool":
            text = msg.api_content if msg.api_content is not None else msg.content or ""
            out.append({"role": "tool", "tool_call_id": msg.tool_call_id, "content": with_images(text, images_of(msg), load_image)})
        i += 1
    return out

