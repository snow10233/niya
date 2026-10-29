import shutil
import sqlite3
import sys

import httpx

from niya.core.config import Settings
from niya.core.db import Database
from niya.llm.choice import CLAUDE, current_choice
from niya.llm.claude_cli import auth_status, bundled_cli, cli_version, describe_auth
from niya.llm.openrouter import OpenRouterClient


def sqlite_has_trigram() -> bool:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(content, tokenize='trigram')")
        conn.execute("INSERT INTO t(content) VALUES ('設定檔在這裡')")
        return conn.execute("SELECT count(*) FROM t WHERE t MATCH '\"設定檔\"'").fetchone()[0] == 1
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


async def run_doctor(settings: Settings) -> int:
    checks: list[tuple[str, bool, str]] = [
        ("Python ≥ 3.12", sys.version_info >= (3, 12), sys.version.split()[0]),
        ("SQLite FTS5 trigram", sqlite_has_trigram(), sqlite3.sqlite_version),
        ("bash", shutil.which("bash") is not None, shutil.which("bash") or "找不到"),
        ("ripgrep（rg）", shutil.which("rg") is not None, shutil.which("rg") or "找不到，search_files 會退回 grep"),
        ("資料目錄", True, str(settings.home)),
    ]
    db = Database(settings.db_path)
    try:
        choice, source = current_choice(db, settings)
    finally:
        db.close()
    checks.append(("模型", True, f"{choice}（{source}）"))
    checks.append(("記憶 embedding", bool(settings.api_key), f"{settings.embedding_model}（{settings.embedding_dimensions} 維）" if settings.api_key else "沒有 OPENROUTER_API_KEY，記憶只能用全文搜尋"))
    if choice.provider == CLAUDE:
        cli = bundled_cli()
        checks.append(("Claude Code", cli is not None, f"{cli_version()} {cli}".strip() if cli else "找不到執行檔"))
        if cli:
            logged_in, detail = describe_auth(await auth_status(settings), settings)
            checks.append(("Claude 登入", logged_in, detail))
    else:
        checks.append(("OPENROUTER_API_KEY", bool(settings.api_key), "已設定" if settings.api_key else "未設定"))
        if settings.api_key:
            client = OpenRouterClient(settings.api_key, base_url=settings.base_url)
            try:
                length = await client.context_length(choice.model)
                checks.append(("模型 context_length", length is not None, f"{length:,}" if length else "OpenRouter 上找不到這個模型"))
            except httpx.HTTPError as exc:
                checks.append(("模型 context_length", False, f"查詢失敗：{type(exc).__name__}"))
            finally:
                await client.aclose()

    ok = True
    for name, passed, detail in checks:
        ok &= passed or name.startswith(("ripgrep", "記憶"))
        print(f"{'✓' if passed else '✗'} {name}：{detail}")
    return 0 if ok else 1
