"""Full-text search over the raw record with SQLite's trigram tokenizer.

Trigram matching finds any substring of three or more characters, which covers Chinese (no word
boundaries) and English alike. Chinese runs are cut into overlapping 3-grams OR-ed together, so a
question phrased differently from the original still finds it, ranked by how many pieces match.
When that finds nothing (short terms, or a sentence whose 3-grams never occur verbatim), a LIKE scan on
2-grams and short terms ranks recent messages by how many of them they contain.
"""

import re
from dataclasses import dataclass

from niya.core.db import Database

MAX_QUERY_CHARS = 200
MAX_TERMS = 32
MAX_FALLBACK_TERMS = 16
FALLBACK_SCAN = 500
_TOKEN = re.compile(r"\w+")


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return 0x3400 <= cp <= 0x9FFF or 0x3040 <= cp <= 0x30FF or 0xAC00 <= cp <= 0xD7AF or 0x20000 <= cp <= 0x2A6DF


def _quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def query_terms(text: str) -> tuple[list[str], list[str]]:
    """Trigram-searchable terms, plus 2-grams and short terms for the LIKE fallback."""
    terms: list[str] = []
    fallback: list[str] = []
    for token in _TOKEN.findall(text[:MAX_QUERY_CHARS]):
        cjk = any(_is_cjk(ch) for ch in token)
        if len(token) < 3:
            if len(token) == 2 or cjk:
                fallback.append(token)
        elif cjk:
            terms.extend(token[i : i + 3] for i in range(len(token) - 2))
            fallback.extend(token[i : i + 2] for i in range(len(token) - 1))
        else:
            terms.append(token)
    unique = list(dict.fromkeys(t.lower() for t in terms))
    return unique[:MAX_TERMS], list(dict.fromkeys(t.lower() for t in fallback))[:MAX_FALLBACK_TERMS]


def match_expression(terms: list[str]) -> str | None:
    return " OR ".join(_quote(t) for t in terms) or None


@dataclass(frozen=True)
class MessageHit:
    id: int
    session_id: str
    ts: float
    speaker: str | None
    role: str
    snippet: str


def _flatten(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _like_snippet(content: str, term: str, width: int = 40) -> str:
    at = content.lower().find(term.lower())
    if at < 0:
        return _flatten(content[: width * 2])
    start, end = max(0, at - width), min(len(content), at + len(term) + width)
    return ("…" if start else "") + _flatten(content[start:at]) + f"«{content[at:at + len(term)]}»" + _flatten(content[at + len(term):end]) + ("…" if end < len(content) else "")


def search_messages(
    db: Database,
    query: str,
    *,
    since: float | None = None,
    until: float | None = None,
    exclude_session: str | None = None,
    limit: int = 10,
) -> list[MessageHit]:
    terms, fallback = query_terms(query)
    where = ["m.ts >= ?", "m.ts < ?", "m.session_id <> ?"]
    params: list[object] = [since if since is not None else float("-inf"), until if until is not None else float("inf"), exclude_session or ""]
    if expression := match_expression(terms):
        rows = db.query(
            "SELECT m.id, m.session_id, m.ts, m.speaker, m.role, snippet(messages_tri, 0, '«', '»', '…', 24) AS snip"
            " FROM messages_tri JOIN messages m ON m.id = messages_tri.rowid"
            f" WHERE messages_tri MATCH ? AND {' AND '.join(where)} ORDER BY bm25(messages_tri), m.id DESC LIMIT ?",
            (expression, *params, limit),
        )
        hits = [MessageHit(r["id"], r["session_id"], r["ts"], r["speaker"], r["role"], _flatten(r["snip"])) for r in rows]
        if hits:
            return hits
    if not fallback:
        return []
    like = " OR ".join("m.content LIKE ? ESCAPE '\\'" for _ in fallback)
    escaped = ["%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for t in fallback]
    rows = db.query(
        f"SELECT m.id, m.session_id, m.ts, m.speaker, m.role, m.content FROM messages m"
        f" WHERE m.role <> 'tool' AND m.content IS NOT NULL AND ({like}) AND {' AND '.join(where)} ORDER BY m.ts DESC LIMIT ?",
        (*escaped, *params, FALLBACK_SCAN),
    )
    scored = []
    for r in rows:
        lowered = r["content"].lower()
        found = [t for t in fallback if t in lowered]
        best = max(found, key=len) if found else fallback[0]
        scored.append((len(found), r["ts"], MessageHit(r["id"], r["session_id"], r["ts"], r["speaker"], r["role"], _like_snippet(r["content"], best))))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [hit for _, _, hit in scored[:limit]]
