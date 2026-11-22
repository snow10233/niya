"""Context compaction: a long session goes on from a handoff niya writes herself, instead of an early sleep.

Only what the model sees changes. Every message stays in the database, sleep still consolidates all of
them, and deep_recall finds the ones that dropped out of the context.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING

from niya.agent.budget import estimate_tokens
from niya.agent.render import format_ts, images_of, render_line

if TYPE_CHECKING:
    from niya.agent.context import Conversation, StoredMessage

# Asked inside the full context, so the summary is written by whoever saw every tool result.
HANDOFF_REQUEST = (
    "（系統）context 快滿了，前面的對話要換成一份交接摘要。之後你只看得到這份摘要和最近的對話原文，工具的結果都會不見。"
    "請寫給接下來的自己看：\n"
    "- 正在做的事、做到哪一步、下一步要做什麼\n"
    "- 主人交代的事，哪些做完了、哪些還沒\n"
    "- 已經做出的決定和結論，以及理由\n"
    "- 之後會用到的細節：路徑、指令、網址、數字、設定值、錯誤訊息，照原樣抄下來\n"
    "- 主人這段時間的狀況或心情，如果跟接下來有關\n"
    "只寫摘要本身，不要呼叫工具，也不要對主人說話。已經用不到的細節可以省略。"
)
CONTINUE_NOTE = "context 整理好了，照交接摘要接著做剛才沒做完的事。"
# An owner request that keeps filling the context stops after this many automatic continuations.
MAX_CONTINUATIONS = 3
STOPPED_NOTE = f"（已經自動接著做了 {MAX_CONTINUATIONS} 次，context 還是一直滿，先停下來。要繼續的話跟 niya 說一聲）"
# The recent dialogue kept word for word, as a share of the context limit.
RECENT_SHARE = 0.25
RECENT_MESSAGE_CHARS = 3000


class CompactionError(Exception):
    pass


def recent_dialogue(messages: Sequence["StoredMessage"], conv: "Conversation", budget: int) -> str:
    """What was said lately, newest first until the budget runs out; tool calls and results are the summary's job."""
    lines: list[str] = []
    used = 0
    for m in reversed(messages):
        if m.role == "assistant" and m.content:
            speaker = "niya"
        elif m.role == "user" and m.speaker != "system":
            speaker = m.speaker or "owner"
        else:
            continue
        text = m.content or ""
        if len(text) > RECENT_MESSAGE_CHARS:
            text = text[:RECENT_MESSAGE_CHARS] + "…（後面省略）"
        if pictures := len(images_of(m)):
            text += f"（附了 {pictures} 張圖）"
        line = render_line(m.ts, speaker, text, conv.tz)
        cost = estimate_tokens(line)
        if lines and used + cost > budget:
            break
        lines.append(line)
        used += cost
    return "\n".join(reversed(lines))


def handoff_text(conv: "Conversation", summary: str, *, now: float, context_limit: int) -> str:
    messages = conv.messages
    start = format_ts(messages[0].ts, conv.tz) if messages else format_ts(now, conv.tz)
    recent = recent_dialogue(messages, conv, int(context_limit * RECENT_SHARE)) or "（沒有）"
    return (
        "<context_handoff>\n"
        f"這個 session 從 {start} 到現在的對話太長，前面的部分換成了下面這份交接摘要，是你自己在 {format_ts(now, conv.tz)} 寫的。"
        "原始對話都還在，睡覺時會照常整理；需要細節時用 deep_recall 找。"
        "摘要裡做完的事不要重做，有新的訊息就以新的訊息為準。\n\n"
        f"# 交接摘要\n{summary.strip()}\n\n"
        f"# 最近的對話原文\n{recent}\n"
        "</context_handoff>"
    )


def compact_conversation(conv: "Conversation", summary: str, *, now: float, context_limit: int, prompt_tokens: int) -> str:
    """Swap everything so far for the handoff; returns the handoff text."""
    if not summary.strip():
        raise CompactionError("交接摘要是空的")
    handoff = handoff_text(conv, summary, now=now, context_limit=context_limit)
    conv.compact(handoff, ts=now, prompt_tokens=prompt_tokens)
    return handoff
