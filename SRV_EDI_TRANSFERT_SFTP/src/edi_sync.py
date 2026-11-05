#!/usr/bin/env python3
"""Daemon de transfert SFTP en temps reel et bidirectionnel de fichiers EDI."""

import argparse
import base64
import configparser
import errno
import hashlib
import html
import json
import logging
import os
import posixpath
import signal
import sqlite3
import ssl
import stat as statmod
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import paramiko
except ImportError:
    paramiko = None

SSH_ERRORS = (paramiko.SSHException, EOFError, OSError) if paramiko is not None else (EOFError, OSError)

PENDING = "pending"
DONE = "done"
FAILED = "failed"

BACKOFF_SCHEDULE_SEC = [30, 60, 300, 900, 3600]
IGNORED_SUFFIXES = (".tmp", ".part")

SCHEMA = """
CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow TEXT NOT NULL,
    filename TEXT NOT NULL,
    size INTEGER NOT NULL,
    src_mtime INTEGER NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(flow, filename, size, src_mtime)
);
"""


def now() -> int:
    return int(time.time())


def backoff_delay(attempts: int) -> int:
    return BACKOFF_SCHEDULE_SEC[min(attempts - 1, len(BACKOFF_SCHEDULE_SEC) - 1)]


def is_ignored_name(name: str) -> bool:
    return name.startswith(".") or name.endswith(IGNORED_SUFFIXES)


def resolve_dest_name(filename: str, size: int, existing_size, now_ts: int):
    if existing_size is None:
        return filename, False
    if existing_size == size:
        return filename, True
    return f"{filename}.dup-{now_ts}", False


def fingerprint_of(server_key) -> str:
    digest = hashlib.sha256(server_key.asbytes()).digest()
    return "sha256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def normalize_fingerprint(fp: str) -> str:
    fp = (fp or "").strip().lower()
    if fp.startswith("sha256:"):
        fp = fp[len("sha256:"):]
    return "sha256:" + fp.rstrip("=")


@dataclass
class SftpConfig:
    name: str
    host: str
    port: int
    user: str
    key_file: str
    host_fingerprint: str = ""


@dataclass
class FlowConfig:
    name: str
    source: str
    source_dir: str
    dest: str
    dest_dir: str
    archive_dir: str


@dataclass
class MonitoringConfig:
    enabled: bool = True
    bind: str = "127.0.0.1"
    port: int = 8443
    tls_cert: str = ""
    tls_key: str = ""
    auth_user: str = ""
    auth_password_sha256: str = ""


@dataclass
class Config:
    scan_interval_sec: int = 5
    stability_scans: int = 2
    max_retries: int = 5
    archive_retention_days: int = 7
    state_dir: str = "/var/lib/edi-sync"
    sftps: dict = field(default_factory=dict)
    flows: list = field(default_factory=list)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)


def load_config(path: str) -> Config:
    parser = configparser.ConfigParser()
    if not parser.read(path):
        raise FileNotFoundError(f"config introuvable: {path}")

    def getg(key, default, cast=str):
        if parser.has_section("general") and parser.has_option("general", key):
            return cast(parser.get("general", key))
        return default

    cfg = Config(
        scan_interval_sec=getg("scan_interval_sec", 5, int),
        stability_scans=getg("stability_scans", 2, int),
        max_retries=getg("max_retries", 5, int),
        archive_retention_days=getg("archive_retention_days", 7, int),
        state_dir=getg("state_dir", "/var/lib/edi-sync"),
    )

    for section in parser.sections():
        if section.startswith("sftp"):
            cfg.sftps[section] = SftpConfig(
                name=section,
                host=parser.get(section, "host"),
                port=parser.getint(section, "port", fallback=22),
                user=parser.get(section, "user"),
                key_file=parser.get(section, "key_file"),
                host_fingerprint=parser.get(section, "host_fingerprint", fallback=""),
            )
    if not cfg.sftps:
        raise ValueError("aucune section [sftp*] dans la config")

    for section in parser.sections():
        if section.startswith("flow"):
            src = parser.get(section, "source")
            dst = parser.get(section, "dest")
            if src not in cfg.sftps:
                raise ValueError(f"[{section}] source '{src}' : aucune section [{src}] definie")
            if dst not in cfg.sftps:
                raise ValueError(f"[{section}] dest '{dst}' : aucune section [{dst}] definie")
            cfg.flows.append(FlowConfig(
                name=section,
                source=src,
                source_dir=parser.get(section, "source_dir"),
                dest=dst,
                dest_dir=parser.get(section, "dest_dir"),
                archive_dir=parser.get(section, "archive_dir", fallback=f"/archive/{section}"),
            ))
    if not cfg.flows:
        raise ValueError("aucune section [flow*] dans la config")

    if parser.has_section("monitoring"):
        m = cfg.monitoring
        m.enabled = parser.getboolean("monitoring", "enabled", fallback=True)
        m.bind = parser.get("monitoring", "bind", fallback="127.0.0.1")
        m.port = parser.getint("monitoring", "port", fallback=8443)
        m.tls_cert = parser.get("monitoring", "tls_cert", fallback="")
        m.tls_key = parser.get("monitoring", "tls_key", fallback="")
        m.auth_user = parser.get("monitoring", "auth_user", fallback="")
        m.auth_password_sha256 = parser.get("monitoring", "auth_password_sha256", fallback="")
    return cfg


class TransferQueue:
    def __init__(self, db_path: Path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(SCHEMA)
            self._conn.commit()

    def enqueue(self, flow, filename, size, src_mtime) -> bool:
        ts = now()
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO transfers (flow, filename, size, src_mtime, status, attempts, next_attempt, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?)",
                (flow, filename, size, src_mtime, PENDING, ts, ts))
            self._conn.commit()
            return cur.rowcount > 0

    def next_pending(self, flow, max_retries):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM transfers WHERE flow=? AND status=? AND attempts<? AND next_attempt<=? ORDER BY id LIMIT 1",
                (flow, PENDING, max_retries, now())).fetchone()
            return dict(row) if row else None

    def mark(self, row_id, status, attempts=None, next_attempt=None, last_error=None):
        with self._lock:
            self._conn.execute(
                "UPDATE transfers SET status=?, attempts=COALESCE(?, attempts), next_attempt=COALESCE(?, next_attempt), last_error=?, updated_at=? WHERE id=?",
                (status, attempts, next_attempt, last_error, now(), row_id))
            self._conn.commit()

    def counts(self, flow) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS c FROM transfers WHERE flow=? GROUP BY status", (flow,)).fetchall()
            return {r["status"]: r["c"] for r in rows}

    def close(self):
        with self._lock:
            self._conn.close()


class StabilityTracker:
    def __init__(self, required_scans: int):
        self.required = max(1, required_scans)
        self._state = {}

    def update(self, name, size) -> bool:
        entry = self._state.get(name)
        if entry is None or entry[0] != size:
            self._state[name] = [size, 0]
            return False
        entry[1] += 1
        return entry[1] >= self.required - 1

    def forget_missing(self, current_names):
        for name in list(self._state):
            if name not in current_names:
                del self._state[name]

    def discard(self, name):
        self._state.pop(name, None)


class SftpEndpoint:
    def __init__(self, cfg: SftpConfig):
        self.cfg = cfg
        self._client = None
        self._sftp = None

    def _connect(self):
        if paramiko is None:
            raise RuntimeError("paramiko non installe (pip install -r requirements.txt)")
        pkey = None
        last_exc = None
        for key_cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey, paramiko.DSSKey):
            try:
                pkey = key_cls.from_private_key_file(self.cfg.key_file)
                break
            except Exception as exc:
                last_exc = exc
        if pkey is None:
            raise RuntimeError(f"cle {self.cfg.key_file} illisible: {last_exc}")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(
            hostname=self.cfg.host,
            port=self.cfg.port,
            username=self.cfg.user,
            pkey=pkey,
            look_for_keys=False,
            allow_agent=False,
            timeout=30,
            banner_timeout=30,
            auth_timeout=30,
        )
        server_key = client.get_transport().get_remote_server_key()
        received = fingerprint_of(server_key)
        expected = normalize_fingerprint(self.cfg.host_fingerprint)
        if expected and received != expected:
            client.close()
            raise RuntimeError(
                f"empreinte du serveur {self.cfg.host} invalide: attendu {expected}, recu {received}")
        self._client = client
        self._sftp = client.open_sftp()

    def _close(self):
        for obj in (self._sftp, self._client):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self._sftp = None
        self._client = None

    def _ensure(self):
        transport = self._client.get_transport() if self._client is not None else None
        if transport is not None and transport.is_active() and self._sftp is not None:
            return
        self._close()
        self._connect()

    def _call(self, fn, *args):
        last = None
        for _ in range(2):
            try:
                self._ensure()
                return fn(self._sftp, *args)
            except SSH_ERRORS as exc:
                last = exc
                self._close()
        raise last

    def list_files(self, remote_dir):
        def fn(sftp, d):
            out = []
            for attr in sftp.listdir_attr(d):
                if statmod.S_ISDIR(attr.st_mode or 0):
                    continue
                out.append((attr.filename, attr.st_size or 0, int(attr.st_mtime or 0)))
            return out
        return self._call(fn, remote_dir)

    def stat(self, path):
        def fn(sftp, p):
            try:
                return sftp.stat(p)
            except FileNotFoundError:
                return None
        return self._call(fn, path)

    def exists(self, path) -> bool:
        return self.stat(path) is not None

    def get(self, remote, local):
        return self._call(lambda sftp, r, l: sftp.get(r, l), remote, local)

    def put(self, local, remote):
        return self._call(lambda sftp, l, r: sftp.put(l, r), local, remote)

    def rename(self, old, new):
        return self._call(lambda sftp, o, n: sftp.rename(o, n), old, new)

    def remove(self, path):
        def fn(sftp, p):
            try:
                sftp.remove(p)
            except FileNotFoundError:
                pass
        return self._call(fn, path)

    def mkdirs(self, path):
        def fn(sftp, p):
            parts = [seg for seg in p.split("/") if seg]
            cur = "/" if p.startswith("/") else "."
            for seg in parts:
                cur = posixpath.join(cur, seg)
                try:
                    sftp.mkdir(cur)
                except OSError as exc:
                    if exc.errno != errno.EEXIST:
                        raise
        return self._call(fn, path)


class FlowWorker(threading.Thread):
    def __init__(self, flow: FlowConfig, endpoints: dict, queue: TransferQueue, cfg: Config, log, stop_event):
        super().__init__(name=f"flow-{flow.name}", daemon=True)
        self.flow = flow
        self.src = endpoints[flow.source]
        self.dst = endpoints[flow.dest]
        self.queue = queue
        self.cfg = cfg
        self.log = log
        self.stop_event = stop_event
        self.tracker = StabilityTracker(cfg.stability_scans)
        self.staging_dir = Path(cfg.state_dir) / "staging" / flow.name
        self.heartbeat_path = Path(cfg.state_dir) / f"heartbeat_{flow.name}"

    def run(self):
        self.log.info("flow demarre", extra={"flow": self.flow.name, "event": "flow_start"})
        while not self.stop_event.is_set():
            try:
                self.cycle()
            except Exception as exc:
                self.log.error("erreur de cycle", extra={
                    "flow": self.flow.name, "event": "cycle_error", "error": str(exc)})
            self._heartbeat()
            self.stop_event.wait(self.cfg.scan_interval_sec)

    def cycle(self):
        try:
            entries = self.src.list_files(self.flow.source_dir)
        except Exception:
            self.src._close()
            raise
        names = set()
        for name, size, mtime in entries:
            if is_ignored_name(name):
                continue
            names.add(name)
            if self.tracker.update(name, size):
                if self.queue.enqueue(self.flow.name, name, size, mtime):
                    self.log.info("nouveau fichier detecte", extra={
                        "flow": self.flow.name, "event": "detected", "filename": name, "size": size})
                self.tracker.discard(name)
        self.tracker.forget_missing(names)
        self.process_pending()

    def process_pending(self):
        while not self.stop_event.is_set():
            row = self.queue.next_pending(self.flow.name, self.cfg.max_retries)
            if row is None:
                break
            self.process(row)

    def process(self, row):
        flow = self.flow.name
        fname = row["filename"]
        src_path = posixpath.join(self.flow.source_dir, fname)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        local = self.staging_dir / (fname + ".part")
        try:
            src_attr = self.src.stat(src_path)
            if src_attr is None:
                self.queue.mark(row["id"], DONE)
                self.log.warning("fichier source disparu entre-temps, marque done", extra={
                    "flow": flow, "event": "vanished", "filename": fname})
                return
            self.src.get(src_path, str(local))
            local_size = local.stat().st_size
            if local_size != src_attr.st_size:
                raise IOError(f"taille local {local_size} != taille source {src_attr.st_size}")
            existing = self.dst.stat(posixpath.join(self.flow.dest_dir, fname))
            existing_size = existing.st_size if existing is not None else None
            dest_name, skip = resolve_dest_name(fname, local_size, existing_size, now())
            final_remote = posixpath.join(self.flow.dest_dir, dest_name)
            if not skip:
                tmp_remote = posixpath.join(self.flow.dest_dir, "." + dest_name + ".tmp")
                self.dst.put(str(local), tmp_remote)
                tmp_attr = self.dst.stat(tmp_remote)
                tmp_size = tmp_attr.st_size if tmp_attr is not None else None
                if tmp_size != local_size:
                    raise IOError(f"taille upload {tmp_size} != taille local {local_size}")
                self.dst.rename(tmp_remote, final_remote)
            else:
                self.log.info("fichier deja livre (meme taille), on archive la source", extra={
                    "flow": flow, "event": "already_delivered", "filename": fname})
            self._archive_source(src_path, fname)
            self.queue.mark(row["id"], DONE, last_error=None)
            self.log.info("transfert OK", extra={
                "flow": flow, "event": "transferred", "filename": fname,
                "dest": final_remote, "size": local_size})
        except Exception as exc:
            attempts = row["attempts"] + 1
            if attempts >= self.cfg.max_retries:
                self.queue.mark(row["id"], FAILED, attempts=attempts, last_error=str(exc))
                self.log.error("transfert en echec definitif", extra={
                    "flow": flow, "event": "failed", "filename": fname,
                    "attempts": attempts, "error": str(exc)})
            else:
                delay = backoff_delay(attempts)
                self.queue.mark(row["id"], PENDING, attempts=attempts,
                                next_attempt=now() + delay, last_error=str(exc))
                self.log.warning("transfert en erreur, nouvelle tentative programmee", extra={
                    "flow": flow, "event": "retry", "filename": fname,
                    "attempts": attempts, "delay_sec": delay, "error": str(exc)})
        finally:
            try:
                local.unlink()
            except FileNotFoundError:
                pass

    def _archive_source(self, src_path, fname):
        self.src.mkdirs(self.flow.archive_dir)
        dest = posixpath.join(self.flow.archive_dir, fname)
        if self.src.exists(dest):
            dest = posixpath.join(self.flow.archive_dir, f"{fname}.dup-{now()}")
        self.src.rename(src_path, dest)

    def _heartbeat(self):
        try:
            self.heartbeat_path.write_text(datetime.now(timezone.utc).isoformat())
        except OSError:
            pass


class PurgeWorker(threading.Thread):
    def __init__(self, flows, endpoints, cfg: Config, log, stop_event, interval_sec=3600):
        super().__init__(name="purge", daemon=True)
        self.flows = flows
        self.endpoints = endpoints
        self.cfg = cfg
        self.log = log
        self.stop_event = stop_event
        self.interval_sec = interval_sec

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.purge_all()
            except Exception as exc:
                self.log.error("erreur purge", extra={"event": "purge_error", "error": str(exc)})
            self.stop_event.wait(self.interval_sec)

    def purge_all(self):
        cutoff = time.time() - self.cfg.archive_retention_days * 86400
        for flow in self.flows:
            endpoint = self.endpoints[flow.source]
            try:
                entries = endpoint.list_files(flow.archive_dir)
            except Exception:
                continue
            for name, _size, mtime in entries:
                if mtime < cutoff:
                    endpoint.remove(posixpath.join(flow.archive_dir, name))
                    self.log.info("archive purgee", extra={
                        "flow": flow.name, "event": "purged", "filename": name})


class EventRing:
    def __init__(self, maxlen=200):
        self._events = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, item):
        with self._lock:
            self._events.append(item)

    def recent(self, n=50):
        with self._lock:
            return list(self._events)[-n:][::-1]


class RingHandler(logging.Handler):
    def __init__(self, ring: EventRing):
        super().__init__()
        self.ring = ring

    def emit(self, record):
        self.ring.add({
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "msg": record.getMessage(),
            "flow": getattr(record, "flow", None),
            "event": getattr(record, "event", None),
            "filename": getattr(record, "filename", None),
        })


def read_heartbeat(state_dir: str, flow_name: str):
    path = Path(state_dir) / f"heartbeat_{flow_name}"
    try:
        return datetime.fromisoformat(path.read_text().strip())
    except Exception:
        return None


def flow_status(queue: TransferQueue, flow: FlowConfig, state_dir: str, scan_interval_sec: int) -> dict:
    counts = queue.counts(flow.name)
    hb = read_heartbeat(state_dir, flow.name)
    age = (datetime.now(timezone.utc) - hb).total_seconds() if hb is not None else None
    failed = counts.get(FAILED, 0)
    if failed > 0:
        status = "error"
    elif hb is None:
        status = "unknown"
    elif age > max(3 * scan_interval_sec, 30):
        status = "stale"
    else:
        status = "ok"
    return {
        "name": flow.name,
        "source": f"{flow.source}:{flow.source_dir}",
        "dest": f"{flow.dest}:{flow.dest_dir}",
        "heartbeat": hb.isoformat() if hb else None,
        "heartbeat_age_sec": round(age, 1) if age is not None else None,
        "counts": counts,
        "status": status,
    }


def build_status(queue, flows, events: EventRing, state_dir: str, scan_interval_sec: int, started_at: float) -> dict:
    return {
        "service": "edi-sync",
        "uptime_sec": round(time.time() - started_at, 1),
        "flows": {f.name: flow_status(queue, f, state_dir, scan_interval_sec) for f in flows},
        "events": events.recent(30),
    }


def render_page(status: dict) -> str:
    def esc(v):
        return html.escape(str(v)) if v is not None else ""
    cls_map = {"ok": "ok", "stale": "warn", "error": "err", "unknown": "warn"}
    rows = []
    for fs in status["flows"].values():
        cls = cls_map.get(fs["status"], "warn")
        counts = fs["counts"]
        rows.append(
            f"<tr class='{cls}'><td>{esc(fs['name'])}</td><td>{esc(fs['source'])}</td>"
            f"<td>{esc(fs['dest'])}</td><td>{esc(fs['heartbeat_age_sec'])}</td>"
            f"<td>{counts.get('pending', 0)}</td><td>{counts.get('done', 0)}</td>"
            f"<td>{counts.get('failed', 0)}</td><td>{esc(fs['status'])}</td></tr>")
    items = "".join(
        f"<li><span class='ts'>{esc(e.get('ts'))}</span> <span class='lvl'>{esc(e.get('level'))}</span> "
        f"[{esc(e.get('flow') or '-')}] <b>{esc(e.get('event') or '')}</b> "
        f"{esc(e.get('filename') or '')} {esc(e.get('msg') or '')}</li>"
        for e in status["events"])
    css = ("body{font-family:system-ui,sans-serif;margin:2rem;background:#f6f7f9;color:#222}"
           "h1{font-size:1.3rem} table{border-collapse:collapse;background:#fff;width:100%}"
           "th,td{border:1px solid #ddd;padding:.4rem .6rem;text-align:left;font-size:.9rem}"
           "th{background:#eee} tr.warn{background:#fff8e1} tr.err{background:#fdecea}"
           "ul{font-family:monospace;font-size:.8rem;background:#fff;border:1px solid #ddd;"
           "padding:1rem;max-height:40vh;overflow:auto} .ts{color:#888} .lvl{color:#555}")
    return (
        "<!doctype html><html lang='fr'><head><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='10'><title>edi-sync — monitoring</title>"
        f"<style>{css}</style></head><body>"
        "<h1>edi-sync — monitoring des flux</h1>"
        f"<p>Uptime : {status['uptime_sec']} s — page rafraîchie toutes les 10 s.</p>"
        "<table><tr><th>Flow</th><th>Source</th><th>Destination</th><th>Heartbeat (s)</th>"
        "<th>pending</th><th>done</th><th>failed</th><th>État</th></tr>"
        f"{''.join(rows)}</table>"
        "<h2>Derniers événements</h2>"
        f"<ul>{items}</ul></body></html>")


class MonitoringHandler(BaseHTTPRequestHandler):
    server_version = "edi-sync-monitor/1.0"

    def log_message(self, *args):
        pass

    def _authorized(self) -> bool:
        mcfg = self.server.mcfg
        if not mcfg.auth_user:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except Exception:
            return False
        user, _, password = decoded.partition(":")
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return user == mcfg.auth_user and digest == mcfg.auth_password_sha256

    def _send(self, code, body, content_type="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _deny(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="edi-sync"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self._authorized():
            return self._deny()
        path = self.path.split("?")[0]
        if path == "/health":
            return self._send(200, "ok", "text/plain")
        if path in ("/", "/index.html", "/api/status"):
            status = build_status(self.server.queue, self.server.flows, self.server.events,
                                  self.server.state_dir, self.server.scan_interval_sec,
                                  self.server.started_at)
            if path == "/api/status":
                return self._send(200, json.dumps(status, ensure_ascii=False, indent=2),
                                  "application/json")
            return self._send(200, render_page(status))
        return self._send(404, "not found", "text/plain")


def ensure_tls_cert(cert_path: Path, key_path: Path):
    if cert_path.exists() and key_path.exists():
        return str(cert_path), str(key_path)
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key_path), "-out", str(cert_path),
         "-days", "365", "-subj", "/CN=edi-sync-monitoring"],
        check=True, capture_output=True)
    os.chmod(key_path, 0o600)
    return str(cert_path), str(key_path)


class MonitoringServer(threading.Thread):
    def __init__(self, mcfg, queue, flows, events, state_dir, scan_interval_sec, started_at,
                 cert=None, key=None, use_tls=True):
        super().__init__(name="monitoring", daemon=True)
        self.mcfg = mcfg
        self.queue = queue
        self.flows = flows
        self.events = events
        self.state_dir = state_dir
        self.scan_interval_sec = scan_interval_sec
        self.started_at = started_at
        self.cert = cert
        self.key = key
        self.use_tls = use_tls
        self.httpd = None

    def run(self):
        httpd = ThreadingHTTPServer((self.mcfg.bind, self.mcfg.port), MonitoringHandler)
        httpd.mcfg = self.mcfg
        httpd.queue = self.queue
        httpd.flows = self.flows
        httpd.events = self.events
        httpd.state_dir = self.state_dir
        httpd.scan_interval_sec = self.scan_interval_sec
        httpd.started_at = self.started_at
        if self.use_tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(self.cert, self.key)
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        self.httpd = httpd
        httpd.serve_forever(poll_interval=0.5)

    def stop(self):
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        for key in ("flow", "event", "filename", "size", "dest", "attempts", "delay_sec", "error", "signal"):
            if key in record.__dict__:
                payload[key] = record.__dict__[key]
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(events: EventRing = None):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [handler]
    if events is not None:
        root.addHandler(RingHandler(events))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Daemon de transfert SFTP bidirectionnel de fichiers EDI")
    ap.add_argument("--config", default="/etc/edi-sync/config.ini", help="chemin du fichier config.ini")
    ap.add_argument("--check", action="store_true", help="valide la configuration puis quitte")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except Exception as exc:
        print(f"config invalide: {exc}", file=sys.stderr)
        return 2

    if args.check:
        print(f"config OK: {len(cfg.sftps)} endpoint(s) sftp, {len(cfg.flows)} flow(s): "
              f"{[f.name for f in cfg.flows]}")
        return 0

    events = EventRing()
    setup_logging(events)
    log = logging.getLogger("edi-sync")

    state = Path(cfg.state_dir)
    (state / "staging").mkdir(parents=True, exist_ok=True)
    queue = TransferQueue(state / "queue.db")
    endpoints = {name: SftpEndpoint(c) for name, c in cfg.sftps.items()}
    started_at = time.time()

    stop_event = threading.Event()

    def handle_signal(signum, _frame):
        log.info("signal recu, arret demande", extra={"event": "shutdown", "signal": signum})
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    workers = [FlowWorker(f, endpoints, queue, cfg, log, stop_event) for f in cfg.flows]
    purge = PurgeWorker(cfg.flows, endpoints, cfg, log, stop_event)
    for w in workers:
        w.start()
    purge.start()

    monitor = None
    if cfg.monitoring.enabled:
        try:
            mcfg = cfg.monitoring
            cert = mcfg.tls_cert or str(state / "certs" / "monitoring.crt")
            key = mcfg.tls_key or str(state / "certs" / "monitoring.key")
            cert, key = ensure_tls_cert(Path(cert), Path(key))
            monitor = MonitoringServer(mcfg, queue, cfg.flows, events, cfg.state_dir,
                                       cfg.scan_interval_sec, started_at, cert=cert, key=key)
            monitor.start()
            log.info("monitoring HTTPS demarre", extra={
                "event": "monitoring_start", "bind": mcfg.bind, "port": mcfg.port})
        except Exception as exc:
            log.error("monitoring non demarre, le transfert continue", extra={
                "event": "monitoring_error", "error": str(exc)})
            monitor = None

    log.info("daemon demarre", extra={"event": "startup", "flows": [f.name for f in cfg.flows]})

    while not stop_event.is_set():
        stop_event.wait(1)

    for w in workers:
        w.join(timeout=30)
    purge.join(timeout=10)
    if monitor is not None:
        monitor.stop()
        monitor.join(timeout=5)
    queue.close()
    log.info("daemon arrete", extra={"event": "stopped"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
