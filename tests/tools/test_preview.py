import unittest

from niya.tools.preview import ToolView, preview, tool_view


class PreviewTests(unittest.TestCase):
    def test_terminal_shows_the_first_line_capped_at_40(self):
        self.assertEqual(preview("terminal", {"command": "uptime"}), "uptime")
        self.assertEqual(preview("terminal", {"command": "cd /etc/nginx\nnginx -t"}), "cd /etc/nginx ...")
        long = preview("terminal", {"command": "journalctl -u nginx --since today --no-pager | tail -50"})
        self.assertEqual(long, "journalctl -u nginx --since today --n...")
        self.assertEqual(len(long), 40)

    def test_each_tool_picks_its_telling_argument(self):
        cases = [
            ("read_file", {"path": "/etc/nginx/nginx.conf", "offset": 40, "limit": 20}, "nginx.conf L40-59"),
            ("patch", {"path": "a.py", "old_string": "x", "new_string": "y"}, "a.py"),
            ("search_files", {"pattern": "TODO", "path": "src"}, "TODO"),
            ("web_search", {"query": "nginx 502 bad gateway"}, "nginx 502 bad gateway"),
            ("web_extract", {"urls": ["https://a.example", "https://b.example"]}, "https://a.example"),
            ("process", {"action": "wait", "id": "p3", "timeout": 30}, "wait p3 30s"),
            ("skill_view", {"name": "deploy", "file_path": "notes.md"}, "deploy → notes.md"),
            ("alarm", {"action": "set", "title": "喝水", "in_minutes": 10}, "set 喝水（10 分鐘後）"),
            ("alarm", {"action": "cancel", "id": 3}, "cancel #3"),
            ("delegate_task", {"task": "查 nginx 版本", "context": "VPS"}, "查 nginx 版本"),
            ("mcp__docs__search", {"query": "asyncio"}, "asyncio"),
            ("mcp__weather__lookup", {"city": "台北"}, ""),
            ("skills_list", {}, ""),
        ]
        for name, args, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(preview(name, args), expected)

    def test_views(self):
        self.assertEqual(tool_view("terminal", {"command": "ls"}), ToolView("💻 terminal", "ls"))
        self.assertEqual(tool_view("read_file", {"path": "/etc/hosts"}, sub=True), ToolView("↳ 📖 read_file", "hosts"))
        self.assertEqual(tool_view("mcp__weather__lookup", {"city": "台北"}).line(), "⚙️ mcp__weather__lookup...")
        self.assertEqual(tool_view("terminal", {"command": "ls"}).line(), "💻 terminal: ls")

    def test_media_and_discord_tools(self):
        self.assertEqual(preview("browser_exec", {"code": "# 搜尋 Amazon 的紙巾\nnew_tab('https://amazon.com')"}), "搜尋 Amazon 的紙巾")
        self.assertEqual(preview("discord", {"action": "send", "channel_id": "123", "text": "早安"}), "send 123 早安")
        self.assertEqual(tool_view("image_generate", {"prompt": "一隻貓"}), ToolView("🎨 image_generate", "一隻貓"))


if __name__ == "__main__":
    unittest.main()
