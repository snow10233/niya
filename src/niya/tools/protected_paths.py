"""Write rules shared by the file tools (the terminal side lives in hardline.py).

niya runs as root, so these rules only cover what would hurt niya itself or
lock the owner out: niya's own data and config, removing existing SSH keys,
and saving an sshd_config that sshd would reject.
"""

import asyncio
import shutil
from collections.abc import Iterable
from pathlib import Path

SSHD_MAIN_CONFIG = Path("/etc/ssh/sshd_config")


def _key_lines(text: str) -> set[str]:
    return {line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")}


def is_sshd_config(path: Path) -> bool:
    return path.name == "sshd_config" or (path.parent.name == "sshd_config.d" and path.suffix == ".conf")


class ProtectedPaths:
    def __init__(self, protected_dirs: Iterable[Path]) -> None:
        self.dirs = [Path(d).expanduser().resolve() for d in protected_dirs]

    def check_write(self, path: Path, new_content: str | None = None) -> str | None:
        """Return a reason when writing `new_content` to `path` (None = deleting) is not allowed."""
        target = path.expanduser().resolve()
        for directory in self.dirs:
            if target == directory or directory in target.parents:
                return f"{target} 是 niya 自己的資料或設定，檔案工具不能修改。"
        if target.name == "authorized_keys" and target.exists():
            existing = _key_lines(target.read_text(encoding="utf-8", errors="replace"))
            removed = existing - _key_lines(new_content or "")
            if removed:
                return f"不能刪掉 authorized_keys 裡既有的 key（這次會少掉 {len(removed)} 把），只能新增。"
        return None


async def validate_sshd(config: Path) -> str | None:
    """Run `sshd -t`; return its complaint, or None when the config is fine or sshd is not installed."""
    sshd = shutil.which("sshd") or ("/usr/sbin/sshd" if Path("/usr/sbin/sshd").exists() else None)
    if sshd is None:
        return None
    main = config if config.name == "sshd_config" else SSHD_MAIN_CONFIG
    proc = await asyncio.create_subprocess_exec(sshd, "-t", "-f", str(main), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    output, _ = await proc.communicate()
    if proc.returncode == 0:
        return None
    return output.decode("utf-8", errors="replace").strip() or f"sshd -t 失敗（exit {proc.returncode}）"
