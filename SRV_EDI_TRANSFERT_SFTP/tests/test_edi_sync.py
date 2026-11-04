import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import edi_sync as es


VALID_INI = """
[general]
scan_interval_sec = 5
stability_scans = 2
max_retries = 3
archive_retention_days = 7
state_dir = /tmp/edi-state

[sftp1]
host = sftp1.example.com
port = 22
user = edi
key_file = /etc/edi-sync/keys/k1
host_fingerprint = sha256:AAAA

[sftp2]
host = sftp2.example.com
user = edi
key_file = /etc/edi-sync/keys/k2

[flow_a]
source = sftp1
source_dir = /out
dest = sftp2
dest_dir = /in
archive_dir = /archive/flow_a

[flow_b]
source = sftp2
source_dir = /out
dest = sftp1
dest_dir = /in
"""


class TestLoadConfig(unittest.TestCase):
    def _write(self, text):
        fd, path = tempfile.mkstemp(suffix=".ini")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_valid_config(self):
        cfg = es.load_config(self._write(VALID_INI))
        self.assertEqual(cfg.scan_interval_sec, 5)
        self.assertEqual(cfg.max_retries, 3)
        self.assertEqual(set(cfg.sftps), {"sftp1", "sftp2"})
        self.assertEqual(cfg.sftps["sftp2"].port, 22)
        self.assertEqual([f.name for f in cfg.flows], ["flow_a", "flow_b"])
        self.assertEqual(cfg.flows[0].source, "sftp1")
        self.assertEqual(cfg.flows[1].dest, "sftp1")
        self.assertEqual(cfg.flows[1].archive_dir, "/archive/flow_b")

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            es.load_config("/nonexistent/config.ini")

    def test_unknown_source_ref(self):
        bad = VALID_INI.replace("source = sftp1", "source = sftp9")
        with self.assertRaises(ValueError):
            es.load_config(self._write(bad))

    def test_no_flow(self):
        bad = VALID_INI.split("[flow_a]")[0]
        with self.assertRaises(ValueError):
            es.load_config(self._write(bad))


class TestTransferQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queue = es.TransferQueue(Path(self.tmp.name) / "queue.db")
        self.addCleanup(self.queue.close)

    def test_enqueue_is_idempotent(self):
        self.assertTrue(self.queue.enqueue("flow_a", "f.edi", 100, 111))
        self.assertFalse(self.queue.enqueue("flow_a", "f.edi", 100, 111))
        self.assertTrue(self.queue.enqueue("flow_a", "f.edi", 101, 111))
        self.assertTrue(self.queue.enqueue("flow_b", "f.edi", 100, 111))

    def test_next_pending_respects_backoff(self):
        self.queue.enqueue("flow_a", "f.edi", 100, 111)
        row = self.queue.next_pending("flow_a", max_retries=5)
        self.assertIsNotNone(row)
        self.queue.mark(row["id"], es.PENDING, attempts=1, next_attempt=time.time() + 3600)
        self.assertIsNone(self.queue.next_pending("flow_a", max_retries=5))
        self.queue.mark(row["id"], es.PENDING, attempts=1, next_attempt=time.time() - 1)
        self.assertIsNotNone(self.queue.next_pending("flow_a", max_retries=5))

    def test_next_pending_respects_max_retries(self):
        self.queue.enqueue("flow_a", "f.edi", 100, 111)
        row = self.queue.next_pending("flow_a", max_retries=5)
        self.queue.mark(row["id"], es.PENDING, attempts=5, next_attempt=0)
        self.assertIsNone(self.queue.next_pending("flow_a", max_retries=5))

    def test_mark_done_and_counts(self):
        self.queue.enqueue("flow_a", "a.edi", 1, 1)
        self.queue.enqueue("flow_a", "b.edi", 2, 2)
        row = self.queue.next_pending("flow_a", max_retries=5)
        self.queue.mark(row["id"], es.DONE)
        counts = self.queue.counts("flow_a")
        self.assertEqual(counts.get(es.DONE), 1)
        self.assertEqual(counts.get(es.PENDING), 1)


class TestStabilityTracker(unittest.TestCase):
    def test_stable_after_required_scans(self):
        t = es.StabilityTracker(2)
        self.assertFalse(t.update("f.edi", 100))
        self.assertTrue(t.update("f.edi", 100))

    def test_size_change_resets(self):
        t = es.StabilityTracker(2)
        self.assertFalse(t.update("f.edi", 100))
        self.assertFalse(t.update("f.edi", 200))
        self.assertTrue(t.update("f.edi", 200))

    def test_forget_missing(self):
        t = es.StabilityTracker(2)
        t.update("gone.edi", 1)
        t.forget_missing(set())
        self.assertFalse(t.update("gone.edi", 1))


class TestHelpers(unittest.TestCase):
    def test_is_ignored_name(self):
        self.assertTrue(es.is_ignored_name(".hidden"))
        self.assertTrue(es.is_ignored_name("f.edi.tmp"))
        self.assertTrue(es.is_ignored_name("f.part"))
        self.assertFalse(es.is_ignored_name("ORDERS.edi"))

    def test_resolve_dest_name(self):
        self.assertEqual(es.resolve_dest_name("f.edi", 10, None, 1), ("f.edi", False))
        self.assertEqual(es.resolve_dest_name("f.edi", 10, 10, 1), ("f.edi", True))
        name, skip = es.resolve_dest_name("f.edi", 10, 99, 42)
        self.assertEqual(name, "f.edi.dup-42")
        self.assertFalse(skip)

    def test_backoff_delay(self):
        self.assertEqual(es.backoff_delay(1), 30)
        self.assertEqual(es.backoff_delay(2), 60)
        self.assertEqual(es.backoff_delay(3), 300)
        self.assertEqual(es.backoff_delay(4), 900)
        self.assertEqual(es.backoff_delay(5), 3600)
        self.assertEqual(es.backoff_delay(99), 3600)

    def test_fingerprint_normalize(self):
        self.assertEqual(es.normalize_fingerprint("SHA256:ABC=="), "sha256:abc")
        self.assertEqual(es.normalize_fingerprint("abc"), "sha256:abc")
        self.assertEqual(es.normalize_fingerprint(""), "sha256:")


if __name__ == "__main__":
    unittest.main()
