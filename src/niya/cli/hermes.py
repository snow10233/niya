"""`niya hermes …`: bring a Hermes agent's history, notes and persona over (import → replay → notes → apply)."""

import sys
from datetime import datetime
from pathlib import Path

from niya.core.clock import Clock, FakeClock, RealClock
from niya.core.config import Settings
from niya.core.db import Database


def _engine(settings: Settings, db: Database, clock: Clock):
    from niya.llm.openrouter import OpenRouterClient, OpenRouterEmbedder, db_recorder
    from niya.memory.engine import MemoryEngine

    if not settings.api_key:
        raise ValueError("記憶的 embedding 需要 OPENROUTER_API_KEY。")
    client = OpenRouterClient(settings.api_key, base_url=settings.base_url, recorder=db_recorder(db, clock))
    engine = MemoryEngine(db, clock, settings.tz, embedder=OpenRouterEmbedder(client, settings.embedding_model, settings.embedding_dimensions))
    return engine, client


def _sleep_model(settings: Settings) -> str:
    model = settings.sleep_model or "目前對話用的模型"
    return model + (f"（effort {settings.sleep_effort}）" if settings.sleep_effort else "")


def _usage(db: Database, since_id: int) -> str:
    rows = db.query(
        "SELECT task, COUNT(*) AS n, SUM(prompt_tokens) AS p, SUM(completion_tokens) AS c, SUM(ok = 0) AS bad FROM llm_calls WHERE id > ? GROUP BY task ORDER BY n DESC",
        (since_id,),
    )
    return "、".join(f"{r['task']} {r['n']} 次（輸入 {r['p'] or 0:,}／輸出 {r['c'] or 0:,} token" + (f"，失敗 {r['bad']}" if r["bad"] else "") + "）" for r in rows) or "沒有呼叫模型"


async def _import(settings: Settings, db: Database, source: Path) -> int:
    from niya.core.redact import Redactor
    from niya.hermes.history import import_turns, read_turns

    turns, dropped = read_turns(source / "state.db")
    stats = import_turns(db, turns, tz=settings.tz, redact=Redactor(settings.secrets.values()))
    if not stats.turns:
        print("沒有新的對話要匯入。")
        return 0
    span = f"{datetime.fromtimestamp(stats.first, settings.tz):%Y-%m-%d} 到 {datetime.fromtimestamp(stats.last, settings.tz):%Y-%m-%d}"
    print(f"匯入 {span}：{stats.sessions} 個 session、{stats.turns} 個回合、{stats.messages} 則訊息、{stats.segments} 個片段（略過 {dropped} 個沉默的心跳）。")
    print("接著用 niya hermes replay 回放，睡眠整理才會把它們變成記憶。")
    return 0


async def _replay(settings: Settings, db: Database, until: str | None, parallel: int) -> int:
    from niya.app.runtime import sleep_completer
    from niya.hermes.replay import Replay

    clock = FakeClock(0)
    engine, client = _engine(settings, db, clock)
    clients: list = [client]
    try:
        completer = sleep_completer(settings, db, clock, client, clients)()
        before = db.one("SELECT COALESCE(MAX(id), 0) AS id FROM llm_calls")["id"]
        limit = datetime.strptime(until, "%Y-%m-%d %H:%M").replace(tzinfo=settings.tz).timestamp() if until else None
        print(f"睡眠整理用 {_sleep_model(settings)}，同時整理 {parallel} 個片段。", flush=True)
        replay = Replay(db, engine, completer, clock, budget_usd=settings.sleep_budget_usd, parallel=parallel, report=lambda line: print(line, flush=True))
        stats = await replay.run(present=RealClock().now(), until=limit)
        print(f"回放了 {stats.turns} 個回合、{stats.sleeps} 次睡眠（跳過 {stats.skipped_naps} 次沒事的午睡）：{stats.events} 個事件、{stats.memories} 條記憶，{stats.used} 條用上變牢，封存 {stats.archived} 條。")
        if stats.failed_segments:
            print(f"有 {stats.failed_segments} 個片段整理失敗，下次睡覺會再試。", file=sys.stderr)
        print(f"模型用量：{_usage(db, before)}")
        return 0
    finally:
        for c in clients:
            await c.aclose()


async def _notes(settings: Settings, db: Database, source: Path, out: Path | None) -> int:
    from niya.app.runtime import sleep_completer
    from niya.hermes.notes import read_entries, render_review, soul, split

    clock = RealClock()
    clients: list = []
    try:
        entries = read_entries(source / "memories")
        if not entries:
            print(f"{source / 'memories'} 裡沒有 USER.md 或 MEMORY.md。", file=sys.stderr)
            return 1
        print(f"用 {_sleep_model(settings)} 拆解 {sum(map(len, entries.values()))} 條筆記…")
        notes = await split(sleep_completer(settings, db, clock, None, clients)(), entries)
        review = out or settings.home / "hermes-notes.md"
        review.write_text(render_review(notes), encoding="utf-8")
        persona = review.with_name("persona.hermes.md")
        persona.write_text(soul(source / "state.db") + "\n", encoding="utf-8")
        counts = {c: sum(n.category == c for n in notes) for c in ("persona", "owner", "self", "drop")}
        print(f"拆成 {len(notes)} 條：persona {counts['persona']}、owner {counts['owner']}、self {counts['self']}、drop {counts['drop']}。")
        print(f"審核清單：{review}\n人格草稿（從 SOUL.md 來）：{persona}")
        print("改好以後用 niya hermes apply 匯入。")
        return 0
    finally:
        for c in clients:
            await c.aclose()


async def _apply(settings: Settings, db: Database, review: Path, persona_draft: Path | None, persona_out: Path | None) -> int:
    from niya.app.runtime import sleep_completer
    from niya.hermes.notes import apply, parse_review, persona_with_notes

    notes = parse_review(review.read_text(encoding="utf-8"))
    draft = persona_draft or review.with_name("persona.hermes.md")
    target = persona_out or settings.persona_path or settings.home / "persona.md"
    clock = RealClock()
    engine, client = _engine(settings, db, clock)
    clients: list = [client]
    try:
        stats = await apply(engine, sleep_completer(settings, db, clock, client, clients)(), notes)
    finally:
        for c in clients:
            await c.aclose()
    if draft.exists():
        target.write_text(persona_with_notes(draft.read_text(encoding="utf-8"), notes), encoding="utf-8")
        print(f"人格設定寫到 {target}（加了 {stats.persona} 條筆記）。")
    print(f"記憶：{stats.same} 條回放時已經記得（變牢）、{stats.replaced} 條取代舊的、{stats.created} 條新增；不帶 {stats.dropped} 條" + (f"，{stats.blocked} 條疑似注入被擋下" if stats.blocked else "") + "。")
    return 0


async def run_hermes_command(settings: Settings, args) -> int:
    db = Database(settings.db_path)
    try:
        if args.hermes_command == "import":
            return await _import(settings, db, args.source)
        if args.hermes_command == "replay":
            return await _replay(settings, db, args.until, args.parallel)
        if args.hermes_command == "notes":
            return await _notes(settings, db, args.source, args.out)
        return await _apply(settings, db, args.review, args.persona_draft, args.persona)
    finally:
        db.close()
