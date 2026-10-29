import unittest

from niya.discord_bot.text import SleepReplyLimiter, chunk_message, render_progress, should_handle
from niya.tools.preview import ToolView

OWNER = 111


class GateTests(unittest.TestCase):
    def check(self, **overrides):
        args = dict(author_id=OWNER, author_is_bot=False, owner_id=OWNER, is_dm=True, channel_id=5, channel_ids=frozenset({9}))
        args.update(overrides)
        return should_handle(**args)

    def test_only_the_owner_in_dms_or_chosen_channels(self):
        self.assertTrue(self.check())
        self.assertTrue(self.check(is_dm=False, channel_id=9))
        self.assertFalse(self.check(is_dm=False, channel_id=5))
        self.assertFalse(self.check(author_id=222))
        self.assertFalse(self.check(author_is_bot=True))


class LimiterTests(unittest.TestCase):
    def test_once_per_interval(self):
        limiter = SleepReplyLimiter(interval_s=1800)
        self.assertTrue(limiter.allow(0))
        self.assertFalse(limiter.allow(60))
        self.assertFalse(limiter.allow(1799))
        self.assertTrue(limiter.allow(1800))


class ChunkTests(unittest.TestCase):
    def test_short_text_is_untouched(self):
        self.assertEqual(chunk_message("嗨"), ["嗨"])

    def test_long_text_splits_on_lines_with_counters(self):
        text = "\n".join(f"第 {i} 行" + "字" * 80 for i in range(60))
        parts = chunk_message(text)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 2000 for p in parts))
        self.assertTrue(parts[0].endswith(f"（1/{len(parts)}）"))
        rejoined = "\n".join(p.rsplit("\n", 1)[0] for p in parts)
        self.assertEqual(rejoined, text)

    def test_code_blocks_are_closed_and_reopened(self):
        text = "看這段：\n```python\n" + "\n".join(f"print({i})  # " + "x" * 60 for i in range(80)) + "\n```\n完成"
        parts = chunk_message(text)
        self.assertGreater(len(parts), 1)
        for part in parts:
            body = part.rsplit("\n", 1)[0]
            self.assertEqual(body.count("```") % 2, 0, body[:80])
        self.assertTrue(parts[1].startswith("```python\n"))

    def test_a_single_huge_line_is_cut(self):
        parts = chunk_message("a" * 5000)
        self.assertTrue(all(len(p) <= 2000 for p in parts))
        self.assertEqual("".join(p.rsplit("\n", 1)[0] for p in parts), "a" * 5000)


class ProgressRenderTests(unittest.TestCase):
    def test_each_call_gets_its_own_block_under_one_header_per_run(self):
        ls, df, hosts = ToolView("💻 terminal", "ls"), ToolView("💻 terminal", "df -h"), ToolView("📖 read_file", "hosts")
        text = render_progress([(ls, 1), (df, 2), (hosts, 1), (ToolView("📚 skills_list"), 1)])
        self.assertEqual(text, "💻 terminal\n```\nls\n```\n```\ndf -h\n``` (×2)\n📖 read_file\n```\nhosts\n```\n📚 skills_list...")

    def test_fences_inside_a_detail_cannot_break_the_block(self):
        self.assertEqual(render_progress([(ToolView("💻 terminal", "echo ```"), 1)]), "💻 terminal\n```\necho ˋˋˋ\n```")


if __name__ == "__main__":
    unittest.main()
