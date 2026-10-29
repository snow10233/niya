"""delegate_task: hand a self-contained job to a subagent with its own context and get its report back."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from niya.tools.registry import Tool, ToolContext, ToolError, ToolText

MAX_PARALLEL = 3
# (task, context, the calling tool context) -> (the subagent's report, external source it read, if any)
Runner = Callable[[str, str, ToolContext], Awaitable[tuple[str, str | None]]]

DESCRIPTION = (
    "把一件可以獨立完成的工作交給子 agent（另一個你），它用自己的 context 做完，只把報告交回來。"
    "適合：要讀很多網頁或檔案、會產生大量輸出、或可以同時進行的調查，這樣你的 context 不會被塞滿。"
    "子 agent 看不到你和主人的對話，task 要寫清楚目標，context 寫它需要知道的一切（路徑、前因後果、限制）。"
    f"子 agent 不能用記憶、鬧鐘和技能管理，也不能再派子 agent。可以一次呼叫好幾個，同時最多跑 {MAX_PARALLEL} 個。"
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "description": "要完成的事和完成的標準"},
        "context": {"type": "string", "description": "子 agent 需要知道的背景"},
    },
    "required": ["task"],
}


def delegate_tools(runner: Runner) -> list[Tool]:
    slots = asyncio.Semaphore(MAX_PARALLEL)

    async def delegate_task(args: dict[str, Any], ctx: ToolContext) -> ToolText:
        task = str(args.get("task") or "").strip()
        if not task:
            raise ToolError("task 不能是空的。")
        async with slots:
            report, external = await runner(task, str(args.get("context") or "").strip(), ctx)
        text = report.strip() or "（子 agent 沒有交回報告）"
        # A subagent that read outside content may be relaying what that content told it.
        return ToolText(text, external=external is not None, source=f"delegate:{external}" if external else None)

    return [Tool(name="delegate_task", description=DESCRIPTION, parameters=PARAMETERS, handler=delegate_task, toolset="delegate", parallel_safe=True, timeout_s=1800, max_result_chars=12_000)]
