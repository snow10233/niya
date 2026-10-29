import unittest

from niya.tools.preview import MAX_PREVIEW, tool_line


class ToolLineTests(unittest.TestCase):
    def test_terminal_shows_the_command_on_one_line(self):
        self.assertEqual(tool_line("terminal", {"command": "cd /etc/nginx &&\n  nginx -t"}), "💻 terminal: cd /etc/nginx && nginx -t")
        self.assertEqual(tool_line("terminal", {"command": "npm run dev", "background": True}), "💻 terminal: npm run dev（背景）")

    def test_long_arguments_are_clipped(self):
        line = tool_line("terminal", {"command": "echo " + "x" * 300})
        self.assertEqual(len(line), len("💻 terminal: ") + MAX_PREVIEW)
        self.assertTrue(line.endswith("…"))

    def test_each_tool_picks_its_telling_argument(self):
        cases = [
            ("read_file", {"path": "/etc/hosts", "offset": 40}, "📖 read_file: /etc/hosts（從第 40 行）"),
            ("patch", {"path": "a.py", "old_string": "x", "new_string": "y"}, "🔧 patch: a.py"),
            ("search_files", {"pattern": "TODO", "path": "src"}, "🔎 search_files: TODO（在 src）"),
            ("web_extract", {"urls": ["https://a.example", "https://b.example"]}, "📄 web_extract: https://a.example, https://b.example"),
            ("alarm", {"action": "set", "title": "喝水", "in_minutes": 10}, "⏰ alarm: set 喝水（10 分鐘後）"),
            ("alarm", {"action": "cancel", "id": 3}, "⏰ alarm: cancel #3"),
            ("alarm", {"action": "list"}, "⏰ alarm: list"),
            ("recall_timeline", {"since": "上週", "until": "昨天"}, "🗓️ recall_timeline: 上週 ~ 昨天"),
            ("skill_manage", {"action": "create", "name": "deploy-nginx", "content": "..."}, "📝 skill_manage: create deploy-nginx"),
            ("delegate_task", {"task": "查 nginx 版本", "context": "VPS"}, "🔀 delegate_task: 查 nginx 版本"),
            ("process", {"action": "poll", "id": "p3"}, "⚙️ process: poll p3"),
        ]
        for name, args, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(tool_line(name, args), expected)

    def test_no_arguments_and_unknown_tools(self):
        self.assertEqual(tool_line("skills_list", {}), "📚 skills_list")
        self.assertEqual(tool_line("mcp__weather__lookup", {"days": 3, "city": "台北"}), "🔌 mcp__weather__lookup: 台北")
        self.assertEqual(tool_line("mystery", {}), "⚡ mystery")


if __name__ == "__main__":
    unittest.main()
