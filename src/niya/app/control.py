"""The /model and /effort commands. The REPL uses them now; Discord will reuse them later."""

from collections.abc import Awaitable, Callable

import httpx

from niya.app.runtime import AnyAgent, CoreTools, build_agent, needs_new_session, restart_conversation
from niya.core.clock import Clock
from niya.core.config import Settings
from niya.core.db import Database
from niya.llm.choice import (
    EFFORT_HELP,
    EFFORTS,
    OPENROUTER,
    ModelChoice,
    clear_choice,
    clear_effort,
    configured_choice,
    current_choice,
    current_effort,
    describe_options,
    effort_for,
    format_models,
    openrouter_problem,
    parse_choice,
    save_choice,
    save_effort,
    search_models,
)
from niya.llm.openrouter import ModelInfo, list_models

Confirm = Callable[[str], Awaitable[bool]]
Catalog = Callable[[], Awaitable[list[ModelInfo]]]


class ModelControl:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        clock: Clock,
        tools: CoreTools,
        prompt: Callable[[], str],
        agent: AnyAgent,
        confirm: Confirm,
        catalog: Catalog | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.clock = clock
        self.tools = tools
        self.prompt = prompt
        self.agent = agent
        self.confirm = confirm
        self.catalog = catalog or (lambda: list_models(base_url=settings.base_url))
        self._models: list[ModelInfo] | None = None
        self._results: list[ModelInfo] = []

    def status(self) -> str:
        choice, source = current_choice(self.db, self.settings)
        effort, effort_source = current_effort(self.db, self.settings)
        return f"模型 {choice}（{source}），思考強度 {self._effort_label(effort)}（{effort_source}）"

    async def model(self, arg: str) -> str:
        arg = arg.strip()
        if not arg:
            return f"目前：{self.status()}\n{describe_options()}\n用法：/model <模型>、/model search <關鍵字>、/model reset 改回設定檔"
        if arg == "search" or arg.startswith("search "):
            return await self._search(arg.removeprefix("search").strip())
        if arg == "reset":
            return await self._switch(configured_choice(self.settings), reset=True)
        if arg.isdigit():
            index = int(arg) - 1
            if not 0 <= index < len(self._results):
                return "沒有這個編號。先用 /model search <關鍵字> 列出模型。"
            return await self._switch(ModelChoice(OPENROUTER, self._results[index].id))
        try:
            choice = parse_choice(arg)
        except ValueError as exc:
            return f"{exc}\n不知道 ID 的話，用 /model search <關鍵字> 找。"
        if choice.provider == OPENROUTER:
            models = await self._catalog()
            if models is not None and (problem := openrouter_problem(choice.model, models)):
                return problem
        return await self._switch(choice)

    async def effort(self, arg: str) -> str:
        arg = arg.strip()
        provider = self.agent.engine
        levels = EFFORTS[provider]
        if not arg:
            effort, source = current_effort(self.db, self.settings)
            return (
                f"目前的思考強度：{self._effort_label(effort)}（{source}）\n{EFFORT_HELP}\n"
                f"{provider} 可用：{'、'.join(levels)}\n用法：/effort <強度>、/effort reset 改回設定檔"
            )
        if arg == "reset":
            clear_effort(self.db)
            effort = self.settings.effort
        elif arg in levels:
            save_effort(self.db, arg)
            effort = arg
        else:
            return f"{provider} 沒有「{arg}」這個強度，可用：{'、'.join(levels)}"
        await self.agent.set_effort(effort)
        return f"思考強度改成 {self._effort_label(effort)}。"

    def _effort_label(self, effort: str) -> str:
        actual = effort_for(self.agent.engine, effort)
        if actual is None:
            return "預設"
        return actual if actual == effort else f"{actual}（{self.agent.engine} 不支援 {effort}）"

    async def _catalog(self) -> list[ModelInfo] | None:
        if self._models is None:
            try:
                self._models = await self.catalog()
            except httpx.HTTPError:
                return None
        return self._models

    async def _search(self, keyword: str) -> str:
        if not keyword:
            return "用法：/model search <關鍵字>，例如 /model search sonnet、/model search gemini flash"
        models = await self._catalog()
        if models is None:
            return "連不上 OpenRouter，查不到模型清單。"
        self._results = search_models(models, keyword)
        if not self._results:
            return f"OpenRouter 上沒有符合「{keyword}」又支援工具呼叫的模型。"
        lines = [f"OpenRouter 上符合「{keyword}」的模型（只列支援工具呼叫的）：", format_models(self._results), "用 /model <編號> 切換。"]
        if not self.settings.api_key:
            lines.append("注意：還沒設定 OPENROUTER_API_KEY，要先寫進 .env 再重開才能用。")
        return "\n".join(lines)

    async def _switch(self, choice: ModelChoice, *, reset: bool = False) -> str:
        agent = self.agent
        if choice.provider == agent.engine:
            await agent.set_model(choice.model)
        else:
            try:
                replacement = build_agent(self.settings, self.db, self.clock, self.tools, agent.conversation, choice)
            except ValueError as exc:
                return f"無法切換：{exc}"
            if needs_new_session(agent.conversation, choice):
                if not await self.confirm(f"換到 {choice.provider} 要開新 session，目前的對話會結束（紀錄留在資料庫）。確定嗎？"):
                    await replacement.aclose()
                    return "沒有切換。"
                replacement.conversation = restart_conversation(agent.conversation, self.settings, self.clock, self.prompt, reason="engine_switch")
            await agent.aclose()
            self.agent = replacement
        if reset:
            clear_choice(self.db)
        else:
            save_choice(self.db, choice)
        return f"已切換到 {choice}（session {self.agent.conversation.session_id}）。"
