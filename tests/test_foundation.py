import os
import stat
import tempfile
import unittest
from pathlib import Path

from helpers import TZ, ts

from niya.agent.context import Conversation
from niya.agent.prompts import DEFAULT_PERSONA, build_system_prompt, load_persona
from niya.clock import FakeClock
from niya.config import load_settings
from niya.db import Database
from niya.doctor import sqlite_has_trigram


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_file_is_private_and_migrations_are_idempotent(self):
        path = self.dir / "sub" / "niya.db"
        db = Database(path)
        version = db.schema_version
        db.close()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        again = Database(path)
        self.assertEqual(again.schema_version, version)
        self.assertEqual(again.one("SELECT COUNT(*) AS n FROM schema_version")["n"], version)

    def test_transaction_rollback_and_savepoint(self):
        db = Database(":memory:")
        with self.assertRaises(RuntimeError):
            with db.transaction():
                db.set_meta("a", "1")
                raise RuntimeError
        self.assertIsNone(db.get_meta("a"))

        with db.transaction():
            db.set_meta("outer", "1")
            with self.assertRaises(RuntimeError):
                with db.transaction():
                    db.set_meta("inner", "1")
                    raise RuntimeError
        self.assertEqual(db.get_meta("outer"), "1")
        self.assertIsNone(db.get_meta("inner"))
        self.assertFalse(db.conn.in_transaction)

    def test_sqlite_supports_trigram(self):
        self.assertTrue(sqlite_has_trigram())


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_toml_env_override_and_secret_popping(self):
        config = self.dir / "niya.toml"
        config.write_text('[agent]\nmodel = "a/b"\nmax_iterations = 7\n[paths]\nhome = "/tmp/niya-home"\n', encoding="utf-8")
        env = {"OPENROUTER_API_KEY": " sk-test ", "NIYA_MODEL": "c/d", "HOME": "/root"}
        settings = load_settings(config, environ=env, pop_secrets=True)
        self.assertEqual(settings.model, "c/d")
        self.assertEqual(settings.max_iterations, 7)
        self.assertEqual(settings.home, Path("/tmp/niya-home"))
        self.assertEqual(settings.api_key, "sk-test")
        self.assertNotIn("OPENROUTER_API_KEY", env)
        self.assertNotIn("sk-test", repr(settings))

    def test_niya_home_env_wins_and_bad_timezone_rejected(self):
        settings = load_settings(None, environ={"NIYA_HOME": str(self.dir)})
        self.assertEqual(settings.db_path, self.dir / "niya.db")
        with self.assertRaisesRegex(ValueError, "時區"):
            load_settings(None, environ={"NIYA_HOME": str(self.dir), "NIYA_TIMEZONE": "Mars/Base"})


class PromptTests(unittest.TestCase):
    def test_deterministic_and_feature_gated(self):
        kwargs = dict(persona="P", timezone="Asia/Taipei", channel="Discord", run_as="root")
        base = build_system_prompt(**kwargs)
        self.assertEqual(base, build_system_prompt(**kwargs))
        self.assertIn("# 安全規則", base)
        self.assertIn("以 root 身分執行", base)
        self.assertNotIn("deep_recall", base)
        full = build_system_prompt(**kwargs, features={"memory", "alarm", "sleep"}, skills_index="- s：x", core_block="# 記憶\n- m")
        self.assertIn("deep_recall", full)
        self.assertIn("# 技能\n- s：x", full)
        self.assertTrue(full.endswith("# 記憶\n- m"))

    def test_persona_fallback(self):
        self.assertEqual(load_persona(None), DEFAULT_PERSONA)
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write("我是測試人格\n")
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(load_persona(Path(f.name)), "我是測試人格")


class SessionTests(unittest.TestCase):
    def test_open_current_reuses_open_session_and_keeps_prompt(self):
        db = Database(":memory:")
        clock = FakeClock(ts("2026-10-29 06:00"))
        prompts = iter(["first", "second"])
        conv = Conversation.open_current(db, now=clock.now(), tz=TZ, system_prompt=lambda: next(prompts))
        self.assertTrue(conv.session_id.startswith("main-20261029-060000-"))
        same = Conversation.open_current(db, now=clock.now(), tz=TZ, system_prompt=lambda: next(prompts))
        self.assertEqual(same.session_id, conv.session_id)
        self.assertEqual(same.system_prompt, "first")
        conv.end(now=clock.now(), reason="manual")
        fresh = Conversation.open_current(db, now=clock.now(), tz=TZ, system_prompt=lambda: next(prompts))
        self.assertNotEqual(fresh.session_id, conv.session_id)
        self.assertEqual(fresh.system_prompt, "second")


if __name__ == "__main__":
    unittest.main()
