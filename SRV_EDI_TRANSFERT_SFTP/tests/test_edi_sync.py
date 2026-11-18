import base64
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
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

[sftp_babelway]
host = sftp-babelway.example.com
port = 22
user = edi
key_file = /etc/edi-sync/keys/k1
host_fingerprint = sha256:AAAA

[sftp_keane]
host = sftp-keane.example.com
user = edi
key_file = /etc/edi-sync/keys/k2

[flow_babelway_to_keane]
source = sftp_babelway
source_dir = /out
dest = sftp_keane
dest_dir = /in

[flow_keane_to_babelway]
source = sftp_keane
source_dir = /out
dest = sftp_babelway
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
        self.assertEqual(set(cfg.sftps), {"sftp_babelway", "sftp_keane"})
        self.assertEqual(cfg.sftps["sftp_keane"].port, 22)
        self.assertEqual([f.name for f in cfg.flows],
                         ["flow_babelway_to_keane", "flow_keane_to_babelway"])
        self.assertEqual(cfg.flows[0].source, "sftp_babelway")
        self.assertEqual(cfg.flows[0].dest, "sftp_keane")
        self.assertEqual(cfg.flows[1].dest, "sftp_babelway")

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            es.load_config("/nonexistent/config.ini")

    def test_unknown_source_ref(self):
        bad = VALID_INI.replace("source = sftp_babelway", "source = sftp9")
        with self.assertRaises(ValueError):
            es.load_config(self._write(bad))

    def test_no_flow(self):
        bad = VALID_INI.split("[flow_babelway_to_keane]")[0]
        with self.assertRaises(ValueError):
            es.load_config(self._write(bad))

    def test_long_underscore_names(self):
        cfg = es.load_config(self._write(VALID_INI))
        self.assertEqual(set(cfg.sftps), {"sftp_babelway", "sftp_keane"})
        self.assertEqual([f.name for f in cfg.flows],
                         ["flow_babelway_to_keane", "flow_keane_to_babelway"])
        self.assertEqual(cfg.flows[0].source, "sftp_babelway")
        self.assertEqual(cfg.flows[0].dest, "sftp_keane")
        self.assertEqual(cfg.flows[1].source, "sftp_keane")
        self.assertEqual(cfg.flows[1].dest, "sftp_babelway")


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
        self.assertEqual(es.normalize_fingerprint("SHA256:AbC=="), "sha256:AbC")
        self.assertEqual(es.normalize_fingerprint("AbC"), "sha256:AbC")
        self.assertEqual(es.normalize_fingerprint("sha256:"), "sha256:")

    def test_parse_fingerprints_list(self):
        s = es.parse_fingerprints("SHA256:AbC==, sha256:DeF; sha256:GhI")
        self.assertEqual(s, {"sha256:AbC", "sha256:DeF", "sha256:GhI"})
        self.assertEqual(es.parse_fingerprints(""), set())
        self.assertEqual(es.parse_fingerprints("sha256:Seul"), {"sha256:Seul"})


class TestMonitoringConfig(unittest.TestCase):
    def _write(self, text):
        fd, path = tempfile.mkstemp(suffix=".ini")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_defaults(self):
        cfg = es.load_config(self._write(VALID_INI))
        self.assertTrue(cfg.monitoring.enabled)
        self.assertEqual(cfg.monitoring.bind, "127.0.0.1")
        self.assertEqual(cfg.monitoring.port, 8443)

    def test_section_monitoring(self):
        digest = hashlib.sha256(b"secret").hexdigest()
        ini = VALID_INI + ("\n[monitoring]\nenabled = true\nbind = 127.0.0.1\nport = 9443\n"
                           f"auth_user = admin\nauth_password_sha256 = {digest}\n")
        cfg = es.load_config(self._write(ini))
        self.assertEqual(cfg.monitoring.port, 9443)
        self.assertEqual(cfg.monitoring.auth_user, "admin")
        self.assertEqual(cfg.monitoring.auth_password_sha256, digest)


class TestEventRing(unittest.TestCase):
    def test_recent_order_and_maxlen(self):
        ring = es.EventRing(maxlen=3)
        for i in range(5):
            ring.add({"n": i})
        self.assertEqual([e["n"] for e in ring.recent(10)], [4, 3, 2])


class TestFlowStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = self.tmp.name
        self.queue = es.TransferQueue(Path(self.state_dir) / "queue.db")
        self.addCleanup(self.queue.close)
        self.flow = es.FlowConfig(name="flow_a", source="sftp1", source_dir="/out",
                                  dest="sftp2", dest_dir="/in")

    def _heartbeat(self, dt):
        (Path(self.state_dir) / "heartbeat_flow_a").write_text(dt.isoformat())

    def test_ok(self):
        self._heartbeat(datetime.now(timezone.utc))
        st = es.flow_status(self.queue, self.flow, self.state_dir, 5)
        self.assertEqual(st["status"], "ok")
        self.assertEqual(st["source"], "sftp1:/out")
        self.assertEqual(st["dest"], "sftp2:/in")

    def test_stale(self):
        self._heartbeat(datetime.now(timezone.utc) - timedelta(hours=1))
        st = es.flow_status(self.queue, self.flow, self.state_dir, 5)
        self.assertEqual(st["status"], "stale")

    def test_failed_is_error(self):
        self._heartbeat(datetime.now(timezone.utc))
        self.queue.enqueue("flow_a", "f.edi", 1, 1)
        row = self.queue.next_pending("flow_a", 5)
        self.queue.mark(row["id"], es.FAILED, attempts=5)
        st = es.flow_status(self.queue, self.flow, self.state_dir, 5)
        self.assertEqual(st["status"], "error")

    def test_no_heartbeat_is_unknown(self):
        st = es.flow_status(self.queue, self.flow, self.state_dir, 5)
        self.assertEqual(st["status"], "unknown")


class TestMonitoringServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = self.tmp.name
        self.queue = es.TransferQueue(Path(self.state_dir) / "queue.db")
        self.addCleanup(self.queue.close)
        self.flow = es.FlowConfig(name="flow_a", source="sftp1", source_dir="/out",
                                  dest="sftp2", dest_dir="/in")
        self.events = es.EventRing()
        mcfg = es.MonitoringConfig(enabled=True, bind="127.0.0.1", port=0)
        self.mon = es.MonitoringServer(mcfg, self.queue, [self.flow], self.events,
                                       self.state_dir, 5, time.time(), use_tls=False)
        self.mon.start()
        self._wait_ready(self.mon)
        self.port = self.mon.httpd.server_address[1]
        self.addCleanup(self._stop, self.mon)

    def _wait_ready(self, mon):
        for _ in range(100):
            if mon.httpd is not None:
                return
            time.sleep(0.05)
        raise RuntimeError("serveur de monitoring non demarre")

    def _stop(self, mon):
        mon.stop()
        mon.join(timeout=5)

    def _get(self, path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {})
        return urllib.request.urlopen(req, timeout=5)

    def test_health(self):
        with self._get("/health") as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.read().decode(), "ok")

    def test_api_status_json(self):
        with self._get("/api/status") as r:
            data = json.loads(r.read().decode())
        self.assertIn("flow_a", data["flows"])
        self.assertEqual(data["flows"]["flow_a"]["status"], "unknown")
        self.assertIn("uptime_sec", data)

    def test_index_html(self):
        with self._get("/") as r:
            body = r.read().decode()
        self.assertEqual(r.status, 200)
        self.assertIn("edi-sync", body)
        self.assertIn("flow_a", body)

    def test_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_basic_auth(self):
        mcfg = es.MonitoringConfig(enabled=True, bind="127.0.0.1", port=0,
                                    auth_user="admin",
                                    auth_password_sha256=hashlib.sha256(b"secret").hexdigest())
        mon = es.MonitoringServer(mcfg, self.queue, [self.flow], self.events,
                                  self.state_dir, 5, time.time(), use_tls=False)
        mon.start()
        self._wait_ready(mon)
        port = mon.httpd.server_address[1]
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=5)
            self.assertEqual(ctx.exception.code, 401)
            token = base64.b64encode(b"admin:secret").decode()
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/status",
                                         headers={"Authorization": f"Basic {token}"})
            with urllib.request.urlopen(req, timeout=5) as r:
                self.assertEqual(r.status, 200)
        finally:
            self._stop(mon)


class _FakeTransport:
    def is_active(self):
        return True


class _FakeClient:
    def get_transport(self):
        return _FakeTransport()


class _FakeSftp:
    def __init__(self, existing):
        self.existing = set(existing)
        self.created = []

    def mkdir(self, p):
        if p in self.existing:
            raise OSError("failure")
        self.created.append(p)

    def stat(self, p):
        if p in self.existing or p in self.created:
            return object()
        return None


class TestMkdirs(unittest.TestCase):
    def _endpoint(self, fake):
        ep = es.SftpEndpoint(es.SftpConfig(name="sftp1", host="h", port=22, user="u", key_file="k"))
        ep._client = _FakeClient()
        ep._sftp = fake
        return ep

    def test_existing_dirs_are_ok(self):
        fake = _FakeSftp(existing={"/", "/archive"})
        self._endpoint(fake).mkdirs("/archive/flow_a")
        self.assertIn("/archive/flow_a", fake.created)

    def test_missing_parent_raises(self):
        class AlwaysFail:
            def mkdir(self, p):
                raise OSError("failure")

            def stat(self, p):
                return object() if p == "/" else None
        with self.assertRaises(OSError):
            self._endpoint(AlwaysFail()).mkdirs("/archive/flow_a")


class TestLogFile(unittest.TestCase):
    def test_log_file_option_parsed(self):
        fd, path = tempfile.mkstemp(suffix=".ini")
        with os.fdopen(fd, "w") as f:
            f.write(VALID_INI.replace("state_dir = /tmp/edi-state",
                                       "state_dir = /tmp/edi-state\nlog_file = /tmp/x/daemon.log"))
        self.addCleanup(os.unlink, path)
        cfg = es.load_config(path)
        self.assertEqual(cfg.log_file, "/tmp/x/daemon.log")

    def test_log_file_written(self):
        import logging as lg
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "daemon.log")
            root = lg.getLogger()
            old_handlers = root.handlers[:]
            try:
                es.setup_logging(events=None, log_file=p)
                lg.getLogger("t").warning("hello-test")
                for h in root.handlers:
                    h.flush()
                content = open(p).read()
                self.assertIn("hello-test", content)
                self.assertIn('"level": "WARNING"', content)
            finally:
                root.handlers = old_handlers

    def test_default_no_log_file(self):
        fd, path = tempfile.mkstemp(suffix=".ini")
        with os.fdopen(fd, "w") as f:
            f.write(VALID_INI)
        self.addCleanup(os.unlink, path)
        self.assertEqual(es.load_config(path).log_file, "")


class TestArchiveLocal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = es.Config(state_dir=self.tmp.name,
                             local_archive_dir=os.path.join(self.tmp.name, "ARCHIVE"))
        self.flow = es.FlowConfig(name="flow_babelway_to_keane", source="sftp_babelway",
                                  source_dir="/out", dest="sftp_keane", dest_dir="/in")
        import logging
        self.worker = es.FlowWorker(self.flow, None, None, None, self.cfg,
                                    logging.getLogger("t"), __import__("threading").Event())

    def test_archive_local_moves_file(self):
        src = Path(self.tmp.name) / "f.edi"
        src.write_text("contenu")
        self.worker._archive_local(src, "f.edi")
        dest = Path(self.cfg.local_archive_dir) / self.flow.name / "f.edi"
        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_text(), "contenu")
        self.assertFalse(src.exists())

    def test_archive_local_same_size_is_idempotent(self):
        src = Path(self.tmp.name) / "f.edi"
        src.write_text("contenu")
        self.worker._archive_local(src, "f.edi")
        src.write_text("contenu")
        self.worker._archive_local(src, "f.edi")
        dest_dir = Path(self.cfg.local_archive_dir) / self.flow.name
        self.assertEqual(len(list(dest_dir.iterdir())), 1)

    def test_no_local_archive_dir_is_noop(self):
        self.cfg.local_archive_dir = ""
        src = Path(self.tmp.name) / "f.edi"
        src.write_text("x")
        self.worker._archive_local(src, "f.edi")
        self.assertTrue(src.exists())


class TestPurgeLocal(unittest.TestCase):
    def test_purge_removes_old_files_only(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "ARCHIVE" / "flow_x"
            base.mkdir(parents=True)
            old = base / "old.edi"
            recent = base / "recent.edi"
            old.write_text("old")
            recent.write_text("recent")
            old_ts = time.time() - 10 * 86400
            os.utime(old, (old_ts, old_ts))
            cfg = es.Config(local_archive_dir=str(Path(d) / "ARCHIVE"), archive_retention_days=7)
            flow = es.FlowConfig(name="flow_x", source="s", source_dir="/out", dest="d", dest_dir="/in")
            import logging
            purge = es.PurgeWorker([flow], cfg, logging.getLogger("t"), __import__("threading").Event())
            purge.purge_all()
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())


if __name__ == "__main__":
    unittest.main()
