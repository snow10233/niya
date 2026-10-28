import shutil
import sqlite3
import sys

import httpx

from niya.config import Settings
from niya.llm import OpenRouterClient


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
        ("OPENROUTER_API_KEY", bool(settings.api_key), "已設定" if settings.api_key else "未設定"),
        ("模型", bool(settings.model), settings.model or "未設定 agent.model / NIYA_MODEL"),
        ("資料目錄", True, str(settings.home)),
    ]
    if settings.api_key and settings.model:
        client = OpenRouterClient(settings.api_key, base_url=settings.base_url)
        try:
            length = await client.context_length(settings.model)
            checks.append(("模型 context_length", length is not None, f"{length:,}" if length else "OpenRouter 上找不到這個模型"))
        except httpx.HTTPError as exc:
            checks.append(("模型 context_length", False, f"查詢失敗：{type(exc).__name__}"))
        finally:
            await client.aclose()

    ok = True
    for name, passed, detail in checks:
        ok &= passed or name.startswith("ripgrep")
        print(f"{'✓' if passed else '✗'} {name}：{detail}")
    return 0 if ok else 1
