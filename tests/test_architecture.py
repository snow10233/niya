import ast
import unittest
from pathlib import Path

import niya

FORBIDDEN = ("niya.agent", "niya.llm", "niya.discord_bot", "niya.tools", "niya.runtime", "claude_agent_sdk", "httpx")


class ArchitectureTests(unittest.TestCase):
    def test_memory_package_stands_alone(self):
        package = Path(niya.__file__).parent / "memory"
        for path in package.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                for name in names:
                    with self.subTest(file=path.name, module=name):
                        self.assertFalse(name.startswith(FORBIDDEN), f"{path.name} imports {name}")


if __name__ == "__main__":
    unittest.main()
