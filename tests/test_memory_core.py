import unittest

import numpy as np
from helpers import TZ, ts

from niya.db import Database
from niya.memory.curve import cue_threshold, days_until, initial_stability, reinforce, retrievability
from niya.memory.timefmt import exact_label, time_label
from niya.memory.timeparse import parse_time_range
from niya.memory.vectors import VectorIndex, from_blob, normalise, search_archived, store_vector, to_blob


class CurveTests(unittest.TestCase):
    def test_retrievability(self):
        self.assertEqual(retrievability(0, 5), 1.0)
        self.assertAlmostEqual(retrievability(5, 5), 0.9)
        self.assertGreater(retrievability(1, 5), retrievability(10, 5))

    def test_initial_stability_matches_the_plan(self):
        self.assertAlmostEqual(initial_stability(1), 0.5)
        self.assertAlmostEqual(initial_stability(10), 30.0)
        self.assertAlmostEqual(initial_stability(6), 4.86, places=2)
        self.assertAlmostEqual(days_until(0.3, initial_stability(6)), 102, delta=1)

    def test_reinforcement(self):
        fresh, faded = reinforce(5, 0.95, "use"), reinforce(5, 0.4, "use")
        self.assertGreater(faded, fresh)
        self.assertGreater(fresh, 5)
        small_gain = reinforce(50, 0.5, "use") / 50
        self.assertLess(small_gain, reinforce(5, 0.5, "use") / 5)
        self.assertEqual(reinforce(5, 0.4, "exposure"), 5)
        self.assertGreater(reinforce(5, 0.4, "use"), reinforce(5, 0.4, "reencounter"))
        relearned = reinforce(5, 0.2, "relearn")
        self.assertGreater(relearned, 5)

    def test_days_until_inverts_retrievability(self):
        t = days_until(0.6, 3)
        self.assertAlmostEqual(retrievability(t, 3), 0.6)

    def test_faded_memories_need_stronger_cues(self):
        self.assertAlmostEqual(cue_threshold(1.0), 0.30)
        self.assertGreater(cue_threshold(0.4), cue_threshold(0.9))


class TimeLabelTests(unittest.TestCase):
    now = ts("2026-10-29 09:15")  # Thursday

    def label(self, when, r, precision="minute"):
        return time_label(ts(when), precision, r, self.now, TZ)

    def test_blur_follows_retrievability(self):
        self.assertEqual(self.label("2026-10-28 14:10", 0.95), "昨天下午 2 點")
        self.assertEqual(self.label("2026-10-28 14:10", 0.7), "昨天")
        self.assertEqual(self.label("2026-10-27 10:00", 0.95), "前天上午 10 點")
        self.assertEqual(self.label("2026-10-26 10:00", 0.95), "這週一上午 10 點")
        self.assertEqual(self.label("2026-10-20 10:00", 0.7), "上週二")
        self.assertEqual(self.label("2026-10-02 10:00", 0.7), "10/2（五）")
        self.assertEqual(self.label("2026-10-22 10:00", 0.5), "10 月下旬")
        self.assertEqual(self.label("2026-09-10 10:00", 0.3), "上個月")
        self.assertEqual(self.label("2025-11-10 10:00", 0.2), "2025 年秋天")
        self.assertEqual(self.label("2025-10-10 10:00", 0.7), "2025/10/10（五）")

    def test_never_finer_than_stored(self):
        self.assertEqual(self.label("2026-10-28 14:10", 0.99, precision="day"), "昨天")
        self.assertEqual(time_label(None, "unknown", 0.9, self.now, TZ), "不記得什麼時候")
        self.assertEqual(self.label("2026-11-04 14:00", 0.95), "11/4（三）下午 2 點")
        self.assertEqual(self.label("2026-10-30 08:00", 0.95), "明天早上 8 點")

    def test_exact_label(self):
        self.assertEqual(exact_label(ts("2026-10-28 10:02"), ts("2026-10-28 10:40"), TZ), "2026-10-28 (三) 10:02–10:40")


class TimeParseTests(unittest.TestCase):
    now = ts("2026-10-29 09:15")

    def span(self, text):
        found = parse_time_range(text, self.now, TZ)
        return found and (found[0], found[1])

    def test_phrases(self):
        cases = {
            "昨天下午做了什麼": ("2026-10-28 12:00", "2026-10-28 18:00"),
            "上週二": ("2026-10-20 00:00", "2026-10-21 00:00"),
            "週二那天": ("2026-10-27 00:00", "2026-10-28 00:00"),
            "上週": ("2026-10-19 00:00", "2026-10-26 00:00"),
            "三天前": ("2026-10-26 00:00", "2026-10-27 00:00"),
            "上個月": ("2026-09-01 00:00", "2026-10-01 00:00"),
            "十月二十八日": ("2026-10-28 00:00", "2026-10-29 00:00"),
            "12月": ("2025-12-01 00:00", "2026-01-01 00:00"),
            "下週三看牙醫": ("2026-11-04 00:00", "2026-11-05 00:00"),
            "上禮拜五晚上": ("2026-10-23 18:00", "2026-10-24 00:00"),
            "去年": ("2025-01-01 00:00", "2026-01-01 00:00"),
        }
        for text, (start, end) in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.span(text), (ts(start), ts(end)))

    def test_no_time_words(self):
        for text in ("網站壞了", "幫我看 nginx", "31號"):
            with self.subTest(text=text):
                self.assertIsNone(self.span(text))


class VectorTests(unittest.TestCase):
    def test_blob_round_trip_and_index(self):
        v = normalise([3.0, 4.0])
        self.assertTrue(np.allclose(from_blob(to_blob([3.0, 4.0]), 2), v))
        index = VectorIndex()
        for i, vec in enumerate([[1, 0, 0], [0.9, 0.1, 0], [0, 1, 0]], start=1):
            index.add(i, normalise(vec))
        for i in range(4, 40):
            index.add(i, normalise([0, 0, 1]))
        self.assertEqual([i for i, _ in index.search(normalise([1, 0, 0]), 2)], [1, 2])
        index.remove(1)
        self.assertEqual(index.search(normalise([1, 0, 0]), 1)[0][0], 2)
        self.assertEqual(len(index), 38)

    def test_archived_vectors_are_searched_from_the_database(self):
        db = Database(":memory:")
        for i, (state, vec) in enumerate([("archived", [1, 0]), ("active", [1, 0]), ("archived", [0, 1])], start=1):
            db.execute(
                "INSERT INTO memories(id, kind, content, importance, state, stability, last_review_at, origin, created_at) VALUES (?, 'fact', 'x', 5, ?, 1, 0, 't', 0)",
                (i, state),
            )
            store_vector(db, i, "m", vec)
        self.assertEqual([i for i, _ in search_archived(db, "m", normalise([1, 0]), 2)], [1, 3])
        self.assertEqual(VectorIndex.load(db, "m").ids, [2])


if __name__ == "__main__":
    unittest.main()
