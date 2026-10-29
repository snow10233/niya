"""text_to_speech: turn text into an mp3 with MiniMax (t2a_v2), to send the owner as a voice clip.

Request shape follows NousResearch/hermes-agent (MIT) tools/tts_tool.py@222465d84 (_generate_minimax_tts).
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from niya.core.media import MediaStore
from niya.llm.openrouter import CallRecord, Usage
from niya.tools.registry import Tool, ToolContext, ToolError

ENDPOINTS = {"global": "https://api.minimax.io/v1/t2a_v2", "cn": "https://api.minimaxi.com/v1/t2a_v2"}
DEFAULT_MODEL = "speech-2.8-hd"
DEFAULT_VOICE = "female-shaonv"
MAX_CHARS = 10_000
EMOTIONS = ("happy", "sad", "angry", "fearful", "disgusted", "surprised", "calm", "fluent", "whisper")

DESCRIPTION = (
    "把文字念成語音（mp3），存在本機。要給主人聽就用 discord 工具的 send 附檔。"
    "文字照念出來的樣子寫：數字、日期、符號寫成要念的字（例如「3/4」寫成「三月四日」或「四分之三」），破音字挑只有一種讀法的寫法（「什麼」不寫「甚麼」）。"
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": f"要念的文字（最多 {MAX_CHARS:,} 字）"},
        "emotion": {"type": "string", "enum": list(EMOTIONS), "description": "語氣（不填就讓模型自己判斷）"},
        "speed": {"type": "number", "description": "語速，0.5–2（預設 1）", "minimum": 0.5, "maximum": 2},
    },
    "required": ["text"],
}


@dataclass(frozen=True)
class Voice:
    model: str = DEFAULT_MODEL
    voice_id: str = DEFAULT_VOICE
    region: str = "global"
    group_id: str = ""


def tts_tools(api_key: str, voice: Voice, http: httpx.AsyncClient, media: MediaStore, *, recorder: Callable[[CallRecord], None] | None = None) -> list[Tool]:
    url = ENDPOINTS[voice.region] + (f"?GroupId={voice.group_id}" if voice.group_id else "")

    async def text_to_speech(args: dict[str, Any], ctx: ToolContext) -> str:
        text = str(args.get("text") or "").strip()
        if not text:
            raise ToolError("text 不能是空的。")
        if len(text) > MAX_CHARS:
            raise ToolError(f"一次最多 {MAX_CHARS:,} 字，請分段。")
        setting: dict[str, Any] = {"voice_id": voice.voice_id, "speed": min(max(float(args.get("speed") or 1), 0.5), 2.0), "vol": 1, "pitch": 0}
        if args.get("emotion") in EMOTIONS:
            setting["emotion"] = args["emotion"]
        body = {
            "model": voice.model,
            "text": text,
            "stream": False,
            "language_boost": "auto",
            "voice_setting": setting,
            "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 1},
        }
        started = time.monotonic()
        try:
            response = await http.post(url, json=body, headers={"Authorization": f"Bearer {api_key}"}, timeout=120)
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            record(text, started, error=f"{type(exc).__name__}: {exc}")
            raise ToolError(f"連不上 MiniMax：{type(exc).__name__}") from None
        status = (data.get("base_resp") or {}).get("status_code", -1)
        audio = (data.get("data") or {}).get("audio") or ""
        if response.status_code >= 400 or status != 0 or not audio:
            message = (data.get("base_resp") or {}).get("status_msg") or response.text[:200]
            record(text, started, error=message)
            raise ToolError(f"MiniMax 沒有產生語音（{status}）：{message}")
        record(text, started)
        path = media.save("tts", bytes.fromhex(audio), ".mp3", now=ctx.now())
        length = (data.get("extra_info") or {}).get("audio_length")
        return f"語音做好了：{path}" + (f"（{length / 1000:.1f} 秒）" if isinstance(length, (int, float)) else "")

    def record(text: str, started: float, error: str | None = None) -> None:
        if recorder:
            usage = Usage(prompt_tokens=len(text))  # MiniMax bills by characters
            recorder(CallRecord(task="tts", model=f"minimax:{voice.model}", usage=usage, latency_ms=int((time.monotonic() - started) * 1000), ok=error is None, error=error))

    return [Tool(name="text_to_speech", description=DESCRIPTION, parameters=PARAMETERS, handler=text_to_speech, toolset="tts", timeout_s=150)]
