"""Which model niya thinks with: a provider plus a model, set in config and switchable at runtime."""

from collections.abc import Sequence
from dataclasses import dataclass

from niya.core.config import PROVIDERS, Settings
from niya.core.db import Database
from niya.llm.openrouter import ModelInfo

CLAUDE = "claude"
OPENROUTER = "openrouter"
PROVIDER_LABELS = {
    CLAUDE: "Claude 訂閱，透過官方 Claude Code 執行（niya login 登入；有 ANTHROPIC_API_KEY 時改用 API 計費）",
    OPENROUTER: "OpenRouter（需要 OPENROUTER_API_KEY）",
}
# Claude Code resolves these aliases to the newest model of each family.
CLAUDE_MODELS = {
    "opus": "Claude Opus 最新版",
    "sonnet": "Claude Sonnet 最新版",
    "haiku": "Claude Haiku 最新版",
    "fable": "Claude Fable 最新版（最強，用量消耗也最大）",
}
DEFAULT_MODELS = {CLAUDE: "opus"}
OPENROUTER_EXAMPLE = "openrouter:anthropic/claude-sonnet-5.5"
META_KEY = "llm.choice"
EFFORT_KEY = "llm.effort"
EFFORTS = {
    CLAUDE: ("low", "medium", "high", "xhigh", "max"),
    OPENROUTER: ("none", "minimal", "low", "medium", "high", "xhigh", "max"),
}
EFFORT_HELP = "思考強度：越高想得越久、越準，也越慢、越耗用量。none 是不思考（只有 OpenRouter 的部分模型支援）。"


@dataclass(frozen=True)
class ModelChoice:
    provider: str
    model: str

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


def infer_provider(model: str) -> str:
    # OpenRouter IDs are vendor/model; Claude Code takes aliases and claude-* IDs.
    return OPENROUTER if "/" in model else CLAUDE


def parse_choice(text: str) -> ModelChoice:
    """'claude:sonnet', 'openrouter:vendor/model', or a bare model whose provider is inferred."""
    text = text.strip()
    provider, sep, model = text.partition(":")
    if not (sep and provider in PROVIDERS):
        provider, model = (text, "") if text in PROVIDERS else (infer_provider(text), text)
    model = model.strip() or DEFAULT_MODELS.get(provider, "")
    if not model:
        raise ValueError(f"{provider} 要指定模型，例如 {OPENROUTER_EXAMPLE}")
    return ModelChoice(provider, model)


def configured_choice(settings: Settings) -> ModelChoice:
    if settings.provider:
        return ModelChoice(settings.provider, settings.model or DEFAULT_MODELS.get(settings.provider, ""))
    if settings.model:
        return ModelChoice(infer_provider(settings.model), settings.model)
    return ModelChoice(CLAUDE, DEFAULT_MODELS[CLAUDE])


def current_choice(db: Database, settings: Settings) -> tuple[ModelChoice, str]:
    """The owner's runtime choice wins over the config file; returns the choice and where it came from."""
    saved = db.get_meta(META_KEY)
    if saved:
        try:
            return parse_choice(saved), "手動選擇"
        except ValueError:
            pass
    return configured_choice(settings), "設定檔"


def save_choice(db: Database, choice: ModelChoice) -> None:
    db.set_meta(META_KEY, str(choice))


def clear_choice(db: Database) -> None:
    db.execute("DELETE FROM state_meta WHERE key = ?", (META_KEY,))


def current_effort(db: Database, settings: Settings) -> tuple[str, str]:
    """'' means the model's own default."""
    saved = db.get_meta(EFFORT_KEY)
    if saved is not None:
        return saved, "手動選擇"
    return settings.effort, "設定檔"


def save_effort(db: Database, effort: str) -> None:
    db.set_meta(EFFORT_KEY, effort)


def clear_effort(db: Database) -> None:
    db.execute("DELETE FROM state_meta WHERE key = ?", (EFFORT_KEY,))


def effort_for(provider: str, effort: str) -> str | None:
    """The level an engine actually gets; Claude cannot turn thinking off, so its floor is low."""
    if not effort:
        return None
    if provider == CLAUDE and effort not in EFFORTS[CLAUDE]:
        return "low"
    return effort


def search_models(models: Sequence[ModelInfo], keyword: str, *, limit: int = 15) -> list[ModelInfo]:
    """Models niya can use (tool calling) whose ID or name contains every word of the keyword."""
    words = keyword.lower().split()
    return [
        m
        for m in models
        if m.tools and not m.id.endswith(":batch") and all(w in f"{m.id} {m.name}".lower() for w in words)
    ][:limit]


def _size(tokens: int | None) -> str:
    if not tokens:
        return "?"
    return f"{round(tokens / 1_000_000, 1):g}M" if tokens >= 1_000_000 else f"{tokens // 1000}k"


def _price(value: float | None) -> str:
    return "?" if value is None else f"${value:.3g}"


def format_models(models: Sequence[ModelInfo]) -> str:
    width = max(len(m.id) for m in models)
    return "\n".join(
        f"  {i:>2}. {m.id:<{width}}  context {_size(m.context_length):>5}  輸入/輸出 {_price(m.prompt_price)}/{_price(m.completion_price)} 每百萬 token"
        for i, m in enumerate(models, start=1)
    )


def openrouter_problem(model: str, models: Sequence[ModelInfo]) -> str | None:
    info = next((m for m in models if m.id == model), None)
    if info is None:
        return f"OpenRouter 上找不到 {model}，用 /model search <關鍵字> 找找看。"
    if not info.tools:
        return f"{model} 不支援工具呼叫，niya 沒辦法用它。"
    return None


def describe_options() -> str:
    lines = ["可選的模型："]
    lines += [f"  claude:{alias:<8}{label}" for alias, label in CLAUDE_MODELS.items()]
    lines.append(f"  openrouter:<模型 ID>  OpenRouter 上的模型，例如 {OPENROUTER_EXAMPLE}")
    lines.append("  （claude 也接受完整的模型 ID，例如 claude:claude-opus-5-5）")
    lines.append("找 OpenRouter 的模型：/model search <關鍵字>，再用 /model <編號> 切換")
    return "\n".join(lines)
