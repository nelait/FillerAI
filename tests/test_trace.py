"""Tests for the running commentary a training run gives.

The cursor is the part worth testing hard. The UI asks "what is new since
line 40?" every few hundred milliseconds while a worker thread is appending,
and an answer that is off by one either drops a line or repeats one. Both
look like a bug in the training code to whoever is reading the log.
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fillerai.train.trace import SILENT, Trace, resolve


class TestTrace(unittest.TestCase):
    def test_lines_come_back_in_the_order_they_were_written(self):
        trace = Trace()
        trace.step("first")
        trace.detail("second")
        trace.done("third")
        self.assertEqual([line.text for line in trace.since()],
                         ["first", "second", "third"])
        self.assertEqual([line.index for line in trace.since()], [0, 1, 2])

    def test_a_cursor_returns_exactly_what_came_after_it(self):
        trace = Trace()
        for index in range(5):
            trace.log(f"line {index}")
        self.assertEqual([line.text for line in trace.since(2)],
                         ["line 2", "line 3", "line 4"])
        self.assertEqual(trace.since(5), [])

    def test_reading_to_the_end_and_writing_more_loses_nothing(self):
        """The polling loop, in miniature."""
        trace = Trace()
        cursor, seen = 0, []
        for round_number in range(4):
            trace.log(f"round {round_number}")
            trace.log(f"round {round_number} again")
            new = trace.since(cursor)
            cursor += len(new)
            seen.extend(line.text for line in new)
        self.assertEqual(len(seen), 8)
        self.assertEqual(len(set(seen)), 8)
        self.assertEqual(cursor, trace.count)

    def test_steps_are_counted_so_progress_can_be_a_fraction(self):
        trace = Trace()
        trace.expect(4)
        trace.step("one")
        trace.step("two")
        progress = trace.progress()
        self.assertEqual(progress["step"], 2)
        self.assertEqual(progress["steps"], 4)
        self.assertAlmostEqual(progress["fraction"], 0.5)

    def test_more_steps_than_expected_do_not_push_the_fraction_past_one(self):
        trace = Trace()
        trace.expect(2)
        for _ in range(5):
            trace.step("another")
        self.assertLessEqual(trace.progress()["fraction"], 1.0)

    def test_a_finished_run_says_so(self):
        trace = Trace()
        self.assertFalse(trace.progress()["finished"])
        trace.finish()
        self.assertTrue(trace.progress()["finished"])
        self.assertIsNone(trace.failure)

    def test_a_failed_run_says_why_in_the_log_as_well(self):
        trace = Trace()
        trace.finish("training failed: no records")
        self.assertEqual(trace.failure, "training failed: no records")
        self.assertIn("no records", trace.text())

    def test_writes_from_several_threads_all_arrive(self):
        """The fit runs on a worker while request threads read it."""
        trace = Trace()
        def write(worker: int) -> None:
            for index in range(50):
                trace.log(f"{worker}:{index}")

        threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        lines = trace.since()
        self.assertEqual(len(lines), 200)
        # Indexes are handed out under the lock, so they are a clean run.
        self.assertEqual([line.index for line in lines], list(range(200)))

    def test_reading_while_writing_never_returns_a_gap(self):
        trace = Trace()
        stop = threading.Event()
        seen: list[str] = []

        def write() -> None:
            for index in range(300):
                trace.log(str(index))
            stop.set()

        writer = threading.Thread(target=write)
        writer.start()
        cursor = 0
        while not stop.is_set() or cursor < trace.count:
            new = trace.since(cursor)
            cursor += len(new)
            seen.extend(line.text for line in new)
        writer.join()
        self.assertEqual(seen, [str(index) for index in range(300)])

    def test_a_formatted_line_carries_its_time_and_level(self):
        trace = Trace()
        trace.step("growing trees")
        line = trace.since()[0]
        self.assertIn("==", line.formatted())
        self.assertIn("growing trees", line.formatted())
        self.assertEqual(line.to_dict()["level"], "step")


class TestSilent(unittest.TestCase):
    def test_nothing_is_kept_when_nobody_is_watching(self):
        SILENT.log("this goes nowhere")
        self.assertEqual(SILENT.since(), [])

    def test_the_silent_trace_still_counts_steps_for_the_caller(self):
        before = SILENT.progress()["step"]
        SILENT.step("a stage")
        self.assertEqual(SILENT.progress()["step"], before + 1)

    def test_resolve_gives_the_silent_one_for_none(self):
        self.assertIs(resolve(None), SILENT)
        mine = Trace()
        self.assertIs(resolve(mine), mine)


if __name__ == "__main__":
    unittest.main()
