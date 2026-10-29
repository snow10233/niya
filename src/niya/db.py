import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

MIGRATIONS: list[str] = [
    """
    CREATE TABLE state_meta(key TEXT PRIMARY KEY, value TEXT);

    CREATE TABLE sessions(
      id TEXT PRIMARY KEY,
      kind TEXT NOT NULL DEFAULT 'main',
      parent_id TEXT,
      started_at REAL NOT NULL,
      ended_at REAL,
      end_reason TEXT,
      system_prompt TEXT NOT NULL
    );

    CREATE TABLE turns(
      id TEXT PRIMARY KEY,
      session_id TEXT NOT NULL REFERENCES sessions(id),
      source TEXT NOT NULL,
      started_at REAL NOT NULL,
      ended_at REAL,
      gap_s REAL,
      recall_block TEXT
    );
    CREATE INDEX idx_turns_session ON turns(session_id, started_at);

    CREATE TABLE messages(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id TEXT NOT NULL REFERENCES sessions(id),
      turn_id TEXT,
      role TEXT NOT NULL,
      speaker TEXT,
      content TEXT,
      api_content TEXT,
      tool_calls TEXT,
      tool_call_id TEXT,
      tool_name TEXT,
      extra TEXT,
      ts REAL NOT NULL
    );
    CREATE INDEX idx_messages_session ON messages(session_id, id);
    CREATE INDEX idx_messages_turn ON messages(turn_id);

    CREATE TABLE llm_calls(
      id INTEGER PRIMARY KEY,
      ts REAL NOT NULL,
      task TEXT,
      model TEXT,
      prompt_tokens INTEGER,
      completion_tokens INTEGER,
      cached_tokens INTEGER,
      cost_usd REAL,
      latency_ms INTEGER,
      ok INTEGER NOT NULL,
      error TEXT
    );
    """,
    """
    ALTER TABLE sessions ADD COLUMN tainted_at REAL;
    ALTER TABLE sessions ADD COLUMN taint_source TEXT;
    ALTER TABLE sessions ADD COLUMN taint_turn_id TEXT;

    CREATE TABLE egress_known_hosts(
      host TEXT PRIMARY KEY,
      source TEXT NOT NULL,
      created_at REAL NOT NULL,
      last_seen REAL NOT NULL
    );

    CREATE TABLE egress_pending(
      id INTEGER PRIMARY KEY,
      session_id TEXT NOT NULL,
      turn_id TEXT,
      command TEXT NOT NULL,
      hosts TEXT NOT NULL,
      taint_source TEXT,
      created_at REAL NOT NULL,
      expires_at REAL NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending',
      decided_at REAL,
      notified_expired INTEGER NOT NULL DEFAULT 0,
      used_at REAL
    );
    CREATE INDEX idx_egress_pending_session ON egress_pending(session_id, status);

    CREATE TABLE egress_log(
      id INTEGER PRIMARY KEY,
      ts REAL NOT NULL,
      session_id TEXT,
      turn_id TEXT,
      command TEXT NOT NULL,
      program TEXT,
      hosts TEXT,
      local_data INTEGER,
      tainted INTEGER,
      suspicious INTEGER,
      outcome TEXT
    );
    """,
    """
    -- Which engine ran the session (openrouter / claude) and, for Claude Code, its own session ID to resume.
    ALTER TABLE sessions ADD COLUMN engine TEXT;
    ALTER TABLE sessions ADD COLUMN engine_session_id TEXT;
    """,
    """
    -- Daytime segmentation: the units that NREM later cuts into events.
    ALTER TABLE turns ADD COLUMN sim_prev REAL;
    ALTER TABLE turns ADD COLUMN segment_id INTEGER;
    CREATE TABLE segments(
      id INTEGER PRIMARY KEY,
      session_id TEXT NOT NULL REFERENCES sessions(id),
      kind TEXT NOT NULL DEFAULT 'chat',
      started_at REAL NOT NULL,
      ended_at REAL,
      status TEXT NOT NULL DEFAULT 'open',
      closed_reason TEXT,
      consolidated_at REAL,
      sleep_run_id INTEGER
    );
    CREATE INDEX idx_segments_status ON segments(status, session_id);
    CREATE INDEX idx_turns_segment ON turns(segment_id);

    -- Full-text search over what was said (tool output is left out); trigram works for Chinese and English alike.
    CREATE VIRTUAL TABLE messages_tri USING fts5(content, content='messages', content_rowid='id', tokenize='trigram');
    INSERT INTO messages_tri(rowid, content) SELECT id, content FROM messages WHERE role <> 'tool' AND content IS NOT NULL;
    CREATE TRIGGER messages_tri_ai AFTER INSERT ON messages WHEN new.role <> 'tool' AND new.content IS NOT NULL BEGIN
      INSERT INTO messages_tri(rowid, content) VALUES (new.id, new.content);
    END;
    CREATE TRIGGER messages_tri_ad AFTER DELETE ON messages WHEN old.role <> 'tool' AND old.content IS NOT NULL BEGIN
      INSERT INTO messages_tri(messages_tri, rowid, content) VALUES ('delete', old.id, old.content);
    END;
    CREATE TRIGGER messages_tri_au AFTER UPDATE OF content, role ON messages BEGIN
      INSERT INTO messages_tri(messages_tri, rowid, content) SELECT 'delete', old.id, old.content WHERE old.role <> 'tool' AND old.content IS NOT NULL;
      INSERT INTO messages_tri(rowid, content) SELECT new.id, new.content WHERE new.role <> 'tool' AND new.content IS NOT NULL;
    END;

    -- Everything that wants niya's attention waits here, so nothing is lost across restarts or sleep.
    CREATE TABLE inbox(
      id INTEGER PRIMARY KEY,
      source TEXT NOT NULL,
      priority INTEGER NOT NULL DEFAULT 0,
      content TEXT NOT NULL,
      meta TEXT,
      received_at REAL NOT NULL,
      taken_at REAL
    );
    CREATE INDEX idx_inbox_pending ON inbox(taken_at, priority, received_at);
    """,
    """
    -- Alarms niya sets for itself: remind the owner, or wake up and do something.
    CREATE TABLE alarms(
      id INTEGER PRIMARY KEY,
      title TEXT NOT NULL,
      purpose TEXT NOT NULL DEFAULT 'remind',
      note TEXT,
      cron_expr TEXT,
      next_fire_at REAL,
      wake_from_sleep INTEGER NOT NULL DEFAULT 1,
      late_grace_s REAL NOT NULL,
      max_fires INTEGER,
      memory_id INTEGER,
      created_by TEXT NOT NULL DEFAULT 'niya',
      created_at REAL NOT NULL,
      session_id TEXT,
      status TEXT NOT NULL DEFAULT 'scheduled',
      fire_count INTEGER NOT NULL DEFAULT 0,
      last_fired_at REAL
    );
    CREATE INDEX idx_alarms_due ON alarms(status, next_fire_at);
    """,
]


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            file = Path(self.path)
            file.parent.mkdir(parents=True, exist_ok=True)
            if not file.exists():
                file.touch(mode=0o600)
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        for pragma in ("journal_mode=WAL", "synchronous=NORMAL", "foreign_keys=ON", "busy_timeout=5000"):
            self.conn.execute(f"PRAGMA {pragma}")
        self._depth = 0
        self.migrate()

    def migrate(self) -> None:
        self.conn.execute("CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL)")
        current = self.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
        for version, sql in enumerate(MIGRATIONS, start=1):
            if version <= current:
                continue
            # executescript() commits any open transaction first, so the script carries its own BEGIN/COMMIT.
            try:
                self.conn.executescript(f"BEGIN;\n{sql}\nINSERT INTO schema_version(version) VALUES ({version});\nCOMMIT;")
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise

    @property
    def schema_version(self) -> int:
        return self.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0

    @contextmanager
    def transaction(self) -> Iterator["Database"]:
        savepoint = f"sp{self._depth}"
        self.conn.execute("BEGIN IMMEDIATE" if self._depth == 0 else f"SAVEPOINT {savepoint}")
        self._depth += 1
        try:
            yield self
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("ROLLBACK")
            else:
                self.conn.execute(f"ROLLBACK TO {savepoint}")
                self.conn.execute(f"RELEASE {savepoint}")
            raise
        self._depth -= 1
        self.conn.execute("COMMIT" if self._depth == 0 else f"RELEASE {savepoint}")

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM state_meta WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO state_meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def close(self) -> None:
        self.conn.close()
