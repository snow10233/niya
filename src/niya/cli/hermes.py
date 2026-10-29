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


async def _skills(settings: Settings, db: Database, source: Path, notes_path: Path | None, parallel: int, only: list[str]) -> int:
    from niya.app.runtime import sleep_completer
    from niya.hermes.skills import discover, load_reviews, render_checklist, review_all, save_reviews, stage, with_skill_notes

    skills = discover(source / "skills")
    if not skills:
        print(f"{source / 'skills'} 裡沒有技能。", file=sys.stderr)
        return 1
    checklist = settings.home / "hermes-skills.md"
    staging = settings.home / "hermes-skills"
    cache = settings.home / "hermes-skills.json"
    previous = load_reviews(cache, skills) if only else {}
    if unknown := sorted(set(only) - {s.name for s in skills}):
        print(f"沒有這些技能：{'、'.join(unknown)}", file=sys.stderr)
        return 1
    counts = {o: sum(s.origin == o for s in skills) for o in ("self", "changed", "bundled")}
    print(f"找到 {len(skills)} 個技能：自己寫的 {counts['self']}、內建被改過的 {counts['changed']}、內建沒改過的 {counts['bundled']}（只列出來，不讀）。")
    to_read = [s for s in skills if s.name in only or s.name not in previous]
    print(f"用 {_sleep_model(settings)} 讀 {sum(s.origin != 'bundled' for s in to_read)} 個技能，同時讀 {parallel} 個…", flush=True)
    clock = RealClock()
    clients: list = []
    try:
        before = db.one("SELECT COALESCE(MAX(id), 0) AS id FROM llm_calls")["id"]
        draft = (notes_path or settings.home / "hermes-notes.md").with_name("persona.hermes.md")
        persona = draft.read_text(encoding="utf-8") if draft.exists() else ""
        fresh = await review_all(sleep_completer(settings, db, clock, None, clients)(), to_read, persona=persona, parallel=parallel, report=lambda line: print(line, flush=True))
    finally:
        for c in clients:
            await c.aclose()
    for result in fresh:  # the rest stay as they were staged, owner's edits included
        stage(result, staging)
    results = [{r.skill.name: r for r in fresh}.get(s.name) or previous[s.name] for s in skills]
    save_reviews(results, cache)
    checklist.write_text(render_checklist(results, staging), encoding="utf-8")
    notes = notes_path or settings.home / "hermes-notes.md"
    old = notes.read_text(encoding="utf-8") if notes.exists() else ""
    if old:
        notes.with_name(notes.name + ".bak").write_text(old, encoding="utf-8")
    notes.write_text(with_skill_notes(old, results), encoding="utf-8")
    moving = [r for r in results if r.verdict == "move"]
    print(f"建議搬 {len(moving)} 個、不搬 {len(results) - len(moving)} 個；從技能抽出 {sum(len(r.notes) for r in results)} 條筆記。")
    if failed := [r.skill.name for r in results if r.error]:
        print(f"{len(failed)} 個技能模型沒有回應（{'、'.join(failed)}），清單上標了，請自己判斷。", file=sys.stderr)
    print(f"搬家清單：{checklist}\n改好的技能：{staging}/\n筆記接在：{notes}" + ("（原本的備份在 .bak）" if old else ""))
    print(f"模型用量：{_usage(db, before)}")
    print("改好以後用 niya hermes apply 一起匯入。")
    return 0


def _apply_skills(settings: Settings, db: Database, checklist: Path) -> None:
    from niya.hermes.skills import import_skills, parse_checklist
    from niya.skills.store import SkillStore

    chosen = [name for name, verdict in parse_checklist(checklist.read_text(encoding="utf-8")).items() if verdict == "move"]
    store = SkillStore(settings.home / "skills", db, RealClock())
    stats = import_skills(store, checklist.with_suffix(""), chosen)
    print(f"技能：搬了 {len(stats.moved)} 個到 {store.root}" + (f"，{len(stats.existing)} 個已經有了沒動" if stats.existing else "") + "。")
    for name, why in stats.refused:
        print(f"  沒搬 {name}：{why}", file=sys.stderr)


async def _apply(settings: Settings, db: Database, review: Path, persona_draft: Path | None, persona_out: Path | None, skills: Path | None) -> int:
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
    checklist = skills or review.with_name("hermes-skills.md")
    if checklist.exists():
        _apply_skills(settings, db, checklist)
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
        if args.hermes_command == "skills":
            return await _skills(settings, db, args.source, args.notes, args.parallel, [n for n in (args.only or "").split(",") if n])
        return await _apply(settings, db, args.review, args.persona_draft, args.persona, args.skills)
    finally:
        db.close()
