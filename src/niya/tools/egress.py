"""Hold back suspicious uploads until the owner says yes.

An upload is suspicious only when all three hold:
1. the session has read external content (web, MCP, fetched URLs) — it may carry injected instructions;
2. the destination host is unknown — not configured, not mentioned by the owner, not approved before;
3. the data comes from this machine — a file, a command's output, a pipe, or a script.

Secrets never reach the model's context (tool output is redacted), so leaking one
requires referencing a file or command in the command line; literal strings in a
command are therefore not treated as local data.
"""

import asyncio
import json
import re
import shlex
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from niya.core.db import Database
from niya.tools.hardline import _normalize_command_for_detection

PENDING_TTL_S = 30 * 60
OWNER_HOST_TTL_S = 30 * 24 * 3600
MAX_SCRIPT_BYTES = 64 * 1024
AFFIRMATIVE = re.compile(r"^\s*(?:好|好的|可以|ok|okay|yes|y|行|允許|同意|go)\s*[!！。.~～]*\s*$", re.IGNORECASE)

_URL_HOST = re.compile(r"(?i)\b(?:https?|ftps?|wss?|rsync)://(?:[^/\s'\"`@]+@)?(\[[0-9a-f:]+\]|[^/\s'\"`:?#]+)")
_SCP_REMOTE = re.compile(r"^(?:[^@/\s]+@)?([A-Za-z0-9.\-]+|\[[0-9a-fA-F:]+\]):(?!//)")
_DEV_TCP = re.compile(r"/dev/(?:tcp|udp)/([^/\s]+)/")
_DOMAIN = re.compile(r"(?i)(?<![\w.@/-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63})(?![\w-])")
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
_FILE_SUFFIXES = {"py", "md", "txt", "json", "yaml", "yml", "toml", "sh", "log", "conf", "cfg", "ini", "js", "ts", "html", "css", "csv", "db", "env", "service", "lock", "bak", "tar", "gz", "zip", "png", "jpg", "pdf"}
_CODE_SEND = re.compile(
    r"(?i)\b(?:requests|httpx|session|client|axios)\.(?:post|put|patch)\b|urlopen\([^)]*\bdata\s*=|urllib\.request\.Request\([^)]*\bdata\s*="
    r"|\bsocket\b|\bsmtplib\b|\bftplib\b|\bparamiko\b|\bhttps?\.request\(|fetch\([^)]*method\s*:\s*['\"](?:post|put|patch)"
)

_WRAPPERS = {"sudo", "env", "nohup", "time", "exec", "command", "nice", "ionice", "stdbuf", "timeout", "builtin", "doas"}
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
_INTERPRETERS = {"python", "python3", "node", "perl", "ruby", "php", "deno", "bun"}
_FETCHERS = {"curl", "wget", "http", "https", "xh", "xhs", "lynx", "w3m"}
_CURL_DATA = {"-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--data-urlencode", "--json", "-F", "--form", "--form-string"}
_CURL_WITH_VALUE = {"-H", "--header", "-o", "--output", "-u", "--user", "-A", "--user-agent", "-e", "--referer", "-w", "--write-out", "-K", "--config",
                    "-b", "--cookie", "-c", "--cookie-jar", "-x", "--proxy", "--connect-timeout", "-m", "--max-time", "--retry", "-r", "--range",
                    "--cacert", "--cert", "--key", "-E", "--resolve", "--interface", "-z", "--time-cond"}
_SSH_WITH_VALUE = {"-p", "-i", "-l", "-o", "-F", "-J", "-L", "-R", "-D", "-c", "-m", "-b", "-E", "-P", "-S", "-W"}


@dataclass(frozen=True)
class Egress:
    program: str
    hosts: tuple[str, ...]
    local_data: bool


def _host(value: str) -> str | None:
    value = value.strip().strip("'\"")
    if m := _URL_HOST.search(value):
        return _clean_host(m.group(1))
    if m := _SCP_REMOTE.match(value):
        return _clean_host(m.group(1))
    bare = value.split("/", 1)[0].rsplit("@", 1)[-1]
    if re.fullmatch(r"(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,63}(?::\d+)?|\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?", bare):
        return _clean_host(bare)
    return None


def _clean_host(host: str) -> str:
    host = host.lower().strip("[]").rstrip(".")
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host


def _pipelines(command: str) -> list[list[str]]:
    """Split into top-level pipelines (on ; & && || newline), each a list of `|` stages."""
    pipelines: list[list[str]] = []
    stages: list[str] = []
    start, quote, escaped, i = 0, None, False, 0
    while i < len(command):
        char = command[i]
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char in ";&|\n":
            doubled = char in "&|" and i + 1 < len(command) and command[i + 1] == char
            stages.append(command[start:i])
            if char != "|" or doubled:
                pipelines.append(stages)
                stages = []
            i += 1 if doubled else 0
            start = i + 1
        i += 1
    stages.append(command[start:])
    pipelines.append(stages)
    return [[s for s in p if s.strip()] for p in pipelines if any(s.strip() for s in p)]


def _tokens(stage: str) -> list[str]:
    try:
        lexer = shlex.shlex(stage, posix=True, punctuation_chars="<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return stage.split()


def _strip_wrappers(tokens: list[str]) -> list[str]:
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
            i += 1
            continue
        name = Path(token).name
        if name not in _WRAPPERS:
            break
        i += 1
        while i < len(tokens) and tokens[i].startswith("-"):
            i += 1
        if name == "timeout" and i < len(tokens):
            i += 1
    return tokens[i:]


def _substitutions(text: str) -> list[str]:
    return re.findall(r"\$\(([^()]*)\)", text) + re.findall(r"`([^`]*)`", text)


class EgressDetector:
    def __init__(
        self,
        *,
        read_script: Callable[[str, str], str | None] | None = None,
        git_remote_url: Callable[[str, str], Awaitable[str | None]] | None = None,
    ) -> None:
        self.read_script = read_script or _read_script
        self.git_remote_url = git_remote_url or _git_remote_url

    async def detect(self, command: str, cwd: str, *, depth: int = 0) -> list[Egress]:
        found: list[Egress] = []
        normalized = _normalize_command_for_detection(command)
        for pipeline in _pipelines(normalized):
            for index, stage in enumerate(pipeline):
                if hit := await self._stage(stage, piped=index > 0, cwd=cwd, depth=depth):
                    found.append(hit)
                if m := _DEV_TCP.search(stage):
                    found.append(Egress("/dev/tcp", (_clean_host(m.group(1)),), True))
        if depth < 2:
            for inner in _substitutions(normalized):
                found += await self.detect(inner, cwd, depth=depth + 1)
        return found

    def fetches_network(self, command: str) -> bool:
        for pipeline in _pipelines(_normalize_command_for_detection(command)):
            for stage in pipeline:
                tokens = _strip_wrappers(_tokens(stage))
                if tokens and Path(tokens[0]).name in _FETCHERS and any(_host(t) for t in tokens[1:] if not t.startswith("-")):
                    return True
        return False

    async def _stage(self, stage: str, *, piped: bool, cwd: str, depth: int) -> Egress | None:
        tokens = _strip_wrappers(_tokens(stage))
        if not tokens:
            return None
        program, args = Path(tokens[0]).name, tokens[1:]
        stdin_redirect = any(t.startswith("<") for t in args)
        has_subst = "$(" in stage or "`" in stage
        if program == "curl":
            return _curl(args, piped or stdin_redirect, has_subst)
        if program == "wget":
            return _wget(args, has_subst)
        if program in ("http", "https", "xh", "xhs"):
            return _httpie(args, piped or stdin_redirect)
        if program == "scp":
            remote = [a for a in args if not a.startswith("-") and _SCP_REMOTE.match(a)]
            if args and remote and remote[-1] == [a for a in args if not a.startswith("-")][-1]:
                return Egress("scp", (_host(remote[-1]) or "",), True)
            return None
        if program == "rsync":
            operands = [a for a in args if not a.startswith("-")]
            if operands and (_SCP_REMOTE.match(operands[-1]) or operands[-1].startswith("rsync://")):
                return Egress("rsync", tuple(h for h in [_host(operands[-1])] if h), True)
            return None
        if program in ("sftp", "ftp", "lftp", "tftp"):
            return Egress(program, tuple(h for h in [_first_host(args)] if h), True)
        if program in ("nc", "ncat", "netcat", "socat", "telnet"):
            if piped or stdin_redirect or (program == "socat" and any(a == "-" or a.upper().startswith(("FILE:", "EXEC:", "SYSTEM:", "STDIN")) for a in args)):
                hosts = [h for a in args if (h := _socat_host(a) if program == "socat" else _host(a))]
                return Egress(program, tuple(hosts[:1]), True)
            return None
        if program == "ssh":
            if piped or stdin_redirect:
                return Egress("ssh", tuple(h for h in [_ssh_host(args)] if h), True)
            return None
        if program == "git":
            return await self._git(args, cwd)
        if program == "gh":
            return _gh(args)
        if depth < 2 and (program in _SHELLS or program in _INTERPRETERS or tokens[0].startswith(("./", "/", "~/"))):
            return await self._script(tokens, cwd, depth)
        return None

    async def _git(self, args: list[str], cwd: str) -> Egress | None:
        i = 0
        while i < len(args) and args[i].startswith("-"):
            if args[i] in ("-C", "-c") and i + 1 < len(args):
                if args[i] == "-C":
                    cwd = str(Path(cwd) / args[i + 1])
                i += 2
            else:
                i += 1
        if i >= len(args) or args[i] != "push":
            return None
        rest = args[i + 1 :]
        repo = next((a.split("=", 1)[1] for a in rest if a.startswith("--repo=")), None)
        operands = [a for a in rest if not a.startswith("-")]
        target = repo or (operands[0] if operands else "origin")
        url = target if (_URL_HOST.search(target) or _SCP_REMOTE.match(target)) else await self.git_remote_url(cwd, target)
        host = _host(url) if url else None
        return Egress("git push", (host,) if host else (), True)

    async def _script(self, tokens: list[str], cwd: str, depth: int) -> Egress | None:
        program, args = Path(tokens[0]).name, tokens[1:]
        inline = next((args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-c", "-e", "--eval")), None)
        if program in _SHELLS or program in _INTERPRETERS:
            if inline is not None:
                code, is_shell = inline, program in _SHELLS
            else:
                path = next((a for a in args if not a.startswith("-")), None)
                code = self.read_script(path, cwd) if path else None
                is_shell = program in _SHELLS
        else:
            code = self.read_script(tokens[0], cwd)
            is_shell = _shebang(code or "") in _SHELLS
        if not code:
            return None
        if is_shell:
            inner = await self.detect(code, cwd, depth=depth + 1)
            if inner:
                return Egress(f"script:{program}", tuple(h for e in inner for h in e.hosts), True)
            return None
        if _CODE_SEND.search(code):
            hosts = tuple(dict.fromkeys(_clean_host(m.group(1)) for m in _URL_HOST.finditer(code)))
            return Egress(f"script:{program}", hosts, True)
        return None


def _curl(args: list[str], stdin: bool, has_subst: bool) -> Egress | None:
    upload, local, hosts, i = False, False, [], 0
    while i < len(args):
        arg = args[i]
        value: str | None = None
        if arg in _CURL_DATA:
            value = args[i + 1] if i + 1 < len(args) else ""
            i += 1
        elif arg.startswith(("--data", "--json", "--form")) and "=" in arg:
            value = arg.split("=", 1)[1]
        elif re.fullmatch(r"-[dF].+", arg):
            value = arg[2:]
        if value is not None:
            upload = True
            local |= value.startswith("@") or "=@" in value or "=<" in value or "$(" in value or "`" in value
        elif arg in ("-T", "--upload-file"):
            upload = local = True
            i += 1
        elif arg in ("-X", "--request") and i + 1 < len(args):
            upload |= args[i + 1].upper() in ("POST", "PUT", "PATCH")
            i += 1
        elif re.fullmatch(r"(?i)-X(POST|PUT|PATCH)", arg):
            upload = True
        elif arg == "--url" and i + 1 < len(args):
            hosts.append(_host(args[i + 1]))
            i += 1
        elif arg in _CURL_WITH_VALUE:
            i += 1
        elif not arg.startswith("-"):
            hosts.append(_host(arg))
        i += 1
    if not upload:
        return None
    return Egress("curl", tuple(h for h in hosts if h), local or stdin or has_subst)


def _wget(args: list[str], has_subst: bool) -> Egress | None:
    upload = local = False
    for i, arg in enumerate(args):
        name = arg.split("=", 1)[0]
        if name in ("--post-data", "--body-data"):
            upload = True
        elif name in ("--post-file", "--body-file"):
            upload = local = True
        elif name == "--method":
            method = arg.split("=", 1)[1] if "=" in arg else (args[i + 1] if i + 1 < len(args) else "")
            upload |= method.upper() in ("POST", "PUT", "PATCH")
    if not upload:
        return None
    hosts = tuple(h for a in args if not a.startswith("-") and (h := _host(a)))
    return Egress("wget", hosts, local or has_subst)


def _httpie(args: list[str], stdin: bool) -> Egress | None:
    operands = [a for a in args if not a.startswith("-")]
    method = operands[0].upper() if operands and operands[0].isupper() else None
    if method:
        operands = operands[1:]
    url, items = (operands[0], operands[1:]) if operands else ("", [])
    data_items = [i for i in items if re.match(r"^[^=:@]*(?::=|=(?!=)|@)", i)]
    if not (data_items or method in ("POST", "PUT", "PATCH") or stdin):
        return None
    local = stdin or any("@" in i for i in data_items)
    return Egress("httpie", tuple(h for h in [_host(url)] if h), local)


def _gh(args: list[str]) -> Egress | None:
    if args[:2] == ["gist", "create"]:
        return Egress("gh gist", ("gist.github.com",), True)
    if args[:1] == ["api"]:
        fields = [args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-f", "-F", "--field", "--raw-field")]
        has_input = "--input" in args
        if fields or has_input:
            host = next((args[i + 1] for i, a in enumerate(args[:-1]) if a == "--hostname"), "api.github.com")
            return Egress("gh api", (host,), has_input or any("@" in f for f in fields))
    return None


def _first_host(args: list[str]) -> str | None:
    return next((h for a in args if not a.startswith("-") and (h := _host(a) or _clean_host(a.rsplit("@", 1)[-1]))), None)


def _ssh_host(args: list[str]) -> str | None:
    i = 0
    while i < len(args):
        if args[i] in _SSH_WITH_VALUE:
            i += 2
            continue
        if not args[i].startswith("-"):
            return _clean_host(args[i].rsplit("@", 1)[-1])
        i += 1
    return None


def _socat_host(address: str) -> str | None:
    m = re.match(r"(?i)^(?:tcp[46]?|openssl|udp[46]?|ssl)(?:-connect)?:([^:,]+):", address)
    return _clean_host(m.group(1)) if m else None


def _shebang(code: str) -> str:
    first = code.splitlines()[0] if code.startswith("#!") else ""
    words = first[2:].split()
    if words and Path(words[0]).name == "env":
        words = words[1:]
    return Path(words[0]).name if words else ""


def _read_script(path: str, cwd: str) -> str | None:
    file = Path(path).expanduser()
    file = file if file.is_absolute() else Path(cwd) / file
    try:
        if not file.is_file() or file.stat().st_size > MAX_SCRIPT_BYTES:
            return None
        return file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


async def _git_remote_url(cwd: str, remote: str) -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec("git", "-C", cwd, "remote", "get-url", "--push", remote, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 5)
    except (OSError, TimeoutError):
        return None
    if proc.returncode != 0:
        return None
    return out.decode().strip() or None


def extract_hosts(text: str) -> set[str]:
    hosts = {_clean_host(m.group(1)) for m in _URL_HOST.finditer(text)}
    hosts |= {d.lower() for d in _DOMAIN.findall(text) if d.rsplit(".", 1)[-1].lower() not in _FILE_SUFFIXES}
    hosts |= {ip for ip in _IPV4.findall(text) if all(int(p) < 256 for p in ip.split("."))}
    return hosts


@dataclass(frozen=True)
class Pending:
    id: int
    command: str
    hosts: tuple[str, ...]
    taint_source: str


Notifier = Callable[[str], Awaitable[None]]


class EgressGate:
    def __init__(
        self,
        db: Database,
        *,
        now: Callable[[], float],
        detector: EgressDetector | None = None,
        allow_hosts: Iterable[str] = (),
        taint_scope: str = "session",
        notify: Notifier | None = None,
    ) -> None:
        if taint_scope not in ("session", "turn"):
            raise ValueError("egress.taint_scope 只能是 session 或 turn。")
        self.db = db
        self.now = now
        self.detector = detector or EgressDetector()
        self.config_hosts = {h.lower() for h in allow_hosts}
        self.taint_scope = taint_scope
        self.notify = notify

    # --- taint -----------------------------------------------------------

    def mark_tainted(self, session_id: str, turn_id: str, source: str) -> None:
        self.db.execute(
            "UPDATE sessions SET tainted_at = COALESCE(tainted_at, ?), taint_source = COALESCE(taint_source, ?), taint_turn_id = ? WHERE id = ?",
            (self.now(), source, turn_id, session_id),
        )

    def taint(self, session_id: str, turn_id: str) -> str | None:
        row = self.db.one("SELECT tainted_at, taint_source, taint_turn_id FROM sessions WHERE id = ?", (session_id,))
        if not row or row["tainted_at"] is None:
            return None
        if self.taint_scope == "turn" and row["taint_turn_id"] != turn_id:
            return None
        return row["taint_source"] or "外部內容"

    def end_turn(self, session_id: str) -> None:
        if self.taint_scope == "turn":
            self.db.execute("UPDATE sessions SET tainted_at = NULL, taint_source = NULL, taint_turn_id = NULL WHERE id = ?", (session_id,))

    # --- known hosts -----------------------------------------------------

    def learn_owner_hosts(self, text: str) -> None:
        now = self.now()
        for host in extract_hosts(text):
            self.db.execute(
                "INSERT INTO egress_known_hosts(host, source, created_at, last_seen) VALUES (?, 'owner_message', ?, ?)"
                " ON CONFLICT(host) DO UPDATE SET last_seen = excluded.last_seen",
                (host, now, now),
            )

    def allow(self, host: str, source: str = "manual") -> None:
        now = self.now()
        self.db.execute(
            "INSERT INTO egress_known_hosts(host, source, created_at, last_seen) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(host) DO UPDATE SET source = excluded.source, last_seen = excluded.last_seen",
            (_clean_host(host), source, now, now),
        )

    def remove(self, host: str) -> bool:
        return self.db.execute("DELETE FROM egress_known_hosts WHERE host = ?", (_clean_host(host),)).rowcount > 0

    def known_hosts(self) -> list[tuple[str, str]]:
        rows = self.db.query("SELECT host, source FROM egress_known_hosts ORDER BY host")
        return [(h, "config") for h in sorted(self.config_hosts)] + [(r["host"], r["source"]) for r in rows]

    def is_known(self, host: str) -> bool:
        candidates = [host]
        parts = host.split(".")
        if not _IPV4.fullmatch(host):
            candidates += [".".join(parts[i:]) for i in range(1, len(parts) - 1)]
        if any(c in self.config_hosts for c in candidates):
            return True
        marks = ",".join("?" * len(candidates))
        rows = self.db.query(f"SELECT source, last_seen FROM egress_known_hosts WHERE host IN ({marks})", candidates)
        return any(r["source"] != "owner_message" or self.now() - r["last_seen"] <= OWNER_HOST_TTL_S for r in rows)

    # --- the guard -------------------------------------------------------

    async def check(self, command: str, *, session_id: str, turn_id: str, cwd: str) -> str | None:
        found = await self.detector.detect(command, cwd)
        if not found:
            return None
        taint = self.taint(session_id, turn_id)
        unknown = sorted({h for e in found for h in e.hosts if not self.is_known(h)} | ({"（無法判斷目的地）"} if any(not e.hosts for e in found) else set()))
        suspicious = bool(taint) and bool(unknown) and any(e.local_data for e in found)
        if suspicious and self._approved_command(session_id, command):
            suspicious = False
        self._log(session_id, turn_id, command, found, taint is not None, suspicious)
        if not suspicious:
            return None
        pending = self._create_pending(session_id, turn_id, command, tuple(unknown), taint or "")
        if self.notify:
            await self.notify(
                f"⚠️ niya 想把本機資料傳到陌生主機 {', '.join(pending.hosts)}（這個 session 讀過 {pending.taint_source}）。\n"
                f"指令：`{command}`\n回「好」就放行，回其他內容就不放行。"
            )
        return (
            f"⛔ 已暫停：這次外傳需要主人同意（原因：讀過外部內容後，要把本機資料傳到陌生主機 {', '.join(pending.hosts)}）。"
            "系統已經問主人了；收到「主人同意」的通知後，再重新執行同一個指令。不要改用其他方法。"
        )

    async def check_upload(self, what: str, destination: str, *, session_id: str, turn_id: str) -> str | None:
        """Local files a tool itself sends out (not a command): held back the same way while the session is tainted."""
        taint = self.taint(session_id, turn_id)
        suspicious = bool(taint) and not self._approved_command(session_id, what)
        self._log(session_id, turn_id, what, [Egress(what.split(" ", 1)[0], (destination,), True)], taint is not None, suspicious)
        if not suspicious:
            return None
        pending = self._create_pending(session_id, turn_id, what, (destination,), taint or "")
        if self.notify:
            await self.notify(f"⚠️ niya 想把本機檔案傳到 {destination}（這個 session 讀過 {pending.taint_source}）。\n內容：`{what}`\n回「好」就放行，回其他內容就不放行。")
        return (
            f"⛔ 已暫停：這次外傳需要主人同意（原因：讀過外部內容後，要把本機檔案傳到 {destination}）。"
            "系統已經問主人了；收到「主人同意」的通知後，再用同樣的參數呼叫一次。不要改用其他方法。"
        )

    def _approved_command(self, session_id: str, command: str) -> bool:
        row = self.db.one(
            "SELECT id FROM egress_pending WHERE session_id = ? AND command = ? AND status = 'approved' AND used_at IS NULL AND decided_at >= ?",
            (session_id, command, self.now() - PENDING_TTL_S),
        )
        if row:
            self.db.execute("UPDATE egress_pending SET used_at = ? WHERE id = ?", (self.now(), row["id"]))
        return row is not None

    def _create_pending(self, session_id: str, turn_id: str, command: str, hosts: tuple[str, ...], taint: str) -> Pending:
        now = self.now()
        with self.db.transaction():
            self.db.execute("UPDATE egress_pending SET status = 'expired', decided_at = ?, notified_expired = 1 WHERE session_id = ? AND status = 'pending'", (now, session_id))
            cursor = self.db.execute(
                "INSERT INTO egress_pending(session_id, turn_id, command, hosts, taint_source, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, turn_id, command, json.dumps(hosts, ensure_ascii=False), taint, now, now + PENDING_TTL_S),
            )
        return Pending(cursor.lastrowid, command, hosts, taint)

    def _log(self, session_id: str, turn_id: str, command: str, found: Sequence[Egress], tainted: bool, suspicious: bool) -> None:
        self.db.execute(
            "INSERT INTO egress_log(ts, session_id, turn_id, command, program, hosts, local_data, tainted, suspicious, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.now(), session_id, turn_id, command, ",".join(e.program for e in found),
                json.dumps(sorted({h for e in found for h in e.hosts}), ensure_ascii=False),
                int(any(e.local_data for e in found)), int(tainted), int(suspicious), "paused" if suspicious else "executed",
            ),
        )

    # --- owner replies ---------------------------------------------------

    def resolve_owner_reply(self, session_id: str, text: str) -> list[str]:
        """Called with each owner message before it reaches the model; returns system lines to show first."""
        lines = self.expired_notices(session_id)
        row = self.db.one("SELECT * FROM egress_pending WHERE session_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1", (session_id,))
        if row is None:
            return lines
        hosts = json.loads(row["hosts"])
        now = self.now()
        if AFFIRMATIVE.match(text):
            self.db.execute("UPDATE egress_pending SET status = 'approved', decided_at = ? WHERE id = ?", (now, row["id"]))
            for host in hosts:
                if not host.startswith("（"):
                    self.allow(host, source="approved")
            lines.append(f"主人同意了外傳 #{row['id']}（{', '.join(hosts)}），可以重新執行同一個指令。")
        else:
            self.db.execute("UPDATE egress_pending SET status = 'denied', decided_at = ? WHERE id = ?", (now, row["id"]))
            lines.append(f"主人沒有同意外傳 #{row['id']}（{', '.join(hosts)}），不要執行，也不要改用其他方法。")
        return lines

    def expired_notices(self, session_id: str) -> list[str]:
        now = self.now()
        rows = self.db.query("SELECT id FROM egress_pending WHERE session_id = ? AND status = 'pending' AND expires_at < ?", (session_id, now))
        for row in rows:
            self.db.execute("UPDATE egress_pending SET status = 'expired', decided_at = ?, notified_expired = 1 WHERE id = ?", (now, row["id"]))
        return [f"外傳 #{row['id']} 等了 30 分鐘沒有回覆，已經過期，不會執行。" for row in rows]
