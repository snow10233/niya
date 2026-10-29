"""image_generate: draw a picture, through OpenRouter or the ChatGPT plan behind the official Codex CLI.

OpenRouter's request shape follows NousResearch/hermes-agent (MIT) plugins/image_gen/openrouter@222465d84.
Hermes reaches the Codex plan by reusing Codex's login token against chatgpt.com directly; niya runs the
official `codex exec` with its `$imagegen` skill instead and picks the file it saves.
The picture comes back as an image the model looks at, so niya can check it before showing the owner.
"""

import asyncio
import base64
import os
import shlex
import shutil
import tempfile
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol

from niya.core.media import IMAGE_SUFFIXES, MediaStore, data_url, image_type, sniff
from niya.llm.openrouter import LLMError, OpenRouterClient
from niya.tools.registry import Tool, ToolContext, ToolError, ToolText
from niya.tools.shell import clean_env, kill_group

ASPECTS = ("square", "landscape", "portrait")
OPENROUTER_ASPECTS = {"square": "1:1", "landscape": "16:9", "portrait": "9:16"}
CODEX_SIZES = {"square": "1024x1024", "landscape": "1536x1024", "portrait": "1024x1536"}
DEFAULT_OPENROUTER_MODEL = "openai/gpt-5.4-image-2"
CODEX_TIMEOUT_S = 600
CODEX_OUTPUT_CHARS = 1500

DESCRIPTION = (
    "畫一張圖，或照參考圖修改。畫好的圖會附在結果裡讓你看，滿意再用 discord 工具的 send 附檔給主人；不滿意就調整描述再畫一次。"
    "prompt 寫清楚主體、風格、構圖和顏色。"
)


class ImageBackend(Protocol):
    label: str
    max_references: int

    async def generate(self, prompt: str, aspect: str, references: list[Path]) -> bytes: ...


class OpenRouterImages:
    max_references = 3

    def __init__(self, client: OpenRouterClient, model: str = DEFAULT_OPENROUTER_MODEL) -> None:
        self.client = client
        self.model = model or DEFAULT_OPENROUTER_MODEL
        self.label = f"openrouter:{self.model}"

    async def generate(self, prompt: str, aspect: str, references: list[Path]) -> bytes:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        content += [{"type": "image_url", "image_url": {"url": url}} for path in references if (url := data_url(str(path)))]
        body = {"model": self.model, "modalities": ["image", "text"], "messages": [{"role": "user", "content": content}], "image_config": {"aspect_ratio": OPENROUTER_ASPECTS[aspect]}}
        try:
            data = await self.client.chat_raw(body, task="image")
        except LLMError as exc:
            raise ToolError(f"OpenRouter 生圖失敗：{exc}") from None
        message = ((data.get("choices") or [{}])[0]).get("message") or {}
        for image in message.get("images") or ():
            url = (image.get("image_url") or {}).get("url", "")
            if url.startswith("data:") and "," in url:
                return base64.b64decode(url.split(",", 1)[1])
            if url.startswith("https://"):
                response = await self.client.http.get(url)
                response.raise_for_status()
                return response.content
        said = message.get("content") if isinstance(message.get("content"), str) else ""
        raise ToolError(f"{self.model} 沒有回傳圖片" + (f"，只說了：{said[:300]}" if said else "（這個模型可能不會畫圖）") + "。")


class CodexImages:
    max_references = 16

    def __init__(self, command: list[str], *, codex_home: Path | None = None, env: Mapping[str, str] | None = None, secret_names: Iterable[str] = (), timeout_s: float = CODEX_TIMEOUT_S) -> None:
        self.command = command
        self.codex_home = codex_home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        self.env = clean_env(env if env is not None else os.environ, secret_names)
        self.timeout_s = timeout_s
        self.label = "codex"

    async def generate(self, prompt: str, aspect: str, references: list[Path]) -> bytes:
        args = [*self.command, "exec", "--skip-git-repo-check", "--sandbox", "read-only"]
        if references:
            args += ["--image", ",".join(str(p) for p in references)]
        args.append(f"$imagegen {prompt}\n\n尺寸 {CODEX_SIZES[aspect]}。只生成一張圖，不要建立或修改任何檔案。")
        started = time.time()
        with tempfile.TemporaryDirectory(prefix="niya-codex-") as cwd:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=self.env, cwd=cwd, start_new_session=True
                )
            except OSError as exc:
                raise ToolError(f"codex 啟動失敗：{exc}") from None
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), self.timeout_s)
            except TimeoutError:
                await kill_group(proc)
                raise ToolError(f"codex 超過 {self.timeout_s:g} 秒還沒畫完，已經停掉。") from None
            except asyncio.CancelledError:
                await kill_group(proc)
                raise
        output = out.decode("utf-8", errors="replace").strip()
        if picture := self._newest(started):
            return picture.read_bytes()
        tail = output[-CODEX_OUTPUT_CHARS:] or "（沒有輸出）"
        hint = "（還沒登入的話，在 VPS 上執行 codex login）" if proc.returncode else ""
        raise ToolError(f"codex 沒有產生圖片（exit {proc.returncode}）{hint}：\n{tail}")

    def _newest(self, since: float) -> Path | None:
        folder = self.codex_home / "generated_images"
        found = [p for p in folder.rglob("*") if p.is_file() and p.stat().st_mtime >= since - 1 and image_type(p)] if folder.is_dir() else []
        return max(found, key=lambda p: p.stat().st_mtime) if found else None


def find_codex(configured: str = "", path: str | None = None) -> list[str] | None:
    if configured:
        return shlex.split(configured)
    search = os.pathsep.join(p for p in ((path if path is not None else os.environ.get("PATH", "")), str(Path.home() / ".local" / "bin")) if p)
    found = shutil.which("codex", path=search)
    return [found] if found else None


def image_tools(backend: ImageBackend, media: MediaStore) -> list[Tool]:
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "要畫什麼"},
            "aspect": {"type": "string", "enum": list(ASPECTS), "description": "比例（預設 square）"},
            "images": {"type": "array", "items": {"type": "string"}, "description": f"參考圖或要修改的圖（本機路徑，最多 {backend.max_references} 張）"},
        },
        "required": ["prompt"],
    }

    async def image_generate(args: dict[str, Any], ctx: ToolContext) -> ToolText:
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            raise ToolError("prompt 不能是空的。")
        aspect = args.get("aspect") or "square"
        if aspect not in ASPECTS:
            raise ToolError(f"aspect 只能是 {'、'.join(ASPECTS)}。")
        references = [Path(str(p)).expanduser() for p in args.get("images") or ()]
        if len(references) > backend.max_references:
            raise ToolError(f"參考圖最多 {backend.max_references} 張。")
        for path in references:
            if not image_type(path):
                raise ToolError(f"{path} 不是能用的圖片（png、jpg、webp、gif）。")
        data = await backend.generate(prompt, aspect, references)
        mime = sniff(data[:16])
        if mime is None:
            raise ToolError(f"{backend.label} 回傳的不是圖片。")
        path = media.save("generated", data, IMAGE_SUFFIXES[mime], now=ctx.now())
        return ToolText(f"畫好了（{backend.label}）：{path}", images=(str(path),))

    return [Tool(name="image_generate", description=DESCRIPTION, parameters=parameters, handler=image_generate, toolset="image", timeout_s=CODEX_TIMEOUT_S + 60)]
