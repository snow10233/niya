"""Hermes' curated notes (USER.md, MEMORY.md) and persona (SOUL.md), sorted into niya's layers.

The model splits every § entry into single facts and sorts each one: persona (never fades), owner (a memory
about the owner), self (something niya knows) or drop (Hermes-only). The owner reviews that list before
anything is written. On apply, a note niya already learned from the replay counts as meeting it again; a
note that overturns a memory supersedes it; the rest become memories of their own. Hermes kept these notes
in every session's prompt, so a new one starts sturdier than a fresh memory would.
"""

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from niya.memory.curve import initial_stability
from niya.memory.engine import KINDS, LIVE, MemoryEngine
from niya.sleep.llm import Completer, parse_json
from niya.tools.threat_patterns import first_threat_message

ENTRY_DELIMITER = "\n§\n"
CATEGORIES = ("persona", "owner", "self", "drop")
NOTE_KINDS = ("fact", "preference", "intention", "insight")
NOTE_BOOST = 3.0  # rehearsed in every Hermes session for weeks
SENSITIVE = "⚠️"
PERSONA_HEADING = "## 從 Hermes 的筆記帶來的（永遠記得）"
_HERMES_RUNTIME = re.compile(r"[^\n。]*Hermes Agent[^\n。]*。")
_LINE = re.compile(r"^- \[(persona|owner|self|drop)\] \((\w+), (\d+)\) (.+)$")

SPLIT_SYSTEM = """你在幫 niya 搬家。niya 是一個會遺忘的長期 agent，以前跑在 Hermes 上。Hermes 留下兩份筆記：
USER.md（關於主人）和 MEMORY.md（agent 自己的筆記），每條筆記常常塞了好幾件事。

把每條筆記拆成「一件事一條」，每條用一句完整、自然的繁體中文寫，看單句就懂（主詞寫出來，例如「主人…」）。
每條分到一類：
- persona：niya 待人處事的準則、跟主人相處的方式、說話風格，以及主人幾乎不會變的基本資料（姓名、生日、學校、住處）。這些寫進人格設定，永遠不會忘。
- owner：主人的偏好、習慣、狀況、正在做的事。會隨時間淡忘，用到才會變牢。
- self：niya 自己需要知道的環境資訊（機器、服務、路徑、帳號、工作流程）。
- drop：只對 Hermes 有意義的（Hermes 的設定、curator、gateway、cron 限制、Hermes 專屬工具），換到 niya 就沒用了。
kind 用 fact / preference / intention / insight；importance 1–10（10 是一定要記得的）。
牽涉主人隱私或過去創傷的內容，sensitive 設成 true，原樣保留它附帶的處理原則（例如「不主動提」）。

只回 JSON：{"notes": [{"file": "USER.md", "entry": 1, "content": "...", "category": "owner", "kind": "preference", "importance": 6, "sensitive": false}]}"""

MATCH_SYSTEM = """niya 要把舊筆記放進記憶。每條筆記附上 niya 現有、可能相關的記憶。逐條判斷：
- same：現有的某條記憶講的是同一件事（換句話說也算），填它的編號。
- replaces：筆記是比較新的資訊，推翻了現有的某條記憶，填它的編號。
- new：現有的記憶都沒講到這件事。
只回 JSON：{"matches": [{"note": 1, "verdict": "same", "memory": 12}]}"""


@dataclass
class Note:
    file: str
    entry: int
    content: str
    category: str
    kind: str
    importance: int
    sensitive: bool = False
    original: str = ""


def read_entries(memories_dir: Path) -> dict[str, list[str]]:
    out = {}
    for name in ("USER.md", "MEMORY.md"):
        path = memories_dir / name
        if path.exists():
            out[name] = [e.strip() for e in path.read_text(encoding="utf-8").split(ENTRY_DELIMITER) if e.strip()]
    return out


def soul(state_db: Path) -> str:
    """The persona part of Hermes' latest system prompt (SOUL.md comes first, Hermes' own rules after)."""
    conn = sqlite3.connect(f"file:{state_db}?immutable=1", uri=True)
    try:
        row = conn.execute(
            "SELECT p.prompt FROM sessions s JOIN system_prompts p ON p.hash = s.system_prompt_hash WHERE s.source NOT IN ('cron', 'subagent') ORDER BY s.started_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    text = (row[0] if row else "").split("\nYou run on Hermes Agent", 1)[0]
    return _HERMES_RUNTIME.sub("", text).strip()


async def split(completer: Completer, entries: dict[str, list[str]]) -> list[Note]:
    listing = "\n\n".join(f"# {name}\n" + "\n".join(f"{i}. {e}" for i, e in enumerate(items, start=1)) for name, items in entries.items())
    data = parse_json(await completer.complete(system=SPLIT_SYSTEM, prompt=listing, task="hermes_notes"))
    notes = []
    for item in data.get("notes") or []:
        if not isinstance(item, dict) or not item.get("content"):
            continue
        file, entry = str(item.get("file") or ""), int(item.get("entry") or 0)
        original = entries.get(file, [])[entry - 1] if 0 < entry <= len(entries.get(file, [])) else ""
        notes.append(
            Note(
                file=file,
                entry=entry,
                content=" ".join(str(item["content"]).split()),
                category=item.get("category") if item.get("category") in CATEGORIES else "owner",
                kind=item.get("kind") if item.get("kind") in NOTE_KINDS else "fact",
                importance=min(max(int(item.get("importance") or 5), 1), 10),
                sensitive=bool(item.get("sensitive")),
                original=original,
            )
        )
    return notes


def render_review(notes: list[Note]) -> str:
    lines = [
        "# Hermes 筆記審核",
        "",
        "每行開頭的標籤決定去處：[persona] 寫進人格設定（永遠記得）、[owner] 關於主人的記憶、[self] niya 自己知道的事、[drop] 不帶過來。",
        f"可以改標籤、改內容、整行刪掉；括號裡是類型和重要度（1–10），也可以改。{SENSITIVE} 是敏感內容，請特別確認。",
        "「> 原文」那行只是參考，改了也不會影響結果。",
    ]
    shown: set[tuple[str, int]] = set()
    for file in dict.fromkeys(n.file for n in notes):
        lines += ["", f"## {file}"]
        for note in (n for n in notes if n.file == file):
            if (file, note.entry) not in shown and note.original:
                lines += ["", f"> 原文 {note.entry}：{' '.join(note.original.split())}"]
                shown.add((file, note.entry))
            mark = f"{SENSITIVE} " if note.sensitive else ""
            lines.append(f"- [{note.category}] ({note.kind}, {note.importance}) {mark}{note.content}")
    return "\n".join(lines) + "\n"


def parse_review(text: str) -> list[Note]:
    notes = []
    file = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            file = line[3:].strip()
        elif match := _LINE.match(line):
            category, kind, importance, content = match.groups()
            sensitive = content.startswith(SENSITIVE)
            content = content.removeprefix(SENSITIVE).strip()
            if kind not in NOTE_KINDS:
                raise ValueError(f"不認得的類型 {kind}：{line}")
            notes.append(Note(file=file, entry=0, content=content, category=category, kind=kind, importance=min(max(int(importance), 1), 10), sensitive=sensitive))
    return notes


def persona_with_notes(persona: str, notes: list[Note]) -> str:
    items = [n.content for n in notes if n.category == "persona" and n.content not in persona]
    if not items:
        return persona
    body = persona.rstrip()
    if PERSONA_HEADING not in body:
        body += f"\n\n{PERSONA_HEADING}"
    return body + "\n" + "\n".join(f"- {item}" for item in items) + "\n"


@dataclass
class ApplyStats:
    same: int = 0
    replaced: int = 0
    created: int = 0
    persona: int = 0
    dropped: int = 0
    blocked: int = 0


async def apply(engine: MemoryEngine, completer: Completer, notes: list[Note]) -> ApplyStats:
    stats = ApplyStats(persona=sum(n.category == "persona" for n in notes), dropped=sum(n.category == "drop" for n in notes))
    keep = []
    for note in (n for n in notes if n.category in ("owner", "self")):
        if first_threat_message(note.content, "strict"):
            stats.blocked += 1
        else:
            keep.append(note)
    if not keep:
        return stats
    vectors = await engine.embed([n.content for n in keep])
    if vectors is None:
        raise ValueError("embedding 失敗，沒辦法比對既有的記憶，稍後再試。")
    candidates = [[(m, score) for m, score in engine.nearest(v, 4) if score >= 0.3 and m.kind in KINDS] for v in vectors]
    verdicts: dict[int, tuple[str, int | None]] = {}
    if any(candidates):
        prompt = "\n\n".join(
            f"筆記 {i}：{note.content}\n" + ("\n".join(f"  - {m.id} {m.content}" for m, _ in found) or "  （沒有相關記憶）")
            for i, (note, found) in enumerate(zip(keep, candidates), start=1)
        )
        data = parse_json(await completer.complete(system=MATCH_SYSTEM, prompt=prompt, task="hermes_match"))
        for item in data.get("matches") or []:
            if isinstance(item, dict) and str(item.get("note", "")).isdigit():
                memory = item.get("memory")
                verdicts[int(item["note"])] = (str(item.get("verdict")), int(memory) if str(memory).isdigit() else None)
    now = engine.clock.now()
    with engine.db.transaction():
        for i, (note, vector, found) in enumerate(zip(keep, vectors, candidates), start=1):
            verdict, memory_id = verdicts.get(i, ("new", None))
            target = next((m for m, _ in found if m.id == memory_id and m.state in LIVE), None)
            if verdict == "same" and target is not None:
                engine.review(target, kind="reencounter", ref_id="hermes-notes", at=now)
                stats.same += 1
                continue
            memory = engine.create(
                kind=note.kind,
                content=note.content,
                importance=note.importance,
                occurred_start=None,
                occurred_end=None,
                precision="unknown",
                origin="hermes",
                trust="owner" if note.category == "owner" else "self",
                stability=initial_stability(note.importance, engine.params) * NOTE_BOOST,
                vector=vector,
            )
            if verdict == "replaces" and target is not None:
                engine.supersede(target, memory, now)
                stats.replaced += 1
            else:
                stats.created += 1
    return stats
