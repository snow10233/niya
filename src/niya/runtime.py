import getpass
from collections.abc import Callable, Collection

from niya.agent.context import Conversation
from niya.agent.prompts import build_system_prompt, load_persona
from niya.clock import Clock
from niya.config import Settings
from niya.db import Database


def system_prompt_factory(settings: Settings, *, channel: str, features: Collection[str] = ()) -> Callable[[], str]:
    def build() -> str:
        return build_system_prompt(
            persona=load_persona(settings.persona_path),
            timezone=settings.timezone,
            channel=channel,
            run_as=getpass.getuser(),
            features=features,
        )

    return build


def open_conversation(db: Database, settings: Settings, clock: Clock, prompt: Callable[[], str]) -> Conversation:
    return Conversation.open_current(db, now=clock.now(), tz=settings.tz, system_prompt=prompt)


def restart_conversation(conv: Conversation, settings: Settings, clock: Clock, prompt: Callable[[], str], *, reason: str) -> Conversation:
    conv.end(now=clock.now(), reason=reason)
    return Conversation.start_new(conv.db, now=clock.now(), tz=settings.tz, system_prompt=prompt())
