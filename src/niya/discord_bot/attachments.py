"""What the owner attaches in Discord: pictures become images niya sees, other files are saved so niya can
open them (Discord's attachment links expire)."""

import re
from pathlib import Path
from typing import Any

from niya.core.media import IMAGE_SUFFIXES, MediaStore, sniff

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,8}")


async def save_attachments(attachments: list[Any], media: MediaStore, *, now: float) -> tuple[list[str], list[str]]:
    """(lines to add to the message, image paths)."""
    lines: list[str] = []
    images: list[str] = []
    for attachment in attachments:
        name = attachment.filename
        if attachment.size > MAX_ATTACHMENT_BYTES:
            lines.append(f"[附件] {name}（{attachment.size / 1024 / 1024:.0f} MB，太大沒有下載）：{attachment.url}")
            continue
        try:
            data = await attachment.read()
        except Exception as exc:
            lines.append(f"[附件] {name}（下載失敗：{exc}）：{attachment.url}")
            continue
        mime = sniff(data[:16])
        suffix = Path(name).suffix
        path = media.save("inbox", data, IMAGE_SUFFIXES.get(mime or "") or (suffix if _SUFFIX.fullmatch(suffix) else ""), now=now)
        if mime:
            images.append(str(path))
            lines.append(f"[圖片] {name}：{path}")
        else:
            lines.append(f"[附件] {name}：{path}")
    return lines, images
