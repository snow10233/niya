"""web_search and web_extract."""

from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlparse

import httpx

from niya.tools.registry import Tool, ToolContext, ToolError, ToolText
from niya.tools.url_safety import sensitive_query_param_name
from niya.tools.web_providers import ExtractProvider, SearchProvider

MAX_URLS = 5
MAX_DOC_CHARS = 15_000

SEARCH_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "搜尋字詞"},
        "limit": {"type": "integer", "description": "最多幾筆結果（預設 5，最多 10）", "minimum": 1, "maximum": 10},
    },
    "required": ["query"],
}
EXTRACT_PARAMETERS = {
    "type": "object",
    "properties": {
        "urls": {"type": "array", "items": {"type": "string"}, "description": f"要讀的網址，最多 {MAX_URLS} 個", "maxItems": MAX_URLS},
    },
    "required": ["urls"],
}


def _check_url(url: str, secrets: Iterable[str]) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ToolError(f"只能讀 http(s) 網址：{url}")
    if (name := sensitive_query_param_name(url)) is not None:
        raise ToolError(f"網址裡帶有像是密鑰的參數（{name}），不能送出。")
    if any(secret in url for secret in secrets):
        raise ToolError("網址裡含有 niya 的密鑰，不能送出。")


def web_tools(search: SearchProvider | None, extract: ExtractProvider, *, secrets: Callable[[], Iterable[str]] = lambda: ()) -> list[Tool]:
    async def web_search(args: dict[str, Any], ctx: ToolContext) -> ToolText:
        if search is None:
            raise ToolError("沒有設定搜尋服務（TAVILY_API_KEY、BRAVE_SEARCH_API_KEY 或 web.searxng_url）。")
        query = str(args.get("query") or "").strip()
        if not query:
            raise ToolError("query 不能是空的。")
        limit = min(max(int(args.get("limit") or 5), 1), 10)
        try:
            hits = await search.search(query, limit)
        except httpx.HTTPError as exc:
            raise ToolError(f"{search.name} 搜尋失敗：{type(exc).__name__}: {exc}") from None
        if not hits:
            return ToolText("沒有找到結果。", external=True, source=f"web_search:{search.name}")
        lines = [f"{i}. {h.title}\n   {h.url}\n   {h.description.strip()}" for i, h in enumerate(hits, 1)]
        return ToolText("\n".join(lines), external=True, source=f"web_search:{search.name}")

    async def web_extract(args: dict[str, Any], ctx: ToolContext) -> ToolText:
        urls = args.get("urls")
        if isinstance(urls, str):
            urls = [urls]
        if not isinstance(urls, list) or not urls or not all(isinstance(u, str) for u in urls):
            raise ToolError("urls 必須是網址字串的陣列。")
        if len(urls) > MAX_URLS:
            raise ToolError(f"一次最多讀 {MAX_URLS} 個網址。")
        known = list(secrets())
        for url in urls:
            _check_url(url, known)
        try:
            docs = await extract.extract(urls)
        except httpx.HTTPError as exc:
            raise ToolError(f"{extract.name} 擷取失敗：{type(exc).__name__}: {exc}") from None
        parts = []
        for doc in docs:
            if doc.error:
                parts.append(f"## {doc.url}\n（讀取失敗：{doc.error}）")
                continue
            content = doc.content.strip()
            if len(content) > MAX_DOC_CHARS:
                content = content[:MAX_DOC_CHARS] + f"\n…（內容過長，省略後面 {len(doc.content) - MAX_DOC_CHARS} 字元）"
            parts.append(f"## {doc.title or doc.url}\n{doc.url}\n\n{content or '（沒有文字內容）'}")
        host = urlparse(urls[0]).hostname or "web"
        return ToolText("\n\n".join(parts), external=True, source=f"web_extract:{host}")

    return [
        Tool(name="web_search", description="搜尋網路，回傳標題、網址和摘要。要讀完整內容再用 web_extract。", parameters=SEARCH_PARAMETERS, handler=web_search, toolset="web", parallel_safe=True, timeout_s=60, max_result_chars=12_000),
        Tool(name="web_extract", description=f"讀取網頁（或 PDF 以外的文字網址）的內容，轉成文字回傳。一次最多 {MAX_URLS} 個網址。", parameters=EXTRACT_PARAMETERS, handler=web_extract, toolset="web", parallel_safe=True, timeout_s=120, max_result_chars=40_000),
    ]
