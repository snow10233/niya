"""Search and extract backends.

The result shapes and provider request formats follow
NousResearch/hermes-agent (MIT) agent/web_search_provider.py and
plugins/web/{tavily,searxng,brave_free}@222465d84, rewritten as small async
classes without the Hermes plugin manager.
"""

import html
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Protocol

import httpx

from niya.tools.url_safety import async_is_safe_url, create_ssrf_safe_async_client

MAX_FETCH_BYTES = 5_000_000
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml+xml", "application/javascript")


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    description: str


@dataclass(frozen=True)
class Document:
    url: str
    title: str
    content: str
    error: str | None = None


class SearchProvider(Protocol):
    name: str

    async def search(self, query: str, limit: int) -> list[SearchHit]: ...


class ExtractProvider(Protocol):
    name: str

    async def extract(self, urls: Sequence[str]) -> list[Document]: ...


class Tavily:
    name = "tavily"

    def __init__(self, api_key: str, http: httpx.AsyncClient, base_url: str = "https://api.tavily.com") -> None:
        self.api_key = api_key
        self.http = http
        self.base_url = base_url.rstrip("/")

    async def _post(self, endpoint: str, payload: dict) -> dict:
        response = await self.http.post(f"{self.base_url}/{endpoint}", json={**payload, "api_key": self.api_key}, timeout=60)
        response.raise_for_status()
        return response.json()

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        data = await self._post("search", {"query": query, "max_results": min(limit, 20), "include_raw_content": False, "include_images": False})
        return [SearchHit(r.get("title", ""), r.get("url", ""), r.get("content", "")) for r in data.get("results", [])][:limit]

    async def extract(self, urls: Sequence[str]) -> list[Document]:
        data = await self._post("extract", {"urls": list(urls), "include_images": False})
        docs = [Document(r.get("url", ""), r.get("title", ""), r.get("raw_content") or r.get("content", "")) for r in data.get("results", [])]
        docs += [Document(f.get("url", ""), "", "", f.get("error", "extraction failed")) for f in data.get("failed_results", [])]
        docs += [Document(str(u), "", "", "extraction failed") for u in data.get("failed_urls", [])]
        return docs


class Brave:
    name = "brave"
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str, http: httpx.AsyncClient) -> None:
        self.api_key = api_key
        self.http = http

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        response = await self.http.get(
            self.endpoint,
            params={"q": query, "count": min(limit, 20)},
            headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"},
            timeout=30,
        )
        response.raise_for_status()
        results = (response.json().get("web") or {}).get("results", []) or []
        return [SearchHit(r.get("title", ""), r.get("url", ""), _strip_tags(r.get("description", ""))) for r in results[:limit]]


class SearXNG:
    name = "searxng"

    def __init__(self, base_url: str, http: httpx.AsyncClient) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = http

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        response = await self.http.get(f"{self.base_url}/search", params={"q": query, "format": "json"}, headers={"Accept": "application/json"}, timeout=30)
        response.raise_for_status()
        results = sorted(response.json().get("results", []), key=lambda r: r.get("score", 0), reverse=True)
        return [SearchHit(str(r.get("title", "")), str(r.get("url", "")), str(r.get("content", ""))) for r in results[:limit]]


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre", "blockquote", "table"}
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> tuple[str, str]:
    try:
        import trafilatura  # optional: much better main-content extraction

        text = trafilatura.extract(markup, output_format="markdown", include_links=True) or ""
        title = (trafilatura.extract_metadata(markup).title or "") if text else ""
        if text:
            return title, text
    except ImportError:
        pass
    parser = _TextExtractor()
    parser.feed(markup)
    text = re.sub(r"[ \t]+", " ", "".join(parser.parts))
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return parser.title.strip(), text


def _strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


class NativeExtractor:
    """Fetch pages directly; every connection (including redirects) is checked against private networks."""

    name = "native"

    def __init__(self, http: httpx.AsyncClient | None = None, *, url_check: Callable[[str], Awaitable[bool]] = async_is_safe_url) -> None:
        self.http = http or create_ssrf_safe_async_client(timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (niya agent)"})
        self.url_check = url_check

    async def extract(self, urls: Sequence[str]) -> list[Document]:
        return [await self._fetch(url) for url in urls]

    async def _fetch(self, url: str) -> Document:
        if not await self.url_check(url):
            return Document(url, "", "", "這個網址指向內部或私有網路，不能抓取。")
        try:
            async with self.http.stream("GET", url) as response:
                if response.status_code >= 400:
                    return Document(url, "", "", f"HTTP {response.status_code}")
                kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if kind and not kind.startswith(TEXT_TYPES):
                    return Document(url, "", "", f"不支援的內容類型：{kind}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_FETCH_BYTES:
                        break
                text = body.decode(response.encoding or "utf-8", errors="replace")
        except httpx.HTTPError as exc:
            return Document(url, "", "", f"抓取失敗：{type(exc).__name__}: {exc}")
        except ValueError as exc:
            return Document(url, "", "", f"抓取被擋下：{exc}")
        if "html" in kind or text.lstrip()[:15].lower().startswith(("<!doctype html", "<html")):
            title, content = html_to_text(text)
            return Document(url, title, content)
        return Document(url, "", text)
