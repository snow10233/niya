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
