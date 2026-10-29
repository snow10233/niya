import json
import tempfile
import unittest
from pathlib import Path

import httpx
from helpers import TZ

from niya.core.media import MediaStore
from niya.llm.openrouter import ToolCall
from niya.tools.registry import ToolContext, ToolRegistry
from niya.tools.tts import Voice, tts_tools


class TtsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.requests, self.records = [], []
        self.reply = {"data": {"audio": b"ID3fake".hex(), "status": 2}, "extra_info": {"audio_length": 2300}, "base_resp": {"status_code": 0, "status_msg": "success"}}

        def handle(request):
            self.requests.append(request)
            return httpx.Response(200, json=self.reply)

        http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(http.aclose)
        voice = Voice(voice_id="my-voice", region="cn", group_id="g1")
        self.registry = ToolRegistry(tts_tools("mm-key", voice, http, MediaStore(self.dir / "media", TZ), recorder=self.records.append))

    async def speak(self, **args):
        [outcome] = await self.registry.execute([ToolCall("c1", "text_to_speech", json.dumps(args))], ToolContext("s", "t", now=lambda: 1_790_000_000.0))
        return outcome

    async def test_audio_is_saved_as_mp3(self):
        outcome = await self.speak(text="早安，主人", emotion="happy", speed=1.2)
        self.assertIn("2.3 秒", outcome.content)
        path = Path(outcome.content.split("：", 1)[1].split("（")[0])
        self.assertEqual((path.suffix, path.read_bytes()), (".mp3", b"ID3fake"))
        [request] = self.requests
        self.assertEqual(str(request.url), "https://api.minimaxi.com/v1/t2a_v2?GroupId=g1")
        self.assertEqual(request.headers["authorization"], "Bearer mm-key")
        body = json.loads(request.content)
        self.assertEqual(body["voice_setting"], {"voice_id": "my-voice", "speed": 1.2, "vol": 1, "pitch": 0, "emotion": "happy"})
        self.assertEqual((self.records[0].task, self.records[0].usage.prompt_tokens, self.records[0].ok), ("tts", 5, True))

    async def test_minimax_errors_are_shown(self):
        self.reply = {"base_resp": {"status_code": 1004, "status_msg": "authentication failed"}}
        outcome = await self.speak(text="嗨")
        self.assertTrue(outcome.is_error)
        self.assertIn("authentication failed", outcome.content)
        self.assertFalse(self.records[0].ok)


if __name__ == "__main__":
    unittest.main()
