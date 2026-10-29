"""Skills on disk (SKILL.md with YAML frontmatter, agentskills.io style) plus their lifecycle in the database.

    $NIYA_HOME/skills/<name>/SKILL.md      active or stale
    $NIYA_HOME/skills/.archive/<name>/     archived (never deleted)
    $NIYA_HOME/skills/.proposals/<name>/   drafted during REM, waiting for niya to accept

Every write is scanned with the strict threat rules and refused on a hit; every read is scanned with the
context rules, so a skill that was tampered with on disk shows up as [BLOCKED] instead of instructions.
"""

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from niya.core.clock import Clock
from niya.core.db import Database
from niya.tools.fuzzy_match import format_no_match_hint, fuzzy_find_and_replace
from niya.tools.threat_patterns import first_threat_message, scan_for_threats

VALID_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MAX_CONTENT = 100_000
MAX_DESCRIPTION = 1024
SUBDIRS = ("references", "templates", "scripts", "assets")
STALE_DAYS = 30
ARCHIVE_DAYS = 90
INDEX_DESCRIPTION = 80
DAY = 86400.0
SKILL_DIR = "${SKILL_DIR}"  # a skill's own folder, filled in when it is read (scripts/ can be run from there)


class SkillError(ValueError):
    pass


@dataclass(frozen=True)
class Skill:
    name: str
    status: str
    description: str
    created_by: str
    last_used_at: float | None
    use_count: int


def parse_skill(content: str) -> tuple[dict[str, Any], str]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", content, re.S)
    if not match:
        raise SkillError("SKILL.md 要以 YAML frontmatter 開頭（--- name / description ---）。")
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise SkillError(f"frontmatter 不是合法的 YAML：{exc}") from None
    if not isinstance(meta, dict):
        raise SkillError("frontmatter 要是 key: value 的格式。")
    return meta, match.group(2)


def render_skill(name: str, description: str, body: str) -> str:
    front = yaml.safe_dump({"name": name, "description": description}, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{front}\n---\n\n{body.strip()}\n"


class SkillStore:
    def __init__(self, root: Path, db: Database, clock: Clock) -> None:
        self.root = root
        self.db = db
        self.clock = clock

    # --- paths & rows ------------------------------------------------------------------------------

    def _dir(self, name: str, status: str) -> Path:
        if status == "archived":
            return self.root / ".archive" / name
        if status == "proposed":
            return self.root / ".proposals" / name
        return self.root / name

    def get(self, name: str) -> Skill | None:
        row = self.db.one("SELECT * FROM skills WHERE name = ?", (name,))
        if row is None:
            return None
        return Skill(row["name"], row["status"], row["description"], row["created_by"], row["last_used_at"], row["use_count"])

    def all(self, statuses: tuple[str, ...] = ("active", "stale")) -> list[Skill]:
        marks = ",".join("?" * len(statuses))
        rows = self.db.query(f"SELECT name FROM skills WHERE status IN ({marks}) ORDER BY name", statuses)
        return [skill for r in rows if (skill := self.get(r["name"]))]

    def _event(self, name: str, kind: str, session_id: str | None = None, detail: str | None = None) -> None:
        self.db.execute("INSERT INTO skill_events(name, kind, ts, session_id, detail) VALUES (?, ?, ?, ?, ?)", (name, kind, self.clock.now(), session_id, detail))

    def _require(self, name: str, statuses: tuple[str, ...]) -> Skill:
        skill = self.get(name)
        if skill is None or skill.status not in statuses:
            raise SkillError(f"沒有「{name}」這個技能{'' if skill is None else f'（目前是 {skill.status}）'}。")
        return skill

    # --- validation --------------------------------------------------------------------------------

    def _check(self, name: str, content: str) -> str:
        if not VALID_NAME.match(name):
            raise SkillError("技能名稱只能用小寫英數字、. _ -，最多 64 字，例如 deploy-nginx。")
        if len(content) > MAX_CONTENT:
            raise SkillError(f"SKILL.md 太長了（上限 {MAX_CONTENT:,} 字），把細節拆到 references/ 底下的檔案。")
        meta, body = parse_skill(content)
        if meta.get("name") != name:
            raise SkillError(f"frontmatter 的 name 要和技能名稱一樣（{name}）。")
        description = str(meta.get("description") or "").strip()
        if not description or len(description) > MAX_DESCRIPTION:
            raise SkillError("frontmatter 要有 description（一句話說明什麼時候用，最多 1024 字）。")
        if not body.strip():
            raise SkillError("SKILL.md 除了 frontmatter，還要寫步驟。")
        if threat := first_threat_message(content, "strict"):
            raise SkillError(f"內容像是注入或危險指令，沒有寫入：{threat}")
        return description

    # --- reading -----------------------------------------------------------------------------------

    def view(self, name: str, file_path: str | None = None, *, session_id: str | None = None) -> str:
        skill = self._require(name, ("active", "stale", "proposed", "archived"))
        folder = self._dir(name, skill.status)
        target = folder / "SKILL.md" if not file_path else self._supporting(folder, file_path)
        if not target.is_file():
            raise SkillError(f"技能「{name}」裡沒有 {file_path or 'SKILL.md'}。")
        text = target.read_text(encoding="utf-8", errors="replace")
        if scan_for_threats(text, "context"):
            self._event(name, "blocked", session_id)
            return f"[BLOCKED] 技能「{name}」的 {target.name} 含有可疑內容，沒有載入；請告訴主人。"
        text = text.replace(SKILL_DIR, str(folder))
        if skill.status in ("active", "stale") and not file_path:
            # Loading a skill means niya is about to follow it: that is what keeps a skill alive.
            self.db.execute("UPDATE skills SET last_used_at = ?, use_count = use_count + 1, status = 'active' WHERE name = ?", (self.clock.now(), name))
            self._event(name, "use", session_id)
        if file_path:
            return text
        files = sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file() and p.name != "SKILL.md")
        note = {"proposed": "（這是睡覺時整理出的草稿，還沒收下）\n", "archived": "（這個技能已經封存）\n"}.get(skill.status, "")
        return note + text + (f"\n\n附檔：{', '.join(files)}（用 file_path 讀；都在 {folder} 底下）" if files else "")

    def index(self) -> str:
        """The skills section of the system prompt: active skills only, one line each."""
        lines = []
        for skill in self.all(("active",)):
            description = skill.description if len(skill.description) <= INDEX_DESCRIPTION else skill.description[: INDEX_DESCRIPTION - 1] + "…"
            lines.append(f"- {skill.name}：{description}")
        return "\n".join(lines)

    # --- writing -----------------------------------------------------------------------------------

    def _write(self, folder: Path, content: str) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / "SKILL.md.tmp"
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(folder / "SKILL.md")

    def _supporting(self, folder: Path, file_path: str) -> Path:
        relative = Path(file_path)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] not in SUBDIRS:
            raise SkillError(f"附檔要放在 {'、'.join(SUBDIRS)} 底下，例如 references/notes.md。")
        return folder / relative

    def create(self, name: str, content: str, *, created_by: str = "niya", session_id: str | None = None) -> Skill:
        if self.get(name) is not None and self.get(name).status != "proposed":
            raise SkillError(f"已經有「{name}」這個技能了，要修改請用 patch 或 edit。")
        description = self._check(name, content)
        self._write(self._dir(name, "active"), content)
        now = self.clock.now()
        self.db.execute(
            "INSERT INTO skills(name, status, description, created_by, created_at, updated_at, last_used_at) VALUES (?, 'active', ?, ?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET status = 'active', description = excluded.description, updated_at = excluded.updated_at, last_used_at = excluded.last_used_at",
            (name, description, created_by, now, now, now),
        )
        shutil.rmtree(self._dir(name, "proposed"), ignore_errors=True)
        self._event(name, "create", session_id)
        return self.get(name)

    def edit(self, name: str, content: str, *, session_id: str | None = None) -> Skill:
        self._require(name, ("active", "stale"))
        description = self._check(name, content)
        self._write(self._dir(name, "active"), content)
        self.db.execute("UPDATE skills SET description = ?, updated_at = ?, status = 'active' WHERE name = ?", (description, self.clock.now(), name))
        self._event(name, "edit", session_id)
        return self.get(name)

    def patch(self, name: str, old: str, new: str, *, file_path: str | None = None, replace_all: bool = False, session_id: str | None = None) -> str:
        self._require(name, ("active", "stale"))
        folder = self._dir(name, "active")
        target = folder / "SKILL.md" if not file_path else self._supporting(folder, file_path)
        before = target.read_text(encoding="utf-8")
        after, count, _, error = fuzzy_find_and_replace(before, old, new, replace_all=replace_all)
        if error:
            raise SkillError(error + format_no_match_hint(error, count, old, before))
        if target.name == "SKILL.md":
            description = self._check(name, after)
            self.db.execute("UPDATE skills SET description = ? WHERE name = ?", (description, name))
        elif threat := first_threat_message(after, "strict"):
            raise SkillError(f"內容像是注入或危險指令，沒有寫入：{threat}")
        target.write_text(after, encoding="utf-8")
        self.db.execute("UPDATE skills SET updated_at = ?, status = 'active' WHERE name = ?", (self.clock.now(), name))
        self._event(name, "patch", session_id, file_path)
        return f"已修改技能「{name}」的 {file_path or 'SKILL.md'}，取代 {count} 處。"

    def write_file(self, name: str, file_path: str, content: str, *, session_id: str | None = None) -> str:
        self._require(name, ("active", "stale"))
        if len(content) > MAX_CONTENT:
            raise SkillError(f"附檔太長了（上限 {MAX_CONTENT:,} 字）。")
        if threat := first_threat_message(content, "strict"):
            raise SkillError(f"內容像是注入或危險指令，沒有寫入：{threat}")
        target = self._supporting(self._dir(name, "active"), file_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self._event(name, "write_file", session_id, file_path)
        return f"已寫入技能「{name}」的 {file_path}。"

    def _move(self, name: str, from_status: str, to_status: str) -> None:
        source, target = self._dir(name, from_status), self._dir(name, to_status)
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.exists():
            shutil.move(str(source), str(target))
        self.db.execute("UPDATE skills SET status = ?, updated_at = ? WHERE name = ?", (to_status, self.clock.now(), name))

    def archive(self, name: str, *, reason: str = "manual", session_id: str | None = None) -> Skill:
        skill = self._require(name, ("active", "stale", "proposed"))
        self._move(name, skill.status, "archived")
        self._event(name, "archive", session_id, reason)
        return self.get(name)

    def restore(self, name: str, *, session_id: str | None = None) -> Skill:
        self._require(name, ("archived",))
        self._move(name, "archived", "active")
        self.db.execute("UPDATE skills SET last_used_at = ? WHERE name = ?", (self.clock.now(), name))
        self._event(name, "restore", session_id)
        return self.get(name)

    def accept(self, name: str, *, session_id: str | None = None) -> Skill:
        self._require(name, ("proposed",))
        content = (self._dir(name, "proposed") / "SKILL.md").read_text(encoding="utf-8")
        return self.create(name, content, created_by="rem", session_id=session_id)

    def propose(self, name: str, description: str, body: str) -> Skill:
        """A draft from REM: kept aside until niya decides to accept it."""
        existing = self.get(name)
        if existing is not None and existing.status != "proposed":
            raise SkillError(f"已經有「{name}」這個技能了。")
        content = render_skill(name, description, body)
        self._check(name, content)
        self._write(self._dir(name, "proposed"), content)
        now = self.clock.now()
        self.db.execute(
            "INSERT INTO skills(name, status, description, created_by, created_at, updated_at) VALUES (?, 'proposed', ?, 'rem', ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET description = excluded.description, updated_at = excluded.updated_at",
            (name, description.strip(), now, now),
        )
        self._event(name, "proposed")
        return self.get(name)

    # --- lifecycle ---------------------------------------------------------------------------------

    def lifecycle(self) -> tuple[list[str], list[str]]:
        """Unused for 30 days: stale (dropped from the prompt index). Unused for 90 days: archived."""
        now = self.clock.now()
        staled, archived = [], []
        for row in self.db.query("SELECT name, status, COALESCE(last_used_at, created_at) AS used FROM skills WHERE status IN ('active', 'stale')"):
            idle = (now - row["used"]) / DAY
            if idle >= ARCHIVE_DAYS:
                self.archive(row["name"], reason="unused")
                archived.append(row["name"])
            elif idle >= STALE_DAYS and row["status"] == "active":
                self.db.execute("UPDATE skills SET status = 'stale' WHERE name = ?", (row["name"],))
                self._event(row["name"], "stale")
                staled.append(row["name"])
        return staled, archived
