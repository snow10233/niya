import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from helpers import TZ, ts

from niya.app.alarms import Alarms
from niya.core.clock import FakeClock
from niya.core.db import Database
from niya.hermes.cron import apply_plans, cron_to_calendar, interval_cron, parse_checklist, plan, read_jobs, render_checklist, unit_files

ORIGIN = {"platform": "discord", "chat_id": "300", "thread_id": "300"}


def job(id, name, **extra):
    base = {"id": id, "name": name, "no_agent": True, "schedule": {"kind": "interval", "minutes": 5}, "schedule_display": "every 5m",
            "script": "check.py", "deliver": "discord:100", "failure_deliver": None, "origin": ORIGIN, "enabled": True, "state": "scheduled"}
    return base | extra


JOBS = {"jobs": [
    job("a1", "snowbox 健康監控", interpreter="/root/.hermes/scripts/.venv/bin/python"),
    job("b2", "skill 同步", script="sync.sh", schedule={"kind": "cron", "expr": "0 * * * *"}, schedule_display="0 * * * *", deliver="local", failure_deliver="discord:100"),
    job("c3", "PH 線 100% 跑完", script="line.py", schedule={"kind": "cron", "expr": "0 1 * * *"}, deliver="origin", failure_deliver="local", workdir="/opt/snowcast"),
    job("d4", "待審整理", no_agent=False, script="", schedule={"kind": "interval", "minutes": 4320}, prompt="讀 snowcast 的待審一節，照做並回報。",
        skills=["snowcast"], deliver="discord:200:250", next_run_at="2026-10-31T21:07:06+08:00"),
    job("e5", "夢", no_agent=False, script="dream.py", schedule={"kind": "cron", "expr": "30 4 * * *"}, prompt="整併記憶。", skills=["memory-dreaming"]),
    job("f6", "IDrive 到期提醒", no_agent=False, script="", schedule={"kind": "once", "run_at": "2026-11-22T20:00:00+08:00"}, prompt="提醒主人 IDrive 快到期。"),
    job("g7", "舊排程", enabled=False),
    job("h8", "每 7 分鐘", no_agent=False, script="", schedule={"kind": "interval", "minutes": 7}, prompt="x"),
]}


class CronTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.plans = {p.job.id: p for p in (plan(j, TZ, dropped_skills=frozenset({"memory-dreaming"})) for j in read_jobs(JOBS))}

    def test_where_results_go(self):
        jobs = {j.id: j for j in read_jobs(JOBS)}
        self.assertEqual((jobs["a1"].deliver, jobs["a1"].on_failure), ("100", "100"))
        self.assertEqual((jobs["b2"].deliver, jobs["b2"].on_failure), (None, "100"))
        self.assertEqual((jobs["c3"].deliver, jobs["c3"].on_failure), ("300", None))  # origin is the thread the job was made in
        self.assertEqual(jobs["d4"].deliver, "250")
        self.assertFalse(jobs["g7"].enabled)

    def test_schedules(self):
        self.assertEqual(cron_to_calendar("0 1 * * *"), "*-*-* 01:00:00")
        self.assertEqual(cron_to_calendar("0 7 * * 1"), "Mon *-*-* 07:00:00")
        self.assertEqual(cron_to_calendar("*/15 9-17 * * 1-5"), "Mon..Fri *-*-* 09..17:00/15:00")
        self.assertEqual(cron_to_calendar("30 4,5,6 1 */2 *"), "*-01/2-01 04,05,06:30:00")
        for bad in ("0 1 1 * 1", "0 1 * *", "0 L * * *"):
            with self.subTest(expr=bad), self.assertRaises(ValueError):
                cron_to_calendar(bad)
        at = datetime.fromtimestamp(ts("2026-10-31 21:07"), TZ)
        self.assertEqual([interval_cron(m, at) for m in (4320, 1440, 120, 15, 7)], ["7 21 */3 * *", "7 21 * * *", "7 */2 * * *", "*/15 * * * *", None])

    def test_each_job_gets_a_place(self):
        p = self.plans
        self.assertEqual({k: v.choice for k, v in p.items()}, {"a1": "timer", "b2": "timer", "c3": "timer", "d4": "alarm", "e5": "drop", "f6": "alarm", "g7": "drop", "h8": "drop"})
        self.assertEqual(p["a1"].command, ["/root/.hermes/scripts/.venv/bin/python", "/root/.hermes/scripts/check.py"])
        self.assertEqual(p["b2"].command, ["/bin/bash", "/root/.hermes/scripts/sync.sh"])
        self.assertEqual(p["c3"].command, ["/usr/bin/python3", "/root/.hermes/scripts/line.py"])
        self.assertIn("自己的 Python", p["c3"].notes[0])
        self.assertEqual(p["a1"].timer[:2], ["OnBootSec=2min", "OnUnitActiveSec=5min"])
        self.assertEqual(p["c3"].timer[0], "OnCalendar=*-*-* 01:00:00 Asia/Taipei")
        review = p["d4"].alarm
        self.assertEqual((review["cron"], review["purpose"], review["at"]), ("7 21 */3 * *", "task", ts("2026-10-31 21:07") + 6 - 60))
        self.assertIn("照技能 snowcast 做。", review["note"])
        self.assertIn("發到頻道 250", review["note"])
        self.assertEqual((p["f6"].alarm["purpose"], p["f6"].alarm["at"]), ("remind", ts("2026-11-22 20:00")))
        self.assertIn("用到的技能 memory-dreaming 不搬。", p["e5"].notes)
        self.assertIn("沒辦法寫成鬧鐘", p["h8"].notes[0])

    def test_checklist_round_trip(self):
        text = render_checklist(list(self.plans.values()), self.dir / "hermes-cron", TZ)
        self.assertIn("- [alarm] f6 IDrive 到期提醒：", text)
        self.assertIn("  - 鬧鐘：一次，2026-11-22 20:00（purpose=remind）", text)
        self.assertIn("  - 有輸出：不發；失敗：discord:100", text)
        self.assertEqual(parse_checklist(text.replace("[timer] b2", "[drop] b2"))["b2"], "drop")
        self.assertEqual(len(parse_checklist(text)), 8)

    def test_units_quote_what_systemd_would_read_as_its_own(self):
        units = unit_files(self.plans["c3"])
        service = units["niya-hermes-c3.service"]
        self.assertIn('ExecStart=/opt/niya/.venv/bin/niya run-job --name "PH 線 100%% 跑完" --deliver discord:300 --on-failure none', service)
        self.assertIn("--cwd /opt/snowcast -- /usr/bin/python3 /root/.hermes/scripts/line.py", service)
        self.assertIn("Description=Hermes 搬來的排程：PH 線 100%% 跑完", service)
        self.assertIn("EnvironmentFile=-/root/.hermes/.env\nEnvironmentFile=/etc/niya/niya.env", service)  # niya's file wins on clashes
        self.assertIn("[Timer]\nOnCalendar=*-*-* 01:00:00 Asia/Taipei\nPersistent=true", units["niya-hermes-c3.timer"])

    def test_apply_writes_timers_and_alarms_once(self):
        db = Database(":memory:")
        alarms = Alarms(db, FakeClock(ts("2026-10-30 16:00")), TZ)
        units = self.dir / "hermes-cron"
        units.mkdir()
        (units / "niya-hermes-old.timer").write_text("stale")
        choices = {k: v.choice for k, v in self.plans.items()} | {"b2": "drop"}
        stats = apply_plans(list(self.plans.values()), choices, alarms, units)
        self.assertEqual((stats.timers, stats.alarms), (["snowbox 健康監控", "PH 線 100% 跑完"], ["待審整理", "IDrive 到期提醒"]))
        self.assertEqual(sorted(p.name for p in units.iterdir()), ["install.sh", "niya-hermes-a1.service", "niya-hermes-a1.timer", "niya-hermes-c3.service", "niya-hermes-c3.timer"])
        install = (units / "install.sh").read_text()
        self.assertTrue(os.access(units / "install.sh", os.X_OK))
        self.assertIn("systemctl enable --now niya-hermes-a1.timer niya-hermes-c3.timer", install)
        self.assertIn("Hermes 還在跑", install)
        made = {a.title: a for a in alarms.active()}
        self.assertEqual((made["待審整理"].cron_expr, made["待審整理"].next_fire_at), ("7 21 */3 * *", ts("2026-10-31 21:07")))
        self.assertEqual(made["IDrive 到期提醒"].next_fire_at, ts("2026-11-22 20:00"))
        again = apply_plans(list(self.plans.values()), choices, alarms, units)
        self.assertEqual((again.alarms, again.existing), ([], ["待審整理", "IDrive 到期提醒"]))
        self.assertEqual(len(alarms.active()), 2)


if __name__ == "__main__":
    unittest.main()
