"""Hermes' skills: what they know goes to the notes review, the skills themselves to a checklist.

Hermes kept much of what it learned inside skills (paths, accounts, the owner's habits), and the replay never
saw it: sleep only reads tool names and arguments. For every skill the agent wrote or changed, the model pulls
the facts out in the notes' review format, says whether the skill still makes sense on niya, and lists the
edits that swap Hermes' tools for niya's. Every skill is copied into a staging folder with those edits applied,
so the owner can read exactly what will move; skills Hermes shipped and nobody touched are listed, not read.
"""

import asyncio
import hashlib
import json
import re
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from niya.hermes.notes import CATEGORIES, NOTE_KINDS, Note, note_line
from niya.skills.store import SKILL_DIR, SkillError, SkillStore, parse_skill
from niya.sleep.llm import Completer, parse_json
from niya.tools.fuzzy_match import fuzzy_find_and_replace

SKIP_DIRS = frozenset({".archive", ".curator_backups", ".hub"})
JUNK = frozenset({"__pycache__", ".DS_Store"})
PROMPT_CHARS = 250_000
ATTEMPTS = 3  # a model thinking at max effort sometimes spends its whole output before writing the JSON
ORIGINS = {"self": "Hermes 自己寫的", "changed": "Hermes 內建、被改過的", "bundled": "Hermes 內建、沒改過的"}
VERDICTS = ("move", "drop")
NOTES_HEADING = "# 從 Hermes 技能抽出的筆記"
_CHECK_LINE = re.compile(r"^- \[(move|drop)\] ([a-z0-9][a-z0-9._-]*)：")

SKILL_SYSTEM = """你在幫 niya 搬家。niya 是一個會遺忘的長期 agent，以前跑在 Hermes 上，現在換成 niya 自己的程式，主人只用 Discord 跟它說話。
Hermes 把很多學到的東西寫在技能（SKILL.md 加附檔）裡，你要讀一個技能，做三件事。

一、抽出技能裡記著的事實，放進 niya 的記憶。只抽事實：主人的偏好、習慣、狀況、決定，以及 niya 需要知道的環境資訊（機器、服務、路徑、帳號、專案現況）。
操作步驟和指令留在技能裡，不要抽。每條用一句完整、自然的繁體中文寫，看單句就懂（主詞寫出來，例如「主人…」）。分類：
- persona：跟主人相處的方式、說話風格這類人格層級的準則，以及主人幾乎不會變的基本資料。這些會一直放在 niya 的人格設定裡，所以要少而精：做某件工作的規則和作法不算（那是 owner 或 self），最後附的人格設定裡已經有的也不要再列。
- owner：主人的偏好、習慣、狀況、正在做的事。
- self：niya 自己需要知道的環境資訊。
- drop：只對 Hermes 有意義的（Hermes 的設定、gateway、curator、cron 機制、Hermes 專屬工具）。
kind 用 fact / preference / intention / insight；importance 1–10。主人過去的創傷、健康、法律問題這類說出去會傷到主人的內容，sensitive 設成 true，原樣保留它附帶的處理原則；一般的成人話題、購物和收藏偏好不算。

二、判斷技能要不要搬（verdict）：
- drop：內容在講 Hermes 本身（設定、gateway、session、model routing、curator、cron 機制、怎麼改 MEMORY.md／USER.md／SOUL.md），或 niya 已經內建同樣的機制（睡覺整理記憶、做夢、鬧鐘），或已經過時。
- move：其他的，包括主人的專案、工作流程、外部服務和網站的操作方法。只有小地方依賴 Hermes 的，搬過去再用 edits 改掉。
reason 用一句話說明。

三、verdict 是 move 的話，列出要改的地方（edits），把 Hermes 的工具和做法換成 niya 的：
- terminal、process、read_file、write_file、patch、search_files、web_search、web_extract、delegate_task、browser_exec、image_generate、text_to_speech、skills_list、skill_view、skill_manage：niya 有同名工具，不用改。
- vision_analyze → niya 直接看得到圖：用 read_file 讀圖片檔，工具結果附的截圖也會直接給你看。
- browser_navigate、browser_click、browser_snapshot 這類 → browser_exec（寫 Python 操作瀏覽器）。
- send_message、discord、discord_admin → discord 工具（action：list_channels、read、send（可附檔）、delete、create_thread、rename）。niya 不接 Telegram。
- cronjob、cronjob_manage（Hermes 排程）→ alarm（purpose=task 加 repeat 的 cron 規則；時間到了是叫醒 niya 本人去做，不是另開 session 跑 prompt）。
- memory（寫 MEMORY.md／USER.md）→ remember；session_search → deep_recall 或 recall_timeline。niya 沒有 MEMORY.md、USER.md，記憶會淡忘、睡覺時整理。
- SOUL.md → niya 的人格設定（persona.md，主人管理，niya 不自己改）。
- MCP 工具在 niya 叫 mcp__<server>__<tool>。MCP server 寫在 niya 設定檔的 [mcp.servers.<name>]（command、args、env，或 url），niya 啟動時連上就固定了：沒有逐一挑選工具的設定，也不能在執行中開關或重連，改設定要請主人重開 niya。
- niya 本身是 systemd 的 niya.service。Hermes gateway 的 reload、restart 換到 niya 就沒有了；niya 也不能自己停掉或重開服務。
- execute_code、clarify、todo、mixture_of_agents：niya 沒有。execute_code 改用 terminal 跑 python，clarify 改成直接問主人。
- 技能自己資料夾的路徑已經換成 ${SKILL_DIR}，不用再改。~/.hermes 底下的其他檔案（scripts、.env、logs 等）在主機上還在，路徑照舊；但要靠 Hermes 在跑才有的東西（gateway、hermes 指令、Hermes 的 cron、dashboard）換到 niya 就沒了。
edits 只改必要的地方，不要重寫整份、不要順便潤稿。old 從原文逐字複製，長到在那個檔案裡只出現一次；new 是改好的文字；file 是 ===== 標題上的檔案路徑。不用改就給空陣列。

只回 JSON：{"verdict": "move", "reason": "...", "notes": [{"content": "...", "category": "owner", "kind": "fact", "importance": 6, "sensitive": false}], "edits": [{"file": "SKILL.md", "old": "...", "new": "..."}]}"""


@dataclass(frozen=True)
class HermesSkill:
    name: str
    folder: Path
    path: str  # relative to Hermes' skills folder, e.g. software-development/snowcast
    origin: str
    description: str
    use_count: int = 0
    last_used: str = ""  # YYYY-MM-DD


@dataclass(frozen=True)
class Edit:
    file: str
    old: str
    new: str


@dataclass
class SkillReview:
    skill: HermesSkill
    verdict: str
    reason: str
    notes: list[Note] = field(default_factory=list)
    edits: list[Edit] = field(default_factory=list)
    failed_edits: int = 0
    error: str = ""


@dataclass(frozen=True)
class StagedFile:
    source: Path
    target: str  # relative to the skill folder, same as in Hermes (the skill's scripts may look for it there)
    text: str | None  # None: not text, copied as it is


def _dir_hash(folder: Path) -> str:
    """Hermes' own hash for its bundled manifest (tools/skills_sync.py)."""
    hasher = hashlib.md5()
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            hasher.update(str(path.relative_to(folder)).encode("utf-8"))
            hasher.update(path.read_bytes())
    return hasher.hexdigest()


def discover(root: Path) -> list[HermesSkill]:
    bundled = {}
    manifest = root / ".bundled_manifest"
    if manifest.exists():
        bundled = dict(line.strip().split(":", 1) for line in manifest.read_text(encoding="utf-8").splitlines() if ":" in line)
    usage = json.loads((root / ".usage.json").read_text(encoding="utf-8")) if (root / ".usage.json").exists() else {}
    skills = []
    for skill_md in sorted(root.rglob("SKILL.md")):
        folder = skill_md.parent
        relative = folder.relative_to(root)
        if relative.parts and relative.parts[0] in SKIP_DIRS:
            continue
        try:
            meta, _ = parse_skill(skill_md.read_text(encoding="utf-8", errors="replace"))
        except SkillError:
            meta = {}
        name = str(meta.get("name") or folder.name)
        if name in bundled:
            origin = "changed" if _dir_hash(folder) != bundled[name] else "bundled"
        else:
            origin = "self"
        used = usage.get(name) or {}
        skills.append(
            HermesSkill(
                name=name, folder=folder, path=str(relative), origin=origin, description=" ".join(str(meta.get("description") or "").split()),
                use_count=int(used.get("use_count") or 0), last_used=str(used.get("last_used_at") or "")[:10],
            )
        )
    return skills


def _decode(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def staged_files(skill: HermesSkill) -> list[StagedFile]:
    """The skill's own files (not the skills Hermes nested inside it, not caches), with the skill's own
    folder written as ${SKILL_DIR}."""
    nested = [p.parent for p in skill.folder.rglob("SKILL.md") if p.parent != skill.folder]
    nested_names = "|".join(re.escape(n.name) for n in nested)
    own = re.compile(r"(?:~|/root|\$HOME)/\.hermes/skills/" + re.escape(skill.path) + (rf"(?!/(?:{nested_names})(?![\w-]))" if nested else "") + r"(?![\w-])")
    staged = []
    for path in sorted(skill.folder.rglob("*")):
        relative = path.relative_to(skill.folder)
        if not path.is_file() or path.suffix == ".pyc" or JUNK.intersection(relative.parts) or any(path.is_relative_to(n) for n in nested):
            continue
        text = _decode(path)
        staged.append(StagedFile(path, str(relative), own.sub(lambda _: SKILL_DIR, text) if text is not None else None))
    return staged


def prompt_for(skill: HermesSkill, files: list[StagedFile]) -> str:
    parts = [f"技能：{skill.name}（{ORIGINS[skill.origin]}，用了 {skill.use_count} 次" + (f"，最後一次 {skill.last_used}" if skill.last_used else "") + "）"]
    budget = PROMPT_CHARS
    left_out = []
    for f in sorted(files, key=lambda f: f.target != "SKILL.md"):
        if f.text is None or len(f.text) > budget:
            left_out.append(f.target)
            continue
        budget -= len(f.text)
        parts.append(f"===== {f.target} =====\n{f.text}")
    if left_out:
        parts.append("沒附上的檔案（太長或不是文字）：" + "、".join(left_out))
    return "\n\n".join(parts)


def _parse(skill: HermesSkill, data: dict) -> SkillReview:
    notes = []
    for item in data.get("notes") or []:
        if not isinstance(item, dict) or not item.get("content"):
            continue
        notes.append(
            Note(
                file=f"skill:{skill.name}",
                entry=0,
                content=" ".join(str(item["content"]).split()),
                category=item.get("category") if item.get("category") in CATEGORIES else "owner",
                kind=item.get("kind") if item.get("kind") in NOTE_KINDS else "fact",
                importance=min(max(int(item.get("importance") or 5), 1), 10),
                sensitive=bool(item.get("sensitive")),
            )
        )
    verdict = data.get("verdict") if data.get("verdict") in VERDICTS else "move"
    edits = [Edit(str(e.get("file") or "SKILL.md").removeprefix("./"), str(e["old"]), str(e.get("new") or "")) for e in data.get("edits") or [] if isinstance(e, dict) and e.get("old")]
    return SkillReview(skill, verdict, " ".join(str(data.get("reason") or "").split()), notes, edits if verdict == "move" else [])


async def review(completer: Completer, skill: HermesSkill, persona: str = "") -> SkillReview:
    if skill.origin == "bundled":
        return SkillReview(skill, "drop", "Hermes 內建的技能，沒有被改過。")
    system = SKILL_SYSTEM + (f"\n\nniya 現在的人格設定：\n{persona.strip()}" if persona.strip() else "")
    prompt = prompt_for(skill, staged_files(skill))
    for _ in range(ATTEMPTS):
        try:
            return _parse(skill, parse_json(await completer.complete(system=system, prompt=prompt, task="hermes_skill")))
        except Exception as exc:  # one bad reply should not sink the other skills
            error = f"{type(exc).__name__}: {exc}"
    return SkillReview(skill, "move" if skill.origin == "self" else "drop", "模型沒有回應，請自己判斷。", error=error)


async def review_all(completer: Completer, skills: list[HermesSkill], *, persona: str = "", parallel: int = 8, report: Callable[[str], None] | None = None) -> list[SkillReview]:
    gate = asyncio.Semaphore(max(1, parallel))
    done = 0
    todo = sum(s.origin != "bundled" for s in skills)

    async def one(skill: HermesSkill) -> SkillReview:
        nonlocal done
        if skill.origin == "bundled":
            return await review(completer, skill, persona)
        async with gate:
            result = await review(completer, skill, persona)
        done += 1
        if report:
            report(f"[{done}/{todo}] {skill.name}：{result.verdict}，{len(result.notes)} 條筆記，{len(result.edits)} 處修改" + (f"（{result.error}）" if result.error else ""))
        return result

    return list(await asyncio.gather(*(one(s) for s in skills)))


def save_reviews(results: list[SkillReview], path: Path) -> None:
    """What the model said about each skill, so a few can be read again without redoing the rest."""
    items = [
        {"name": r.skill.name, "verdict": r.verdict, "reason": r.reason, "notes": [asdict(n) for n in r.notes], "edits": [asdict(e) for e in r.edits], "failed_edits": r.failed_edits, "error": r.error}
        for r in results
    ]
    path.write_text(json.dumps(items, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def load_reviews(path: Path, skills: list[HermesSkill]) -> dict[str, SkillReview]:
    by_name = {s.name: s for s in skills}
    out = {}
    for item in json.loads(path.read_text(encoding="utf-8")) if path.exists() else []:
        if skill := by_name.get(item["name"]):
            notes = [Note(**n) for n in item["notes"]]
            edits = [Edit(**e) for e in item["edits"]]
            out[skill.name] = SkillReview(skill, item["verdict"], item["reason"], notes, edits, item["failed_edits"], item["error"])
    return out


def stage(result: SkillReview, staging: Path) -> None:
    """Copy the skill into staging/<name>/ the way niya will hold it, with the edits applied."""
    folder = staging / result.skill.name
    if folder.exists():
        shutil.rmtree(folder)
    files = staged_files(result.skill)
    targets = {f.target for f in files}
    result.failed_edits = sum(e.file not in targets for e in result.edits)
    for f in files:
        target = folder / f.target
        target.parent.mkdir(parents=True, exist_ok=True)
        if f.text is None:
            shutil.copy2(f.source, target)
            continue
        text = f.text
        for edit in (e for e in result.edits if e.file == f.target):
            after, count, _, error = fuzzy_find_and_replace(text, edit.old, edit.new)
            if error or not count:
                result.failed_edits += 1
            else:
                text = after
        target.write_text(text, encoding="utf-8")
        shutil.copymode(f.source, target)


def render_checklist(results: list[SkillReview], staging: Path) -> str:
    lines = [
        "# Hermes 技能搬家清單",
        "",
        "每行開頭的標籤決定去處：[move] 搬到 niya、[drop] 不搬。改標籤就好，底下的說明只是參考。",
        f"搬的時候照 {staging}/<名稱>/ 裡的版本（已經把 Hermes 的工具換成 niya 的），可以直接打開修改，或跟 Hermes 原本的比對。",
        "技能裡記著的事實另外抽出來，接在 hermes-notes.md 最後面，跟筆記一起審。",
    ]
    for origin, title in ORIGINS.items():
        group = sorted((r for r in results if r.skill.origin == origin), key=lambda r: (-r.skill.use_count, r.skill.name))
        if not group:
            continue
        lines += ["", f"## {title}（{len(group)}）", ""]
        for r in group:
            description = r.skill.description if len(r.skill.description) <= 80 else r.skill.description[:79] + "…"
            lines.append(f"- [{r.verdict}] {r.skill.name}：{description}")
            detail = f"{r.skill.path}，用了 {r.skill.use_count} 次" + (f"，最後一次 {r.skill.last_used}" if r.skill.last_used else "")
            if r.edits:
                detail += f"；改了 {len(r.edits) - r.failed_edits} 處" + (f"（{r.failed_edits} 處沒對上，沒改）" if r.failed_edits else "")
            if r.notes:
                detail += f"；抽出 {len(r.notes)} 條筆記"
            lines.append(f"  - {detail}")
            if r.reason:
                lines.append(f"  - {r.reason}" + (f"（{r.error}）" if r.error else ""))
    return "\n".join(lines) + "\n"


def parse_checklist(text: str) -> dict[str, str]:
    return {m.group(2): m.group(1) for line in text.splitlines() if (m := _CHECK_LINE.match(line.strip()))}


def with_skill_notes(review_text: str, results: list[SkillReview]) -> str:
    """The notes review with the skills' facts (re)placed at its end, in its own format, so
    `niya hermes apply` reads them with the rest."""
    lines = [review_text.split(f"\n{NOTES_HEADING}", 1)[0].rstrip()] if review_text.strip() else []
    with_notes = [r for r in results if r.notes]
    if with_notes:
        lines += ["", NOTES_HEADING, "", "每個技能一節，標籤和格式跟上面的筆記一樣。"]
        for r in with_notes:
            lines += ["", f"## skill:{r.skill.name}", "", f"> 來源：{r.skill.path}（{ORIGINS[r.skill.origin]}，{'會搬' if r.verdict == 'move' else '不搬'}）"]
            lines += [note_line(n) for n in r.notes]
    return "\n".join(lines) + "\n"


@dataclass
class ImportStats:
    moved: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)


def import_skills(store: SkillStore, staging: Path, names: list[str]) -> ImportStats:
    """Move the checked skills in from staging; skills niya already has are left alone, so this can run again."""
    stats = ImportStats()
    for name in names:
        folder = staging / name
        existing = store.get(name)
        if existing is not None and existing.status != "proposed":
            stats.existing.append(name)
            continue
        if not (folder / "SKILL.md").is_file():
            stats.refused.append((name, f"{folder} 裡沒有 SKILL.md"))
            continue
        try:
            store.create(name, (folder / "SKILL.md").read_text(encoding="utf-8"), created_by="hermes")
        except SkillError as exc:
            stats.refused.append((name, str(exc)))
            continue
        stats.moved.append(name)
        for path in sorted(p for p in folder.rglob("*") if p.is_file() and p.relative_to(folder) != Path("SKILL.md")):
            relative = str(path.relative_to(folder))
            try:
                store.import_file(name, relative, path.read_bytes())
            except SkillError as exc:
                stats.refused.append((f"{name}/{relative}", str(exc)))
                continue
            shutil.copymode(path, store.root / name / relative)
    return stats
