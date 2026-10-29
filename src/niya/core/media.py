"""Pictures niya can see: what the owner sends, screenshots, images it makes.

They live as files under $NIYA_HOME/media; messages only keep the paths, and each render turns a path into
what the model reads (a data URL, scaled down so it fits the provider's limits). The model is assumed to
take images natively; nothing here describes them in words.
"""

import base64
import io
import secrets
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IMAGE_SUFFIXES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
MAX_EDGE = 1568  # past this the model sees no more detail, it only costs more tokens
MAX_ENCODED = 3_750_000  # base64 of this stays under Anthropic's 5 MB per image
MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_TOKENS = 1600  # about what one scaled image costs in context
CACHE_SIZE = 64


def sniff(head: bytes) -> str | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_type(path: Path) -> str | None:
    try:
        with path.open("rb") as f:
            return sniff(f.read(16))
    except OSError:
        return None


def describe(path: Path) -> str:
    from PIL import Image

    with Image.open(path) as image:
        width, height = image.size
    return f"{width}×{height}，{path.stat().st_size / 1024:,.0f} KB"


class MediaStore:
    def __init__(self, root: Path, tz: ZoneInfo) -> None:
        self.root = root
        self.tz = tz

    def path_for(self, kind: str, suffix: str, *, now: float) -> Path:
        folder = self.root / kind
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{datetime.fromtimestamp(now, self.tz):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}{suffix}"

    def save(self, kind: str, data: bytes, suffix: str, *, now: float) -> Path:
        path = self.path_for(kind, suffix, now=now)
        path.write_bytes(data)
        return path


_cache: OrderedDict[tuple[str, int, int], tuple[str, str]] = OrderedDict()


def encoded(path: str) -> tuple[str, str] | None:
    """(media type, base64) of the image at `path`, scaled to fit; None when it is gone or not an image."""
    file = Path(path)
    try:
        stat = file.stat()
    except OSError:
        return None
    key = (path, stat.st_mtime_ns, stat.st_size)
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    mime = image_type(file)
    if mime is None or stat.st_size > MAX_IMAGE_BYTES:
        return None
    data = file.read_bytes()
    try:
        mime, data = _fit(mime, data)
    except Exception:  # a broken file is the same as a missing one
        return None
    _cache[key] = (mime, base64.b64encode(data).decode("ascii"))
    while len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)
    return _cache[key]


def data_url(path: str) -> str | None:
    found = encoded(path)
    return f"data:{found[0]};base64,{found[1]}" if found else None


def _fit(mime: str, data: bytes) -> tuple[str, bytes]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        if max(image.size) <= MAX_EDGE and len(data) * 4 // 3 <= MAX_ENCODED:
            return mime, data
        image = image.convert("RGBA" if image.mode in ("RGBA", "LA", "P") else "RGB")
        image.thumbnail((MAX_EDGE, MAX_EDGE))
        out = io.BytesIO()
        if image.mode == "RGBA":
            image.save(out, "PNG", optimize=True)
            if out.tell() * 4 // 3 <= MAX_ENCODED:
                return "image/png", out.getvalue()
            image = image.convert("RGB")
            out = io.BytesIO()
        image.save(out, "JPEG", quality=85)
        return "image/jpeg", out.getvalue()
