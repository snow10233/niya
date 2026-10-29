"""Which model niya thinks with: a provider plus a model, set in config and switchable at runtime."""

from dataclasses import dataclass

from niya.config import PROVIDERS, Settings
from niya.db import Database

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
OPENROUTER_EXAMPLE = "openrouter:anthropic/claude-sonnet-4.6"
META_KEY = "llm.choice"


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


def describe_options() -> str:
    lines = ["可選的模型："]
    lines += [f"  claude:{alias:<8}{label}" for alias, label in CLAUDE_MODELS.items()]
    lines.append(f"  openrouter:<模型 ID>  OpenRouter 上的任何模型，例如 {OPENROUTER_EXAMPLE}")
    lines.append("  （claude 也接受完整的模型 ID，例如 claude:claude-opus-5-5）")
    return "\n".join(lines)
