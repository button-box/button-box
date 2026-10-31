import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path

from messagebox.event_log import UnavailableEvents, append_event


def append_many(path, prefix):
    for number in range(20):
        append_event(path, {'type': prefix, 'number': number}, max_bytes=200)


class EventLogTests(unittest.TestCase):
    def test_transition_reminders_and_recovery(self):
        now = [0]
        limiter = UnavailableEvents(clock=lambda: now[0])
        self.assertTrue(limiter.unavailable('cloud'))
        for value in (1, 60, 299):
            now[0] = value
            self.assertFalse(limiter.unavailable('cloud'))
        now[0] = 300
        self.assertTrue(limiter.unavailable('cloud'))
        limiter.available('cloud')
        self.assertTrue(limiter.unavailable('cloud'))

    def test_concurrent_rotation_preserves_whole_lines_and_two_private_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.jsonl'
            processes = [multiprocessing.get_context('fork').Process(target=append_many, args=(path, name))
                         for name in ('first', 'second')]
            for process in processes:
                process.start()
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            logs = [path, Path(str(path) + '.1'), Path(str(path) + '.2')]
            self.assertFalse(Path(str(path) + '.3').exists())
            for log in logs:
                self.assertLessEqual(log.stat().st_size, 200)
                self.assertEqual(log.stat().st_mode & 0o777, 0o600)
                for line in log.read_text().splitlines():
                    self.assertIn(json.loads(line)['type'], ('first', 'second'))
            self.assertEqual(os.stat(str(path) + '.lock').st_mode & 0o777, 0o600)
