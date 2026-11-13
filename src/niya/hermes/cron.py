"""Hermes' cron jobs (cron/jobs.json), sorted onto niya.

A script-only job becomes a systemd timer that runs `niya run-job` (same rules as Hermes' no_agent jobs), a
job that needs the agent becomes one of niya's alarms, and Hermes' own machinery stays behind. The owner
ticks a checklist first; apply writes the alarms into niya.db and the timer units into a folder with an
install script for switch-over day. Hermes has to be stopped first, or every job would run twice.
"""

import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from niya.app.alarms import Alarms

HERMES_HOME = "/root/.hermes"
NIYA_BIN = "/opt/niya/.venv/bin/niya"  # as installed by deploy/niya.service
NIYA_ENV = "/etc/niya/niya.env"
NIYA_CONFIG = "/etc/niya/niya.toml"
SCRIPT_TIMEOUT_S = 3600
UNIT_PREFIX = "niya-hermes-"
CHOICES = ("timer", "alarm", "drop")
_CHECK_LINE = re.compile(r"^- \[(timer|alarm|drop)\] ([0-9A-Za-z_-]+) ")
_DOW = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class HermesJob:
    id: str
    name: str
    agent: bool
    schedule: dict[str, Any]
    display: str
    script: str = ""
    interpreter: str = ""
    workdir: str = ""
    deliver: str | None = None  # Discord channel id
    on_failure: str | None = None
    prompt: str = ""
    skills: tuple[str, ...] = ()
    enabled: bool = True
    next_run_at: str = ""


@dataclass
class Plan:
    """What a job becomes on niya, and what the owner should know about it."""

    job: HermesJob
    choice: str
    command: list[str] = field(default_factory=list)
    timer: list[str] = field(default_factory=list)
    alarm: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _target(value: str | None, origin: dict[str, Any]) -> str | None:
    value = (value or "local").strip()
    if value == "origin":
        return str(origin["chat_id"]) if origin.get("platform") == "discord" and origin.get("chat_id") else None
    if value.startswith("discord:"):
        return value.split(":")[-1] or None  # discord:<chat>:<thread> posts into the thread
    return None  # local, or a platform niya does not use


def read_jobs(data: dict[str, Any]) -> list[HermesJob]:
    jobs = []
    for raw in data.get("jobs") or []:
        origin = raw.get("origin") or {}
        deliver = _target(raw.get("deliver"), origin)
        failure = raw.get("failure_deliver")
        jobs.append(
            HermesJob(
                id=str(raw["id"]),
                name=str(raw.get("name") or raw["id"]),
                agent=not raw.get("no_agent"),
                schedule=dict(raw.get("schedule") or {}),
                display=str(raw.get("schedule_display") or ""),
                script=str(raw.get("script") or ""),
                interpreter=str(raw.get("interpreter") or ""),
                workdir=str(raw.get("workdir") or ""),
                deliver=deliver,
                on_failure=deliver if failure is None else _target(failure, origin),
                prompt=str(raw.get("prompt") or "").strip(),
                skills=tuple(raw.get("skills") or ()),
                enabled=bool(raw.get("enabled", True)) and raw.get("state") != "paused",
                next_run_at=str(raw.get("next_run_at") or ""),
            )
        )
    return jobs


# --- schedules ----------------------------------------------------------------------------------


def _calendar_field(value: str, *, start: int, names: tuple[str, ...] = ()) -> str:
    def one(part: str) -> str:
        if re.fullmatch(r"\d+", part):
            return names[int(part)] if names else part.zfill(2)
        if match := re.fullmatch(r"(\d+)-(\d+)", part):
            a, b = (int(g) for g in match.groups())
            return f"{names[a]}..{names[b]}" if names else f"{a:02d}..{b:02d}"
        raise ValueError(part)

    if value == "*":
        return "*"
    if match := re.fullmatch(r"(\*|\d+)/(\d+)", value):
        if names:
            raise ValueError(value)
        first = start if match.group(1) == "*" else int(match.group(1))
        return f"{first:02d}/{int(match.group(2))}"
    return ",".join(one(part) for part in value.split(","))


def cron_to_calendar(expr: str) -> str:
    """A five-field cron expression as a systemd OnCalendar (without the time zone)."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron 要五個欄位：{expr}")
    minute, hour, dom, month, dow = fields
    if dom != "*" and dow != "*":
        raise ValueError(f"cron 同時限定日期和星期幾，systemd 沒辦法照樣表示：{expr}")
    try:
        day = _calendar_field(dow, start=0, names=_DOW) if dow != "*" else ""
        date = f"*-{_calendar_field(month, start=1)}-{_calendar_field(dom, start=1)}"
        clock = f"{_calendar_field(hour, start=0)}:{_calendar_field(minute, start=0)}:00"
    except (ValueError, IndexError):
        raise ValueError(f"看不懂的 cron：{expr}") from None
    return f"{day + ' ' if day else ''}{date} {clock}"


def timer_lines(schedule: dict[str, Any], tz: ZoneInfo) -> list[str]:
    kind = schedule.get("kind")
    if kind == "interval":
        # Hermes counts an interval from the last run; so does OnUnitActiveSec.
        return ["OnBootSec=2min", f"OnUnitActiveSec={int(schedule['minutes'])}min", "AccuracySec=10s"]
    if kind == "cron":
        return [f"OnCalendar={cron_to_calendar(schedule['expr'])} {tz.key}", "Persistent=true", "AccuracySec=10s"]
    if kind == "once":
        at = datetime.fromisoformat(schedule["run_at"]).astimezone(tz)
        return [f"OnCalendar={at:%Y-%m-%d %H:%M:%S} {tz.key}", "Persistent=true", "AccuracySec=10s"]
    raise ValueError(f"不認得的排程種類：{kind}")


def interval_cron(minutes: int, anchor: datetime) -> str | None:
    """An interval as a cron rule for niya's alarms, keeping Hermes' phase where cron can."""
    if minutes % 1440 == 0:
        days = minutes // 1440
        return f"{anchor.minute} {anchor.hour} {'*/' + str(days) if days > 1 else '*'} * *"
    if minutes % 60 == 0 and 24 % (minutes // 60) == 0:
        return f"{anchor.minute} */{minutes // 60} * * *"
    if minutes < 60 and 60 % minutes == 0:
        return f"*/{minutes} * * * *"
    return None


# --- plans --------------------------------------------------------------------------------------


def script_command(job: HermesJob, hermes_home: str) -> tuple[list[str], list[str]]:
    path = job.script if job.script.startswith("/") else f"{hermes_home}/scripts/{job.script}"
    if job.interpreter:
        return [job.interpreter, path], []
    if path.endswith((".sh", ".bash")):
        return ["/bin/bash", path], []
    return ["/usr/bin/python3", path], ["Hermes 是用它自己的 Python 跑這支腳本，搬過來改用系統的 python3，要確認它需要的套件都裝了（或改用專案的 .venv）。"]


def plan(job: HermesJob, tz: ZoneInfo, *, dropped_skills: frozenset[str] = frozenset(), hermes_home: str = HERMES_HOME) -> Plan:
    if not job.enabled:
        return Plan(job, "drop", notes=["Hermes 上已經停用。"])
    if not job.agent:
        command, notes = script_command(job, hermes_home)
        try:
            timer = timer_lines(job.schedule, tz)
        except (ValueError, KeyError) as exc:
            return Plan(job, "drop", command, notes=[f"排程沒辦法換成 systemd timer：{exc}"])
        if job.deliver is None:
            notes.append("Hermes 只把結果存在本機，這裡也只留在 journal。")
        return Plan(job, "timer", command, timer, notes=notes)
    notes = []
    if skipped := sorted(set(job.skills) & dropped_skills):
        notes.append(f"用到的技能 {'、'.join(skipped)} 不搬。")
    kind = job.schedule.get("kind")
    alarm: dict[str, Any] = {"title": job.name, "purpose": "task"}
    if kind == "once":
        alarm.update(purpose="remind", at=datetime.fromisoformat(job.schedule["run_at"]).timestamp())
    elif kind == "cron":
        alarm["cron"] = job.schedule["expr"]
    elif kind == "interval":
        anchor = datetime.fromisoformat(job.next_run_at).astimezone(tz) if job.next_run_at else datetime.now(tz)
        rule = interval_cron(int(job.schedule["minutes"]), anchor)
        if rule is None:
            return Plan(job, "drop", notes=[*notes, f"每 {job.schedule['minutes']} 分鐘沒辦法寫成鬧鐘的 cron 規則。"])
        if "*/" in rule.split()[2]:
            notes.append(f"每 {int(job.schedule['minutes']) // 1440} 天改成 cron `{rule}`：月底會差一兩天（例如 31 號跑完，隔天 1 號又跑）。")
        alarm.update(cron=rule, at=anchor.timestamp() - 60)
    else:
        return Plan(job, "drop", notes=[*notes, f"不認得的排程種類：{kind}"])
    note = job.prompt
    if job.script:
        note += f"\n\n（Hermes 會先跑 {job.script}，把它的輸出接在這段前面；這裡要自己先跑。）"
        notes.append(f"Hermes 會先跑 {job.script} 再叫 agent，鬧鐘沒有這一步。")
    if job.skills:
        note += f"\n\n照技能 {'、'.join(job.skills)} 做。"
    if job.deliver:
        note += f"\n\n做完用 discord 工具把結果發到頻道 {job.deliver}。"
    alarm["note"] = note.strip()
    return Plan(job, "drop" if skipped else "alarm", alarm=alarm, notes=notes)


# --- checklist ----------------------------------------------------------------------------------


def _when(job: HermesJob) -> str:
    return job.display or str(job.schedule)


def render_checklist(plans: list[Plan], units_dir: Path, tz: ZoneInfo) -> str:
    lines = [
        "# Hermes 排程搬家清單",
        "",
        "每行開頭的標籤決定去處，改標籤就好，底下的說明只是參考：",
        "- [timer]：VPS 上的 systemd timer，用 `niya run-job` 只跑腳本、不叫醒 niya（Hermes 的 no_agent 排程）。",
        "- [alarm]：niya 的鬧鐘，時間到了叫醒 niya 去做。",
        "- [drop]：不搬。",
        f"apply 時 [alarm] 直接寫進 niya.db；[timer] 的設定檔寫到 {units_dir}/，切換那天在 VPS 上先停掉 Hermes，再執行裡面的 install.sh。",
    ]
    for title, agent in (("只跑腳本的", False), ("要 agent 做的", True)):
        group = [p for p in plans if p.job.agent == agent]
        if not group:
            continue
        lines += ["", f"## {title}（{len(group)}）", ""]
        for p in group:
            job = p.job
            lines.append(f"- [{p.choice}] {job.id} {job.name}：{_when(job)}")
            if not job.agent:
                lines.append(f"  - 執行：`{' '.join(p.command)}`" + (f"（在 {job.workdir}）" if job.workdir else ""))
                where = f"discord:{job.deliver}" if job.deliver else "不發"
                failure = f"discord:{job.on_failure}" if job.on_failure else "不發"
                lines.append(f"  - 有輸出：{where}；失敗：{failure}")
            else:
                if p.alarm:
                    rule = f"每 `{p.alarm['cron']}`" if p.alarm.get("cron") else f"一次，{datetime.fromtimestamp(p.alarm['at'], tz):%Y-%m-%d %H:%M}"
                    lines.append(f"  - 鬧鐘：{rule}（purpose={p.alarm['purpose']}）")
                prompt = " ".join(job.prompt.split())
                lines.append(f"  - 要做的事：{prompt[:160] + '…' if len(prompt) > 160 else prompt}")
            lines += [f"  - {note}" for note in p.notes]
    return "\n".join(lines) + "\n"


def parse_checklist(text: str) -> dict[str, str]:
    return {m.group(2): m.group(1) for line in text.splitlines() if (m := _CHECK_LINE.match(line.strip()))}


# --- apply --------------------------------------------------------------------------------------


def _systemd_arg(arg: str) -> str:
    """One ExecStart argument: % and $ are systemd's own, so they are doubled."""
    arg = arg.replace("%", "%%").replace("$", "$$")
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", arg):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def unit_files(p: Plan, *, hermes_home: str = HERMES_HOME) -> dict[str, str]:
    job = p.job
    name = f"{UNIT_PREFIX}{job.id}"
    description = f"Hermes 搬來的排程：{job.name}".replace("%", "%%")
    command = [NIYA_BIN, "run-job", "--name", job.name, "--deliver", f"discord:{job.deliver}" if job.deliver else "none"]
    command += ["--on-failure", f"discord:{job.on_failure}" if job.on_failure else "none", "--timeout", str(SCRIPT_TIMEOUT_S)]
    if job.workdir:
        command += ["--cwd", job.workdir]
    command += ["--", *p.command]
    service = "\n".join([
        "[Unit]",
        f"Description={description}",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=oneshot",
        "User=root",
        f"EnvironmentFile=-{hermes_home}/.env",
        f"EnvironmentFile={NIYA_ENV}",
        f"Environment=NIYA_CONFIG={NIYA_CONFIG}",
        f"Environment=HERMES_HOME={hermes_home}",
        f"ExecStart={' '.join(_systemd_arg(a) for a in command)}",
        f"TimeoutStartSec={SCRIPT_TIMEOUT_S + 120}",
        "",
    ])
    timer = "\n".join(["[Unit]", f"Description={description}", "", "[Timer]", *p.timer, "", "[Install]", "WantedBy=timers.target", ""])
    return {f"{name}.service": service, f"{name}.timer": timer}


INSTALL = """#!/bin/sh
# 切換那天在 VPS 上以 root 執行：確認 Hermes 已經停了，再裝上從 Hermes 搬來的排程。
set -e
cd "$(dirname "$0")"
if systemctl --user is-active --quiet hermes-gateway 2>/dev/null || pgrep -f 'hermes.*gateway' >/dev/null; then
    echo "Hermes 還在跑，同一個排程會跑兩次。先停掉 Hermes（systemctl --user stop hermes-gateway），再執行一次。" >&2
    exit 1
fi
install -m 644 {units} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now {timers}
systemctl list-timers '{prefix}*'
"""


@dataclass
class CronStats:
    timers: list[str] = field(default_factory=list)
    alarms: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)


def apply_plans(plans: list[Plan], choices: dict[str, str], alarms: Alarms, units_dir: Path, *, hermes_home: str = HERMES_HOME) -> CronStats:
    stats = CronStats()
    timers = [p for p in plans if choices.get(p.job.id) == "timer"]
    if units_dir.exists():
        shutil.rmtree(units_dir)
    if timers:
        units_dir.mkdir(parents=True)
        names = []
        for p in timers:
            if not p.timer or not p.command:
                stats.refused.append((p.job.name, "這個排程沒辦法變成 timer"))
                continue
            for filename, content in unit_files(p, hermes_home=hermes_home).items():
                (units_dir / filename).write_text(content, encoding="utf-8")
                names.append(filename)
            stats.timers.append(p.job.name)
        install = units_dir / "install.sh"
        install.write_text(INSTALL.format(units=" ".join(names), timers=" ".join(n for n in names if n.endswith(".timer")), prefix=UNIT_PREFIX), encoding="utf-8")
        install.chmod(0o755)
    existing = {a.title for a in alarms.active()}
    for p in (p for p in plans if choices.get(p.job.id) == "alarm"):
        if not p.alarm:
            stats.refused.append((p.job.name, "這個排程沒辦法變成鬧鐘"))
        elif p.job.name in existing:
            stats.existing.append(p.job.name)
        else:
            try:
                alarms.set(**p.alarm, created_by="hermes")
            except ValueError as exc:
                stats.refused.append((p.job.name, str(exc)))
                continue
            stats.alarms.append(p.job.name)
    return stats
