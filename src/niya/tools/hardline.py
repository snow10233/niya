"""Commands niya refuses to run, no matter what.

Adapted from NousResearch/hermes-agent (MIT) tools/approval.py@222465d84:
the hardline patterns, command normalization and quote-aware detection
variants are kept as-is; Hermes-home folding, Windows path variants and the
approval machinery are removed. niya adds root-specific rules (self
protection and SSH lock-out) in `protected_patterns`.
"""

import functools
import os
import re
import shlex
import unicodedata
from collections.abc import Iterable
from pathlib import Path

from niya.tools.ansi_strip import strip_ansi

# Regex fragment matching the *start* of a command (i.e. positions where
# a shell would begin parsing a new command).  Used by shutdown/reboot
# patterns so they don't fire on "echo reboot" or "grep 'shutdown' log".
# Matches: start of string, after command separators (; && || | newline),
# after subshell openers ( `$(` or backtick ), optionally consuming
# leading wrapper commands (sudo, env VAR=VAL, exec, nohup, setsid).
_CMDPOS = (
    # Real ;/&/| separators are converted to newlines by the quote-aware
    # _mark_command_starts pass. Keeping them in this flat regex mistakes
    # quoted regex/data (for example grep '(safe|rm -rf /)') for commands.
    r'(?:^|[\n`]|\$\()'            # start position
    r'\s*'                          # optional whitespace
    r'(?:sudo\s+(?:-[^\s]+\s+)*)?'  # optional sudo with flags
    r'(?:env\s+(?:\w+=\S*\s+)*)?'   # optional env with VAR=VAL pairs
    r'(?:(?:exec|nohup|setsid|time)\s+)*'  # optional wrapper commands
    r'\s*'
)

# Destructive-path argument matcher for the rm hardline rules.
#
# The path token in `rm -rf /` is almost always written quoted in real
# shells — `rm -rf "/"`, `rm -rf "$HOME"` — and `${HOME}` is the universal
# brace form. A bare-token anchor (`(/...)(\s|$)`) silently misses all of
# these: the surrounding quote breaks both the leading position (the flag
# group can't consume `"`) and the trailing `(\s|$)` terminator, letting
# `rm -rf "/"` slip past the unconditional floor entirely.
#
# Accept the path either fully wrapped in a matching quote pair OR bare with
# a terminator. The matching-quote branch catches `rm -rf "/"` (path quoted
# on its own). The bare branch's terminator accepts whitespace, end-of-string
# OR a shell metacharacter (`) ` ; | &`) so a real root wipe inside a command
# substitution — `$(rm -rf /)`, `` `rm -rf /` `` — whose `/` is terminated by
# `)`/backtick is still caught.
def _hardline_rm_path(path_alt: str, tail: str = r'(?:\s|$|[)`;|&])') -> str:
    return rf'(?:["\'](?:{path_alt})["\']|(?:{path_alt}){tail})'


# Protected system roots whose recursive deletion has no recovery path.
_HARDLINE_SYSTEM_DIRS = (
    r'/home|/home/\*|/root|/root/\*|/etc|/etc/\*|/usr|/usr/\*|'
    r'/var|/var/\*|/bin|/bin/\*|/sbin|/sbin/\*|/boot|/boot/\*|/lib|/lib/\*'
)

# `rm` plus its flag group, shared by the three rm hardline rules. Kept as a
# plain concatenation (not an f-string) so the regex backslashes never live
# inside an f-string replacement field — unsupported on the Python 3.11 floor.
#
# Anchored to _CMDPOS (start of line, after a command separator ; && || |,
# after a subshell opener $(/backtick, or after sudo/env/exec wrappers) so the
# rule fires only when `rm` is an actual command word — not when the literal
# string "rm -rf /" appears as DATA inside another command's argument, e.g.
# `gh pr create --title "block rm -rf / spellings"` or `git commit -m "…rm -rf
# /…"`. Those tripped the unconditional floor and could not run at all before
# the anchor. A real wipe at any command position (bare, chained, in $()/`…`,
# under sudo) still matches; the quoted-path branch in _hardline_rm_path keeps
# catching `rm -rf "/"`.
_RM_FLAG_PREFIX = _CMDPOS + r'rm\s+(-[^\s]*\s+)*'

HARDLINE_PATTERNS = [
    # rm recursive targeting the root filesystem or protected roots.
    # `${HOME}` brace form and quoted paths (`rm -rf "/"`, `rm -rf "$HOME"`)
    # are handled via _hardline_rm_path so the floor cannot be bypassed with
    # the ordinary quoting/brace shell idioms.
    #
    # The path token matches any root-anchored path whose components collapse
    # back to "/" in the shell: a bare "/", repeated slashes ("//"), and
    # "."/".." current/parent segments ("/.", "/./", "/..", "/../..") all
    # resolve to root, optionally followed by a trailing glob ("/*", "//*").
    # Each inter-slash segment must be exactly "." or "..", so a longer dot
    # run or any real name is a literal directory, NOT root — "/tmp", "/home",
    # "/.ssh", "/.config" and even "/..." (a dir literally named "...") fall
    # through to the softer DANGEROUS_PATTERNS / system-directory rules
    # instead of being unconditionally hardline-blocked. The explicit "/ \*"
    # alt preserves the slash-space-glob spelling (`rm -rf / *`, which the
    # shell sees as two args: "/" plus the "*" glob).
    (_RM_FLAG_PREFIX + _hardline_rm_path(r'/(?:(?:\.\.?)?/)*(?:\.\.?)?\**|/ \*'), "recursive delete of root filesystem"),
    (_RM_FLAG_PREFIX + _hardline_rm_path(_HARDLINE_SYSTEM_DIRS), "recursive delete of system directory"),
    (_RM_FLAG_PREFIX + _hardline_rm_path(r'(?:~|\$\{?HOME\}?)(?:/?|/\*)?'), "recursive delete of home directory"),
    # Filesystem format
    (r'\bmkfs(\.[a-z0-9]+)?\b', "format filesystem (mkfs)"),
    # Raw block device overwrites (dd + redirection)
    (r'\bdd\b[^\n]*\bof=/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*', "dd to raw block device"),
    (r'>\s*/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*\b', "redirect to raw block device"),
    # Fork bomb (classic shell form)
    (r':\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:', "fork bomb"),
    # Kill every process on the system
    (r'\bkill\s+(-[^\s]+\s+)*-1\b', "kill all processes"),
    # System shutdown / reboot — anchor to command position (start of line,
    # after a command separator, or after sudo/env wrappers) so we don't
    # false-positive on "echo reboot" or "grep 'shutdown' logs".
    # _CMDPOS matches start-of-command positions.
    (_CMDPOS + r'(shutdown|reboot|halt|poweroff)\b', "system shutdown/reboot"),
    (_CMDPOS + r'init\s+[06]\b', "init 0/6 (shutdown/reboot)"),
    (_CMDPOS + r'systemctl\s+(poweroff|reboot|halt|kexec)\b', "systemctl poweroff/reboot"),
    (_CMDPOS + r'telinit\s+[06]\b', "telinit 0/6 (shutdown/reboot)"),
]

# Pre-compiled variant used by the hot-path matcher. Building these at module
# load eliminates the ~2.6 ms cold-cache re.compile fan-out on the first
# terminal() call per process (12 HARDLINE + 47 DANGEROUS patterns, each
# potentially evicted from Python's 512-entry ``re._cache`` by unrelated
# regex work elsewhere in the agent). DANGEROUS_PATTERNS_COMPILED is built
# at the end of this module after DANGEROUS_PATTERNS is defined.
_RE_FLAGS = re.IGNORECASE | re.DOTALL
HARDLINE_PATTERNS_COMPILED = [
    (re.compile(pattern, _RE_FLAGS), description)
    for pattern, description in HARDLINE_PATTERNS
]



def _normalize_command_for_detection(command: str) -> str:
    """Normalize a command string before dangerous-pattern matching.

    Strips ANSI escape sequences (full ECMA-48 via tools.ansi_strip),
    null bytes, and normalizes Unicode fullwidth characters so that
    obfuscation techniques cannot bypass the pattern-based detection.
    """
    # Strip all ANSI escape sequences (CSI, OSC, DCS, 8-bit C1, etc.)
    command = strip_ansi(command)
    # Strip null bytes
    command = command.replace('\x00', '')
    # Normalize Unicode (fullwidth Latin, halfwidth Katakana, etc.)
    command = unicodedata.normalize('NFKC', command)
    # Collapse shell line continuations (backslash-newline). The shell removes
    # BOTH characters and joins the tokens, so `rm -rf \<newline>/` executes as
    # `rm -rf /`. This must run BEFORE the generic backslash-escape strip below,
    # whose [^\n] class deliberately skips newlines and would otherwise leave
    # the dangling backslash wedged between tokens — defeating the structured
    # rm/mkfs/dd patterns (notably the HARDLINE root-delete floor, which cannot
    # be bypassed even with yolo). Handles both \n and \r\n line endings. Line
    # continuations carry no path separator, so this is a no-op on the Windows
    # home-prefix folds below (which match C:\Users\alice\... — no newline).
    command = re.sub(r'\\\r?\n', '', command)
    # Fold the absolute home prefix into ~/ so static patterns catch /root/.ssh/x the
    # same way they catch ~/.ssh/x. This must run before the backslash strip below.
    command = _rewrite_resolved_user_home(command)
    # Strip shell backslash-escapes: r\m → rm. Prevents \-injection bypass.
    command = re.sub(r'\\([^\n])', r'\1', command)
    # Strip empty-string literals that split tokens: r''m → rm, r"\"m → rm.
    command = re.sub(r"''|\"\"", '', command)
    # Collapse $IFS / ${IFS} word-separator expansions to a literal space.
    # In any POSIX shell the IFS variable defaults to <space><tab><newline>,
    # so `rm${IFS}-rf${IFS}/` is executed as `rm -rf /`. Because the dangerous
    # and hardline patterns anchor on literal whitespace (\s) between a command
    # and its arguments, leaving the unexpanded `${IFS}` token in place lets an
    # attacker slip past EVERY pattern — including the unconditional hardline
    # floor (rm -rf /, mkfs, dd to raw device, shutdown/reboot). Substituting a
    # space here mirrors the shell's own expansion so the patterns fire. The
    # brace form also covers bash substring expansions like `${IFS:0:1}` (a
    # single space). Same de-obfuscation class as the backslash/empty-quote
    # handling above.
    command = re.sub(r'\$\{IFS\b[^}]*\}|\$IFS\b', ' ', command)
    return command


# Shell metacharacters, quotes, and whitespace that terminate a filesystem
# path token on a command line. Used to bound the path tail we normalize.
_PATH_TOKEN_STOP = r"""\s'"`;|&<>()"""
# One path segment (no separators, no terminators) preceded by a separator.
_PATH_TAIL = r"(?P<tail>(?:[/\\][^/\\" + _PATH_TOKEN_STOP + r"]*)+)"


@functools.lru_cache(maxsize=64)
def _home_prefix_fold_regex(path: str):
    """Compile a regex matching *path* used as an absolute directory prefix.

    The home components are matched with either separator (``/`` or ``\\``)
    between them, followed by the rest of the path token (the ``tail`` group),
    so a Windows native path (``C:\\Users\\alice\\.ssh\\authorized_keys``), its
    forward-slash form, and mixed-separator forms all fold — and the tail's
    backslashes get normalized to ``/`` by the caller so multi-segment static
    patterns (``~/.ssh/authorized_keys``) still match. The trailing tail is
    required (``+``), so a bare home with no path under it is not folded.

    Returns ``None`` for an unset or degenerate path — one with fewer than two
    components below the root — so a stray HOME / HERMES_HOME such as ``/``,
    ``C:\\`` or ``""`` cannot rewrite unrelated filesystem prefixes. Cached
    because the resolved home is stable across calls on this hot path.
    """
    if not path:
        return None
    components = [c for c in re.split(r"[/\\]+", path) if c]
    # Require at least two non-empty components below the root. For POSIX this
    # mirrors the historical ``count("/") >= 2`` guard (``/home/alice`` folds,
    # ``/home`` does not); for Windows it rejects a bare drive root (``C:\\``)
    # while accepting a real home (``C:\\Users\\alice``).
    if len(components) < 2:
        return None
    body = r"[/\\]+".join(re.escape(c) for c in components)
    # Optional leading root separator (POSIX ``/`` or UNC ``\\``); a Windows
    # drive letter is captured as the first component.
    return re.compile(r"[/\\]*" + body + _PATH_TAIL)


def _fold_home_prefixes(command: str, paths, replacement: str) -> str:
    """Fold each resolved home *path* prefix in *command* to *replacement*.

    *replacement* has no trailing separator (``~`` / ``~/.hermes``); the matched
    path tail (with its backslashes normalized to ``/``) supplies it. Longest
    candidate first so a deeper home (e.g. an explicit HOME under USERPROFILE)
    folds before a shorter overlapping one that would otherwise clobber it.
    """
    seen: set[str] = set()
    for path in sorted((p for p in paths if p), key=len, reverse=True):
        if path in seen:
            continue
        seen.add(path)
        pattern = _home_prefix_fold_regex(path)
        if pattern is not None:
            command = pattern.sub(
                lambda m: replacement + m.group("tail").replace("\\", "/"),
                command,
            )
    return command


def _rewrite_resolved_user_home(command: str) -> str:
    """Rewrite the current user's absolute home prefix to ``~/``.

    Resolves the home at detection time — its expanduser form, symlink-resolved
    form, and an explicitly set ``HOME`` — so absolute home paths are checked by
    the same static patterns as tilde and ``$HOME`` forms. ``HOME`` is consulted
    directly because Windows' ``os.path.expanduser`` resolves ``~`` from
    ``USERPROFILE`` and ignores ``HOME``, unlike POSIX. Matches both POSIX
    (``/home/alice``) and Windows (``C:\\Users\\alice`` or ``C:/Users/alice``)
    separators. No-op when the home is unset or degenerate.
    """
    try:
        home = os.path.expanduser("~")
        candidates = [
            home,
            os.path.realpath(home),
            os.environ.get("HOME", ""),
        ]
    except Exception:
        return command
    return _fold_home_prefixes(command, candidates, "~")


_PARAM_REPLACEMENT_RE = re.compile(r"\$\{[^}/\s]+/[^}/]*/(?P<replacement>[^}]*)\}")
_PARAM_DEFAULT_RE = re.compile(r"\$\{[^}:}\s]+:-(?P<default>[^}]*)\}")
_SIMPLE_SHELL_LITERAL_RE = re.compile(r"^[A-Za-z0-9_./:@%+=,-]+$")
_ENV_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
_COMMAND_WRAPPER_WORDS = {
    "sudo",
    "env",
    "exec",
    "nohup",
    "setsid",
    "time",
    "command",
    "builtin",
}
_SUDO_OPTIONS_WITH_ARG = {
    "-c", "--close-from",
    "-g", "--group",
    "-h", "--host",
    "-p", "--prompt",
    "-u", "--user",
}

_INTERPRETER_EXEC_FLAGS = {
    "python": {"-c"},
    "node": {"-e", "--eval", "-p", "--print"},
    "perl": {"-e", "--eval"},
    "ruby": {"-e"},
    "php": {"-r"},
    "powershell": {"-command", "-c", "-file", "-f"},
}
_INTERPRETER_WITH_ARG = {
    "python": {"-W", "-X", "--check-hash-based-pycs"},
    "node": {"-C", "--conditions", "--cpu-prof-dir", "--diagnostic-dir", "--icu-data-dir", "--import", "--loader", "--openssl-config", "--require", "--title"},
    "perl": {"-0", "-F", "-I", "-M", "-m", "-x"},
    "ruby": {"-C", "-E", "-F", "-I", "-K", "-r"},
    "php": {"-c", "-d", "-z"},
    "powershell": {"-configurationname", "-custompipename", "-executionpolicy", "-inputformat", "-outputformat", "-settingsfile", "-version", "-windowstyle", "-workingdirectory"},
}
_READ_TOOL_EXEC_FLAGS = {
    "sort": {"--compress-program"},
    "rg": {"--pre", "--hostname-bin"},
    "ag": {"--pager"},
    "man": {"--pager", "--html", "-P", "-H"},
}
# Required-argument options are ownership boundaries: an option-looking next
# token is data, not another option. These sets mirror the invocation grammar
# of the supported binaries (ripgrep 14, GNU sort, man-db, and ag 2.2).
_READ_TOOL_LONG_OPTIONS_WITH_ARG = {
    "rg": {
        "--after-context", "--before-context", "--color", "--colors",
        "--context", "--context-separator", "--dfa-size-limit", "--encoding",
        "--engine", "--field-context-separator", "--field-match-separator",
        "--file", "--generate", "--glob", "--hostname-bin",
        "--hyperlink-format", "--iglob", "--ignore-file", "--max-columns",
        "--max-count", "--max-depth", "--max-filesize", "--path-separator",
        "--pre", "--pre-glob", "--regex-size-limit", "--regexp", "--replace",
        "--sort", "--sortr", "--threads", "--type", "--type-add",
        "--type-clear", "--type-not",
    },
    "sort": {
        "--batch-size", "--buffer-size", "--compress-program",
        "--field-separator", "--files0-from", "--key", "--output",
        "--parallel", "--random-source", "--sort", "--temporary-directory",
    },
    "man": {
        "--config-file", "--encoding", "--extension", "--locale",
        "--manpath", "--pager", "--preprocessor", "--prompt", "--recode",
        "--sections", "--systems",
    },
    "ag": {
        "--ackmate-dir-filter", "--color-line-number", "--color-match",
        "--color-path", "--depth", "--filename-pattern", "--file-search-regex",
        "--ignore", "--ignore-dir", "--max-count", "--pager",
        "--path-to-ignore", "--width", "--workers",
    },
}
_READ_TOOL_SHORT_OPTIONS_WITH_ARG = {
    "rg": frozenset("efEmjgdtTABCMr"),
    "sort": frozenset("koStT"),
    "man": frozenset("CRLmMSserEPp"),
    "ag": frozenset("gGmpW"),
}
_SHELL_PUNCTUATION = {";", "&", "&&", "|", "||", "(", ")", "{", "}"}
_MAX_DETECTION_COMMAND_CHARS = 128_000
_MAX_SEPARATOR_FREE_COMMAND_CHARS = 4_096
_MAX_DETECTION_SEGMENTS = 25_000
_PARSER_LIMIT_DESCRIPTION = "command parser limit exceeded"
_MALFORMED_EXEC_DESCRIPTION = "command parser limit or malformed executable payload"



def _command_parser_limit_exceeded(command: str) -> bool:
    """Bound all parser work before normalization/tokenization.

    Counting separator characters is deliberately conservative: quoted
    separators can over-count, but crossing this very high ceiling fails
    closed rather than allowing an uninspected suffix to execute.
    """
    if len(command) > _MAX_DETECTION_COMMAND_CHARS:
        return True
    # Long separator-free input has no compound-command utility and otherwise
    # makes every legacy regex inspect one giant token. Reject it before any
    # normalization, tokenization, or regex work.
    if (
        len(command) > _MAX_SEPARATOR_FREE_COMMAND_CHARS
        and not any(char in command for char in ";&|\n")
    ):
        return True
    separators = 0
    for char in command:
        if char in ";&|\n":
            separators += 1
            if separators >= _MAX_DETECTION_SEGMENTS:
                return True
    return False


def _shell_tokens_with_spans(segment: str, start: int):
    """Return shell words as ``(value, start, end, quoted)`` or ``None``.

    This deliberately small lexer never expands shell syntax.  It exists to
    preserve source spans, which ``shlex`` does not expose, while deciding
    which *quoted* grep operand is data rather than another command.
    """
    tokens = []
    i = start
    while i < len(segment):
        while i < len(segment) and segment[i].isspace():
            i += 1
        if i >= len(segment):
            break
        token_start = i
        value = []
        quote = None
        while i < len(segment) and (quote or not segment[i].isspace()):
            char = segment[i]
            if quote:
                if char == quote:
                    quote = None
                    i += 1
                elif char == "\\" and quote == '"' and i + 1 < len(segment):
                    value.append(segment[i + 1])
                    i += 2
                else:
                    value.append(char)
                    i += 1
            elif char in {"'", '"'}:
                quote = char
                i += 1
            elif char == "\\":
                if i + 1 >= len(segment):
                    return None
                value.append(segment[i + 1])
                i += 2
            else:
                value.append(char)
                i += 1
        if quote:
            return None
        raw = segment[token_start:i]
        # Only a wholly single-quoted operand is inert shell data. Double
        # quotes still execute $() and backticks; unquoted substitutions do too.
        inert_single_quoted = (
            (raw.startswith("'") and raw.endswith("'"))
            or ("='" in raw and raw.endswith("'"))
        )
        tokens.append(("".join(value), token_start, i, inert_single_quoted))
    return tokens


_GREP_OPTIONS_WITH_ARG = {
    "--after-context", "--before-context", "--binary-files", "--context",
    "--directories", "--devices", "--exclude", "--exclude-dir",
    "--exclude-from", "--include", "--label", "--max-count",
    "--regexp", "--file",
}
_GREP_SHORT_OPTIONS_WITH_ARG = {"A", "B", "C", "D", "d", "e", "f", "m"}


def _quoted_grep_pattern_spans(command: str) -> tuple[list[tuple[int, int]], bool]:
    """Structurally locate quoted grep PCRE operands.

    The returned boolean means the grep parse was ambiguous or malformed.  In
    that case callers fail closed and, critically, use the original command:
    no text is hidden on an uncertain parse.
    """
    spans: list[tuple[int, int]] = []
    offset = 0
    for segment in _iter_top_level_shell_segments(command):
        segment_at = command.find(segment, offset)
        offset = segment_at + len(segment)
        for start, _, word in _iter_shell_command_word_spans(segment):
            if os.path.basename(_deobfuscate_shell_word_for_detection(word)).lower() not in {
                "grep", "egrep",
            }:
                continue
            tokens = _shell_tokens_with_spans(segment, start)
            if tokens is None:
                return [], True
            args = tokens[1:]
            pcre = False
            explicit_patterns = False
            pattern_indexes: list[int] = []
            operand_index = None
            i = 0
            options = True
            while i < len(args):
                token = args[i][0]
                if options and token == "--":
                    options = False
                    i += 1
                    continue
                if options and token.startswith("--"):
                    option, equals, _ = token.partition("=")
                    if option == "--perl-regexp":
                        pcre = True
                    if option in {"--regexp", "--file"}:
                        explicit_patterns = True
                    if option in _GREP_OPTIONS_WITH_ARG and not equals:
                        if i + 1 >= len(args):
                            return [], True
                        if option == "--regexp":
                            pattern_indexes.append(i + 1)
                        i += 2
                        continue
                    if option == "--regexp" and equals:
                        pattern_indexes.append(i)
                    i += 1
                    continue
                if options and token.startswith("-") and token != "-":
                    chars = token[1:]
                    j = 0
                    while j < len(chars):
                        char = chars[j]
                        if char == "P":
                            pcre = True
                        if char in {"e", "f"}:
                            explicit_patterns = True
                        if char in _GREP_SHORT_OPTIONS_WITH_ARG:
                            if j + 1 < len(chars):
                                if char == "e":
                                    pattern_indexes.append(i)
                            else:
                                if i + 1 >= len(args):
                                    return [], True
                                if char == "e":
                                    pattern_indexes.append(i + 1)
                                i += 1
                            break
                        j += 1
                    i += 1
                    continue
                if operand_index is None:
                    operand_index = i
                i += 1
            if not explicit_patterns:
                if operand_index is None:
                    return [], bool(pcre)
                pattern_indexes.append(operand_index)
            if pcre:
                for index in pattern_indexes:
                    _, token_start, token_end, quoted = args[index]
                    if quoted:
                        spans.append((segment_at + token_start, segment_at + token_end))
    return spans, False


def _grep_safe_detection_variant(command: str) -> tuple[str, bool]:
    spans, malformed = _quoted_grep_pattern_spans(command)
    if malformed or not spans:
        return command, malformed
    parts = []
    previous = 0
    for start, end in spans:
        parts.extend((command[previous:start], " " * (end - start)))
        previous = end
    parts.append(command[previous:])
    return "".join(parts), False


def _interpreter_family(executable: str) -> str | None:
    name = os.path.basename(executable).lower()
    if re.fullmatch(r"py(?:\.exe)?|python[23]?(?:\.\d+)*(?:\.exe)?", name):
        return "python"
    if re.fullmatch(r"node(?:js)?(?:\.exe)?", name):
        return "node"
    if re.fullmatch(r"perl[0-9]*(?:\.\d+)*(?:\.exe)?", name):
        return "perl"
    if re.fullmatch(r"ruby[0-9.]*(?:\.exe)?", name):
        return "ruby"
    if re.fullmatch(r"php(?:\.exe)?", name):
        return "php"
    if re.fullmatch(r"powershell(?:\.exe)?|pwsh(?:\.exe)?", name):
        return "powershell"
    return None


def _shell_segment_tokens(segment: str, start: int) -> list[str] | None:
    """Tokenize an already-bounded command segment.

    ``None`` distinguishes malformed quoting from an empty segment so callers
    can fail closed for a program-bearing option rather than silently skip it.
    """
    try:
        lexer = shlex.shlex(segment[start:], posix=True, punctuation_chars="<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return None


def _iter_top_level_shell_segments(command: str):
    """Yield top-level command segments in one left-to-right pass."""
    start = 0
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char in ";&|\n":
            if start < index:
                yield command[start:index]
            # Consume a doubled && / || separator as one boundary.
            if char in "&|" and index + 1 < len(command) and command[index + 1] == char:
                index += 1
            start = index + 1
        index += 1
    if start < len(command):
        yield command[start:]


def _split_option(token: str) -> tuple[str, str | None]:
    if "=" in token:
        option, value = token.split("=", 1)
        return option, value
    return token, None


def _interpreter_exec_flag(family: str, args: list[str]) -> str | None:
    """Return an execution-bearing interpreter option, if present."""
    flags = _INTERPRETER_EXEC_FLAGS[family]
    skip_value = False
    for token in args:
        if skip_value:
            skip_value = False
            continue
        if token == "--":
            break
        if family != "powershell" and not token.startswith("-"):
            break
        option, attached = _split_option(token)
        comparable = option.lower() if family == "powershell" else option
        if comparable in flags:
            return comparable
        with_arg = _INTERPRETER_WITH_ARG[family]
        # `-Wonce` and `ruby -rjson` attach an option value; they are not
        # short-option bundles containing an execution flag. PowerShell's
        # normal long options also use one dash, so bundle parsing never
        # applies to that family.
        has_attached_option_value = any(
            option.startswith(short) and len(option) > len(short)
            for short in with_arg
            if short.startswith("-") and not short.startswith("--")
        )
        if (
            family != "powershell"
            and not option.startswith("--")
            and len(option) > 2
            and not has_attached_option_value
        ):
            for char in option[1:]:
                short = f"-{char}"
                if short in flags:
                    return short
        if comparable in with_arg and attached is None:
            skip_value = True
    return None


_BASH_OPTIONS_WITH_ARG = {"-O", "+O", "-o", "+o", "--init-file", "--rcfile"}
_BASH_SHORT_OPTION_LETTERS = frozenset("ilrsDcabefhkmnptuvxBCEHPTOo")


def _bash_exec_payload(args: list[str]) -> tuple[bool, str | None]:
    """Return whether Bash ``-c`` occurs and the command string it owns.

    Bash's O/o invocation options consume the following argument even when
    they precede a later ``-c`` or occur in the same short-option bundle.
    Likewise, the two startup-file long options own their next token. Parsing
    those operands first prevents both missed payloads and false ``-c`` hits.
    """
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--" or not token.startswith(("-", "+")):
            break
        if token in _BASH_OPTIONS_WITH_ARG:
            index += 2
            continue
        if token.startswith("--"):
            index += 1
            continue

        chars = token[1:]
        # Bash option letters are case-sensitive. Restricting this to its
        # documented alphabet preserves invalid controls such as `-Wc`.
        if not set(chars) <= _BASH_SHORT_OPTION_LETTERS:
            index += 1
            continue
        consumed_option_arg = "O" in chars or "o" in chars
        if "c" not in chars:
            index += 1 + int(consumed_option_arg)
            continue
        payload_index = index + 1 + int(consumed_option_arg)
        payload = args[payload_index] if payload_index < len(args) else None
        return True, payload
    return False, None


def _read_tool_exec_flag(tool: str, args: list[str]) -> tuple[str, str] | None:
    """Return (option, program) for a read-only tool's program-running flag."""
    flags = _READ_TOOL_EXEC_FLAGS[tool]
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            break
        option, payload = _split_option(token)
        matched = option if option in flags else None
        if tool == "man" and token.startswith(("-P", "-H")) and len(token) > 2:
            matched, payload = token[:2], token[2:]
        if matched:
            if payload is None and index + 1 < len(args):
                payload = args[index + 1]
            # This option owns its program argument regardless of spelling.
            # The real binaries execute a payload beginning with '-' rather
            # than reparsing it as one of the tool's later options.
            if payload:
                return matched, payload
            index += 2 if payload is not None and "=" not in token else 1
            continue

        if option in _READ_TOOL_LONG_OPTIONS_WITH_ARG[tool] and payload is None:
            index += 2
            continue

        # In a short bundle, the first argument-taking option owns the rest of
        # the token, or the following token when it occurs last.
        if token.startswith("-") and not token.startswith("--") and len(token) > 1:
            for short_index, char in enumerate(token[1:], start=1):
                if char in _READ_TOOL_SHORT_OPTIONS_WITH_ARG[tool]:
                    index += 2 if short_index == len(token) - 1 else 1
                    break
            else:
                index += 1
            continue
        index += 1
    return None


def _execution_flag_findings(command: str):
    """Yield scoped execution mechanisms and any executable payloads."""
    for segment in _iter_top_level_shell_segments(command):
        for start, _, word in _iter_shell_command_word_spans(segment):
            executable = _deobfuscate_shell_word_for_detection(word)
            tokens = _shell_segment_tokens(segment, start)
            executable_name = os.path.basename(executable).lower()
            family = _interpreter_family(executable)
            is_program_bearing = (
                family is not None or executable_name in _READ_TOOL_EXEC_FLAGS
            )
            if tokens is None:
                if is_program_bearing:
                    yield (_MALFORMED_EXEC_DESCRIPTION, None)
                continue
            if not tokens:
                continue
            if family:
                flag = _interpreter_exec_flag(family, tokens[1:])
                if flag:
                    yield ("script execution via -e/-c flag", None)
                    continue
                if any(token.startswith("<<") for token in tokens[1:]):
                    yield ("script execution via heredoc", None)
                    continue
            if executable_name in {"bash", "sh", "zsh", "ksh"}:
                found, payload = _bash_exec_payload(tokens[1:])
                if found:
                    yield ("shell command via -c/-lc flag", payload)
            tool = executable_name
            if tool in _READ_TOOL_EXEC_FLAGS:
                finding = _read_tool_exec_flag(tool, tokens[1:])
                if finding:
                    option, payload = finding
                    yield (f"arbitrary program execution via {tool} {option}", payload)


def _skip_shell_whitespace(command: str, pos: int) -> int:
    while pos < len(command) and command[pos].isspace():
        pos += 1
    return pos


def _scan_dollar_paren_end(command: str, start: int) -> int | None:
    """Return the offset after a balanced ``$(...)`` command substitution."""
    depth = 1
    quote: str | None = None
    i = start + 2
    while i < len(command):
        ch = command[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < len(command):
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "\\" and i + 1 < len(command):
            i += 2
            continue
        if command.startswith("$(", i):
            depth += 1
            i += 2
            continue
        if ch == ")":
            depth -= 1
            i += 1
            if depth == 0:
                return i
            continue
        i += 1
    return None


def _scan_backtick_end(command: str, start: int) -> int | None:
    i = start + 1
    while i < len(command):
        if command[i] == "\\" and i + 1 < len(command):
            i += 2
            continue
        if command[i] == "`":
            return i + 1
        i += 1
    return None


def _read_shell_word(command: str, pos: int) -> tuple[int, int, str]:
    """Read one shell word without executing expansions."""
    start = _skip_shell_whitespace(command, pos)
    i = start
    quote: str | None = None
    while i < len(command):
        ch = command[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < len(command):
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "\\" and i + 1 < len(command):
            i += 2
            continue
        if command.startswith("$(", i):
            end = _scan_dollar_paren_end(command, i)
            if end is None:
                i += 2
            else:
                i = end
            continue
        if command.startswith("${", i):
            end = command.find("}", i + 2)
            if end == -1:
                i += 2
            else:
                i = end + 1
            continue
        if ch == "`":
            end = _scan_backtick_end(command, i)
            if end is None:
                i += 1
            else:
                i = end
            continue
        if ch.isspace() or ch in ";&|":
            break
        i += 1
    return (start, i, command[start:i])


def _strip_optional_shell_quotes(word: str) -> str:
    if len(word) >= 2 and word[0] == word[-1] and word[0] in ("'", '"'):
        return word[1:-1]
    return word


def _is_simple_shell_literal(value: str) -> bool:
    return bool(value and _SIMPLE_SHELL_LITERAL_RE.fullmatch(value))


def _literal_command_substitution_output(script: str) -> str | None:
    """Resolve tiny literal command substitutions without executing a shell."""
    try:
        tokens = shlex.split(script, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None

    command = tokens[0].lower()
    args = tokens[1:]
    if command == "echo":
        while args and re.fullmatch(r"-[nEe]+", args[0]):
            args = args[1:]
        if len(args) == 1 and _is_simple_shell_literal(args[0]):
            return args[0]
        return None

    if command == "printf":
        if len(args) == 1 and _is_simple_shell_literal(args[0]):
            return args[0]
        if (
            len(args) == 2
            and args[0] == "%s"
            and _is_simple_shell_literal(args[1])
        ):
            return args[1]
    return None


def _replace_simple_command_substitutions(word: str) -> str:
    chars: list[str] = []
    i = 0
    while i < len(word):
        if word.startswith("$(", i):
            end = _scan_dollar_paren_end(word, i)
            if end is not None:
                replacement = _literal_command_substitution_output(word[i + 2:end - 1])
                if replacement is not None:
                    chars.append(replacement)
                    i = end
                    continue
        if word[i] == "`":
            end = _scan_backtick_end(word, i)
            if end is not None:
                replacement = _literal_command_substitution_output(word[i + 1:end - 1])
                if replacement is not None:
                    chars.append(replacement)
                    i = end
                    continue
        chars.append(word[i])
        i += 1
    return "".join(chars)


def _replace_simple_shell_expansions(word: str) -> str:
    word = _replace_simple_command_substitutions(word)
    word = _PARAM_REPLACEMENT_RE.sub(lambda match: match.group("replacement"), word)
    return _PARAM_DEFAULT_RE.sub(lambda match: match.group("default"), word)


def _strip_shell_word_syntax(word: str) -> str:
    chars: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(word):
        ch = word[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < len(word):
                chars.append(word[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
                i += 1
                continue
            chars.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "\\" and i + 1 < len(word):
            chars.append(word[i + 1])
            i += 2
            continue
        chars.append(ch)
        i += 1
    return "".join(chars)


def _deobfuscate_shell_word_for_detection(word: str) -> str:
    """Approximate how shell syntax can spell a command word.

    This is intentionally narrow and non-executing: it only collapses shell
    quoting/escaping plus simple literal command substitutions that appear in
    the command word itself.
    """
    deobfuscated = word
    for _ in range(2):
        previous = deobfuscated
        deobfuscated = _replace_simple_shell_expansions(deobfuscated)
        deobfuscated = _strip_shell_word_syntax(deobfuscated)
        if deobfuscated == previous:
            break
    return deobfuscated


def _iter_shell_command_starts(command: str):
    starts = [0]

    def scan(start: int, end: int) -> None:
        quote: str | None = None
        i = start
        while i < end:
            ch = command[i]
            if quote == "'":
                if ch == "'":
                    quote = None
                i += 1
                continue
            if quote == '"':
                if ch == "\\" and i + 1 < end:
                    i += 2
                    continue
                if ch == '"':
                    quote = None
                    i += 1
                    continue
                if command.startswith("$(", i):
                    nested_end = _scan_dollar_paren_end(command, i)
                    starts.append(i + 2)
                    scan(i + 2, nested_end - 1 if nested_end is not None else end)
                    i = nested_end if nested_end is not None else end
                    continue
                if ch == "`":
                    nested_end = _scan_backtick_end(command, i)
                    starts.append(i + 1)
                    scan(i + 1, nested_end - 1 if nested_end is not None else end)
                    i = nested_end if nested_end is not None else end
                    continue
                i += 1
                continue
            if ch in ("'", '"'):
                quote = ch
                i += 1
                continue
            if ch == "\\" and i + 1 < end:
                i += 2
                continue
            if command.startswith("$(", i):
                nested_end = _scan_dollar_paren_end(command, i)
                starts.append(i + 2)
                scan(i + 2, nested_end - 1 if nested_end is not None else end)
                i = nested_end if nested_end is not None else end
                continue
            if ch == "`":
                nested_end = _scan_backtick_end(command, i)
                starts.append(i + 1)
                scan(i + 1, nested_end - 1 if nested_end is not None else end)
                i = nested_end if nested_end is not None else end
                continue
            if ch in ("(", "{"):
                starts.append(i + 1)
            elif ch in ";\n":
                starts.append(i + 1)
            elif ch in "&|":
                repeated = i + 1 < end and command[i + 1] == ch
                starts.append(i + 2 if repeated else i + 1)
                if repeated:
                    i += 1
            i += 1

    scan(0, len(command))

    seen: set[int] = set()
    for start in starts:
        start = _skip_shell_whitespace(command, start)
        if start < len(command) and start not in seen:
            seen.add(start)
            yield start


def _mark_command_starts(command: str) -> str:
    """Insert a newline before each real (quote-aware) command start.

    ``\\n`` is already a ``_CMDPOS`` separator, so this rewrites subshell
    ``(cmd)`` and brace-group ``{ cmd; }`` openers — which the flat pattern
    class deliberately omits — into a form the anchored hardline/dangerous
    patterns recognize, WITHOUT the quoted-prose false positives that adding
    ``(`` / ``{`` to ``_CMDPOS`` would cause. Starts inside quotes are never
    produced by ``_iter_shell_command_starts``, so quoted arguments such as
    ``--title "block (reboot)"`` are left exactly as-is.
    """
    # Collect the (whitespace-skipped) start offsets, drop 0 (already anchored
    # by ``^``), and splice a newline in front of each — right-to-left so the
    # earlier offsets stay valid as we mutate.
    offsets = sorted(o for o in _iter_shell_command_starts(command) if o > 0)
    if not offsets:
        return command
    # Build once instead of repeatedly slicing and copying the full command for
    # every segment (quadratic at 10k+ compound-command segments).
    parts: list[str] = []
    previous = 0
    for offset in offsets:
        parts.extend((command[previous:offset], "\n"))
        previous = offset
    parts.append(command[previous:])
    return "".join(parts)


def _mask_quoted_newlines(command: str) -> str:
    """Replace raw newlines inside single/double quotes with a space.

    Detection-only rewrite. A newline inside a quoted string is DATA to the
    shell — part of the argument, not a command separator — yet the flat
    ``_CMDPOS`` start-position class treats every raw ``\\n`` as a command
    start. That made any multi-line quoted argument (``hermes send`` message
    bodies, ``git commit -m`` messages, heredoc text) trip the hardline
    blocklist when a data line began with e.g. ``sudo reboot``.

    Quote tracking mirrors ``_iter_shell_command_starts``: single quotes are
    literal until the closing quote; inside double quotes a backslash escapes
    the next character. Real command boundaries are unaffected: unquoted
    newlines pass through untouched, ``$(``/backtick remain ``_CMDPOS``
    anchors independent of newlines, and ``_mark_command_starts`` still
    re-inserts newlines at every genuine quote-aware command start. An
    unclosed quote absorbs following newlines exactly as the shell would
    (the quoted word continues across the line break), so masking them
    cannot hide a runnable command.
    """
    if "\n" not in command:
        return command
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < len(command):
                out.append(command[i:i + 2])
                i += 2
                continue
            if ch == quote:
                quote = None
            out.append(" " if ch == "\n" else ch)
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "\\" and i + 1 < len(command):
            out.append(command[i:i + 2])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _iter_shell_command_word_spans(command: str):
    """Yield command-position words that may be executable names."""
    for command_start in _iter_shell_command_starts(command):
        pos = command_start
        prefix_words = 0
        skip_wrapper_options = False
        skip_next_wrapper_arg = False
        while prefix_words < 12:
            word_start, word_end, word = _read_shell_word(command, pos)
            if word_start == word_end:
                break
            deobfuscated = _deobfuscate_shell_word_for_detection(word)
            lower_word = deobfuscated.lower()
            if skip_next_wrapper_arg:
                skip_next_wrapper_arg = False
                pos = word_end
                prefix_words += 1
                continue
            if skip_wrapper_options and lower_word.startswith("-"):
                option_name = lower_word.split("=", 1)[0]
                skip_next_wrapper_arg = (
                    "=" not in lower_word
                    and option_name in _SUDO_OPTIONS_WITH_ARG
                )
                pos = word_end
                prefix_words += 1
                continue

            yield (word_start, word_end, word)
            prefix_words += 1

            if lower_word in _COMMAND_WRAPPER_WORDS:
                skip_wrapper_options = lower_word in {"sudo", "env"}
                pos = word_end
                continue
            if _ENV_ASSIGNMENT_RE.fullmatch(deobfuscated):
                skip_wrapper_options = False
                pos = word_end
                continue
            break


def _command_detection_variants(command: str):
    # Mask quoted newlines BEFORE normalization: normalization strips
    # backslash-escapes (\" -> ") and empty-string pairs (""), which would
    # corrupt quote tracking — e.g. `echo "a\""` normalizes to `echo "a` (an
    # unterminated quote), so masking the normalized text could swallow a
    # REAL unquoted newline separator that follows. The raw command carries
    # faithful shell quote state.
    normalized = _normalize_command_for_detection(_mask_quoted_newlines(command))
    # Quote-aware grep parsing hides only structurally identified pattern
    # operands. Malformed/ambiguous input remains byte-for-byte intact.
    grep_safe, _ = _grep_safe_detection_variant(normalized)
    seen = {grep_safe}
    yield grep_safe
    # Program-bearing options are parsed in their owning command's context.
    # Surfacing only their payload lets the hardline floor inspect the command
    # that will actually run without promoting similar flags or quoted prose.
    pending = [normalized]
    while pending:
        variant = pending.pop()
        for _, payload in _execution_flag_findings(variant):
            if payload and payload not in seen:
                seen.add(payload)
                yield payload
                # A payload can begin with an option-looking program and then
                # invoke a hardline command after a separator. Mark its real
                # command starts just as we do for the outer command.
                marked_payload = _mark_command_starts(payload)
                if marked_payload != payload and marked_payload not in seen:
                    seen.add(marked_payload)
                    yield marked_payload
                pending.append(payload)
    # Subshell `(cmd)` and brace-group `{ cmd; }` openers put `cmd` at a real
    # command position, but the flat `_CMDPOS`-anchored patterns can't see it:
    # their start-position class deliberately omits `(`/`{` because a bare
    # regex cannot tell `(reboot)` (real subshell) from `--title "(reboot)"`
    # (quoted prose) — adding them there regresses ordinary quoted arguments.
    # Instead, reconstruct the command with a newline (already a `_CMDPOS`
    # separator) inserted at each command start the QUOTE-AWARE tokenizer
    # found. Openers inside quotes never yield a start, so quoted prose is
    # untouched, while `(reboot)` / `{ shutdown -h now; }` now anchor. This
    # covers every `_CMDPOS` rule (shutdown/reboot/init/systemctl/telinit and
    # the rm root/home/system floor) in one place.
    marked = _mark_command_starts(grep_safe)
    if marked != grep_safe and marked not in seen:
        seen.add(marked)
        yield marked
    # Shell quoting/escaping can spell a dangerous executable name in pieces
    # (for example r\m or r''m). Keep that deobfuscation scoped to command
    # words so similarly shaped arguments do not become false positives.
    for word_start, word_end, word in _iter_shell_command_word_spans(normalized):
        deobfuscated = _deobfuscate_shell_word_for_detection(word)
        if not deobfuscated or deobfuscated == word:
            continue
        variant = normalized[:word_start] + deobfuscated + normalized[word_end:]
        if variant in seen:
            continue
        seen.add(variant)
        yield variant



def detect_hardline_command(command: str) -> tuple:
    """Check if a command matches hardline blocklist patterns.

    Hardline patterns are NEVER bypassable, even in YOLO mode.

    Returns:
        (is_hardline, description) or (False, None)
    """
    if _command_parser_limit_exceeded(command):
        return (True, _PARSER_LIMIT_DESCRIPTION)
    normalized = _normalize_command_for_detection(command)
    _, malformed_grep = _grep_safe_detection_variant(normalized)
    if malformed_grep:
        return (True, _MALFORMED_EXEC_DESCRIPTION)
    for command_variant in _command_detection_variants(command):
        variant_lower = command_variant.lower()
        for pattern_re, description in HARDLINE_PATTERNS_COMPILED:
            if pattern_re.search(variant_lower):
                return (True, description)
    return (False, None)


# ---------------------------------------------------------------------------
# niya additions. niya runs as root, so besides the Hermes floor it refuses
# commands that would stop niya itself or lock the owner out of the VPS.
# ---------------------------------------------------------------------------

_SERVICE_VERBS = r'(?:stop|disable|mask|kill)'
# Plain words between a command and its target; stays on one command line.
_ARGS = r'(?:[^\s;|&]+[ \t]+)*'
_END = r'(?:\s|$|[;|&)`])'

LOCKOUT_PATTERNS = [
    (_CMDPOS + r'systemctl[ \t]+' + _ARGS + _SERVICE_VERBS + r'[ \t]+' + _ARGS + r'(?:ssh|sshd)(?:\.service|\.socket)?' + _END, "stopping or disabling the SSH service"),
    (_CMDPOS + r'service[ \t]+(?:ssh|sshd)[ \t]+stop\b', "stopping the SSH service"),
    (_CMDPOS + r'(?:ip6?tables|iptables-legacy|iptables-nft)[ \t]+' + _ARGS + r'(?:-F|--flush)' + _END, "flushing all firewall rules"),
    (_CMDPOS + r'(?:ip6?tables|iptables-legacy|iptables-nft)[ \t]+' + _ARGS + r'(?:-P|--policy)[ \t]+input[ \t]+(?:drop|reject)\b', "default-deny on all inbound traffic"),
    (_CMDPOS + r'nft[ \t]+flush[ \t]+ruleset\b', "flushing all nftables rules"),
    (_CMDPOS + r'rm[ \t]+' + _ARGS + r'\S*(?:authorized_keys|sshd_config)\b', "deleting SSH login configuration"),
    (_CMDPOS + r'(?::[ \t]*)?>[ \t]*\S*(?:authorized_keys|sshd_config)\b', "truncating SSH login configuration"),
    (_CMDPOS + r'(?:truncate|shred)[ \t]+' + _ARGS + r'\S*(?:authorized_keys|sshd_config)\b', "truncating SSH login configuration"),
    (_CMDPOS + r'cp[ \t]+' + _ARGS + r'/dev/null[ \t]+\S*(?:authorized_keys|sshd_config)\b', "truncating SSH login configuration"),
    (_CMDPOS + r'passwd[ \t]+(?:-l|--lock)[ \t]+root\b', "locking the root account"),
    (_CMDPOS + r'usermod[ \t]+' + _ARGS + r'(?:-L|--lock)[ \t]+' + _ARGS + r'root\b', "locking the root account"),
]


def _path_forms(path: Path) -> list[str]:
    forms = [str(path)]
    home = Path(os.path.expanduser("~"))
    try:
        forms.append("~/" + str(path.relative_to(home)))
    except ValueError:
        pass
    return forms


def protected_patterns(protected_dirs: Iterable[Path], service: str = "niya") -> list[tuple[str, str]]:
    """Rules that keep niya from deleting its own code/data or stopping its own service."""
    svc = re.escape(service)
    patterns = [
        (_CMDPOS + r'systemctl[ \t]+' + _ARGS + _SERVICE_VERBS + r'[ \t]+' + _ARGS + svc + r'(?:\.service)?' + _END, f"stopping or disabling the {service} service itself"),
        (_CMDPOS + r'(?:pkill|killall)[ \t]+' + _ARGS + r'\S*' + svc + r'\b', f"killing {service} itself"),
        (_CMDPOS + r'kill[ \t]+[^\n]*\$\([ \t]*pgrep[^\n)]*' + svc, f"killing {service} itself"),
    ]
    forms: list[str] = []
    for directory in protected_dirs:
        resolved = Path(directory).expanduser()
        if str(resolved) not in ("", "/") and resolved.is_absolute():
            forms.extend(_path_forms(resolved))
    if forms:
        alt = "|".join(re.escape(f) + r"(?:/[^\s'\";|&)`]*)?" for f in sorted(set(forms), key=len, reverse=True))
        patterns += [
            (_CMDPOS + r'rm[ \t]+' + _ARGS + _hardline_rm_path(alt), f"deleting {service}'s own code or data"),
            (_CMDPOS + r'(?:truncate|shred)[ \t]+' + _ARGS + r'["\']?(?:' + alt + r')', f"wiping {service}'s own data"),
            (_CMDPOS + r'(?::[ \t]*)?>[ \t]*["\']?(?:' + alt + r')', f"truncating {service}'s own data"),
        ]
    return patterns


class Hardline:
    """The Hermes floor plus extra (pattern, description) rules, checked on the same detection variants."""

    def __init__(self, extra: Iterable[tuple[str, str]] = ()) -> None:
        self._extra = [(re.compile(pattern, _RE_FLAGS), description) for pattern, description in extra]

    def check(self, command: str) -> str | None:
        blocked, description = detect_hardline_command(command)
        if blocked:
            return description
        if not self._extra:
            return None
        for variant in _command_detection_variants(command):
            lowered = variant.lower()
            for pattern, description in self._extra:
                if pattern.search(lowered):
                    return description
        return None


def default_hardline(protected_dirs: Iterable[Path] = (), service: str = "niya") -> Hardline:
    return Hardline([*LOCKOUT_PATTERNS, *protected_patterns(protected_dirs, service)])
