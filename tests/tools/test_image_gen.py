import base64
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import httpx
from helpers import TZ
from PIL import Image

from niya.core.media import MediaStore
from niya.llm.openrouter import OpenRouterClient, ToolCall
from niya.tools.image_gen import CodexImages, OpenRouterImages, find_codex, image_tools
from niya.tools.registry import ToolContext, ToolRegistry


def png_bytes(color=(0, 128, 255)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(out, "PNG")
    return out.getvalue()


FAKE_CODEX = """
import json, sys, time
from pathlib import Path
home = Path(sys.argv[1])
(home / "args.json").write_text(json.dumps(sys.argv[2:]))
if "draw" in sys.argv[-1]:
    folder = home / "generated_images" / "session"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "ig_1.png").write_bytes(bytes.fromhex(sys.argv[-1].split("hex=")[1].split()[0]))
    print("saved image")
else:
    print("Not logged in", file=sys.stderr)
    sys.exit(1)
"""


class ImageGenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.media = MediaStore(self.dir / "media", TZ)
        self.ctx = ToolContext("s", "t", now=lambda: 1_790_000_000.0)

    async def call(self, registry, **args):
        [outcome] = await registry.execute([ToolCall("c1", "image_generate", json.dumps(args))], self.ctx)
        return outcome

    def openrouter(self, respond):
        self.requests = []

        def handle(request):
            self.requests.append(json.loads(request.content))
            return httpx.Response(200, json=respond())

        client = OpenRouterClient("sk-or-test", http=httpx.AsyncClient(transport=httpx.MockTransport(handle)), max_retries=0)
        self.addAsyncCleanup(client.aclose)
        return ToolRegistry(image_tools(OpenRouterImages(client, ""), self.media))

    async def test_openrouter_picture_is_saved_and_shown(self):
        picture = png_bytes()
        url = "data:image/png;base64," + base64.b64encode(picture).decode()
        registry = self.openrouter(lambda: {"choices": [{"message": {"content": "", "images": [{"image_url": {"url": url}}]}}]})
        reference = self.dir / "ref.png"
        reference.write_bytes(png_bytes((255, 0, 0)))
        outcome = await self.call(registry, prompt="一隻藍色的貓", aspect="landscape", images=[str(reference)])
        [path] = outcome.images
        self.assertEqual(Path(path).read_bytes(), picture)
        self.assertTrue(path.startswith(str(self.dir / "media" / "generated")))
        self.assertIn("openrouter:openai/gpt-5.4-image-2", outcome.content)
        [body] = self.requests
        self.assertEqual((body["modalities"], body["image_config"]), (["image", "text"], {"aspect_ratio": "16:9"}))
        self.assertEqual([p["type"] for p in body["messages"][0]["content"]], ["text", "image_url"])

    async def test_openrouter_without_a_picture_says_what_the_model_said(self):
        registry = self.openrouter(lambda: {"choices": [{"message": {"content": "我不會畫圖"}}]})
        outcome = await self.call(registry, prompt="貓")
        self.assertTrue(outcome.is_error)
        self.assertIn("我不會畫圖", outcome.content)

    async def test_bad_arguments(self):
        registry = self.openrouter(lambda: {})
        self.assertIn("aspect", (await self.call(registry, prompt="貓", aspect="wide")).content)
        self.assertIn("不是能用的圖片", (await self.call(registry, prompt="貓", images=[str(self.dir / "nope.png")])).content)
        self.assertEqual(self.requests, [])

    def codex(self):
        script = self.dir / "fake_codex.py"
        script.write_text(FAKE_CODEX)
        home = self.dir / "codex-home"
        home.mkdir()
        backend = CodexImages([sys.executable, str(script), str(home)], codex_home=home, env={"PATH": "/usr/bin:/bin", "OPENROUTER_API_KEY": "x"}, secret_names=["OPENROUTER_API_KEY"])
        return ToolRegistry(image_tools(backend, self.media)), home

    async def test_codex_runs_imagegen_and_picks_the_saved_file(self):
        registry, home = self.codex()
        picture = png_bytes((10, 200, 10))
        reference = self.dir / "ref.png"
        reference.write_bytes(png_bytes())
        outcome = await self.call(registry, prompt=f"draw hex={picture.hex()} 一片草地", aspect="portrait", images=[str(reference)])
        self.assertFalse(outcome.is_error, outcome.content)
        self.assertEqual(Path(outcome.images[0]).read_bytes(), picture)
        args = json.loads((home / "args.json").read_text())
        self.assertEqual(args[:4], ["exec", "--skip-git-repo-check", "--sandbox", "read-only"])
        self.assertEqual(args[4:6], ["--image", str(reference)])
        self.assertTrue(args[-1].startswith("$imagegen draw"))
        self.assertIn("1024x1536", args[-1])

    async def test_codex_without_a_picture_shows_why(self):
        registry, _ = self.codex()
        outcome = await self.call(registry, prompt="一片草地")
        self.assertTrue(outcome.is_error)
        self.assertIn("codex login", outcome.content)
        self.assertIn("Not logged in", outcome.content)

    def test_find_codex(self):
        self.assertEqual(find_codex("npx -y @openai/codex"), ["npx", "-y", "@openai/codex"])
        self.assertIsNone(find_codex(path=str(self.dir)) if not Path.home().joinpath(".local/bin/codex").exists() else None)


if __name__ == "__main__":
    unittest.main()
