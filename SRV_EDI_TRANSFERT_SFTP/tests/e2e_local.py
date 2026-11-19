#!/usr/bin/env python3
"""Test E2E local du daemon edi-sync : lance 2 serveurs SFTP de test en loopback
(tests/sftp_test_server.py, avec simulateur de plateforme), le vrai daemon, puis joue
des scenarios complets (transfert A->B et B->A, suppression source, archive locale,
stabilite, doublon, conflit, monitoring HTTPS, arret propre, reprise apres redemarrage).

Usage : python3 tests/e2e_local.py     (necessite paramiko)
Affiche ✅/❌ par check et un bandeau final. Exit 0 si tout passe.
"""

import base64
import hashlib
import json
import os
import signal
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import paramiko

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src" / "edi_sync.py"
SERVER = HERE / "sftp_test_server.py"

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(("✅" if cond else "❌"), name, ("| " + str(detail)) if detail else "", flush=True)
    return bool(cond)


def wait_for(pred, timeout=20, step=0.3):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


def gen_keys(keys_dir):
    for name in ["host_sftp1", "host_sftp2", "client_sftp1", "client_sftp2"]:
        path = keys_dir / name
        if not path.exists():
            key = paramiko.RSAKey.generate(2048)
            key.write_private_key_file(str(path))
            with open(str(path) + ".pub", "w") as f:
                f.write(f"{key.get_name()} {key.get_base64()}\n")
        os.chmod(path, 0o600)


def host_fp(keys_dir, name):
    key = paramiko.RSAKey(filename=str(keys_dir / name))
    return "sha256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


def write_config(path, keys_dir, state_dir, archive_dir, port1, port2, mon_port):
    config = f"""[general]
scan_interval_sec = 1
stability_scans = 2
max_retries = 3
archive_retention_days = 7
state_dir = {state_dir}
local_archive_dir = {archive_dir}

[sftp1]
host = 127.0.0.1
port = {port1}
user = edi
key_file = {keys_dir}/client_sftp1
host_fingerprint = {host_fp(keys_dir, 'host_sftp1')}

[sftp2]
host = 127.0.0.1
port = {port2}
user = edi
key_file = {keys_dir}/client_sftp2
host_fingerprint = {host_fp(keys_dir, 'host_sftp2')}

[flow_a]
source = sftp1
source_dir = /out
dest = sftp2
dest_dir = /in

[flow_b]
source = sftp2
source_dir = /out
dest = sftp1
dest_dir = /in
dest_tmp_suffix =

[monitoring]
enabled = true
bind = 127.0.0.1
port = {mon_port}
"""
    Path(path).write_text(config)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="edi-e2e-"))
    keys = tmp / "keys"
    keys.mkdir()
    state = tmp / "state"
    state.mkdir()
    arch = tmp / "ARCHIVE"
    s1 = tmp / "sftp1"
    s2 = tmp / "sftp2"
    gen_keys(keys)
    port1, port2, mon_port = 12221, 12222, 18443
    cfg_path = tmp / "config.ini"
    write_config(cfg_path, keys, state, arch, port1, port2, mon_port)

    env = dict(os.environ, PLATFORM_WATCHER="1")
    srv = subprocess.Popen([sys.executable, str(SERVER), "--root", str(tmp),
                            "--port1", str(port1), "--port2", str(port2), "--keys", str(keys)],
                           env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)

    daemon = subprocess.Popen([sys.executable, str(SRC), "--config", str(cfg_path)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(2.5)
        check("daemon demarre", daemon.poll() is None)

        print("-- flow_a: sftp1/out -> sftp2/in, suppression source, archive locale", flush=True)
        content_a = "UNB+UNOA:1+A+B+261105:0900+1++++\nUNH+1+ORDERS:D:96A:UN'\nUNT+3+1'\nUNZ+1+1'\n"
        (s1 / "out" / "ORDERS_A.edi").write_text(content_a)
        ok = wait_for(lambda: (s2 / "in" / "ORDERS_A.edi").exists(), 20)
        check("flow_a: livre dans sftp2/in, contenu identique", ok and (s2 / "in" / "ORDERS_A.edi").read_text() == content_a)
        check("flow_a: source supprimee", wait_for(lambda: not (s1 / "out" / "ORDERS_A.edi").exists(), 10))
        check("flow_a: archive locale", (arch / "flow_a" / "ORDERS_A.edi").exists()
              and (arch / "flow_a" / "ORDERS_A.edi").read_text() == content_a)

        print("-- flow_b (upload direct): sftp2/out -> sftp1/in", flush=True)
        content_b = "UNB+UNOA:1+B+A+261105:0901+2++++\nUNH+1+DESADV:D:96A:UN'\nUNT+2+1'\nUNZ+1+2'\n"
        (s2 / "out" / "DESADV_B.edi").write_text(content_b)
        ok = wait_for(lambda: (s1 / "in" / "DESADV_B.edi").exists(), 20)
        check("flow_b: livre dans sftp1/in au nom final (pas de .tmp)", ok
              and not list((s1 / "in").glob(".*tmp*")))
        check("flow_b: source supprimee + archive locale", wait_for(lambda: not (s2 / "out" / "DESADV_B.edi").exists(), 10)
              and (arch / "flow_b" / "DESADV_B.edi").exists())

        print("-- stabilite (fichier ecrit lentement)", flush=True)
        slow = s1 / "out" / "SLOW_C.edi"
        full = ""
        with open(slow, "w") as f:
            for c in ["UNB+...\n", "UNH+1+X'\n", "UNT+2+1'\nUNZ+1+1'\n"]:
                f.write(c)
                f.flush()
                full += c
                time.sleep(0.3)
        time.sleep(0.3)
        check("stabilite: pas transfere pendant l'ecriture", not (s2 / "in" / "SLOW_C.edi").exists())
        check("stabilite: transfere apres, contenu intact", wait_for(lambda: (s2 / "in" / "SLOW_C.edi").exists(), 20)
              and (s2 / "in" / "SLOW_C.edi").read_text() == full)

        print("-- doublon et conflit de nom", flush=True)
        mtime_before = (s2 / "in" / "ORDERS_A.edi").stat().st_mtime_ns
        (s1 / "out" / "ORDERS_A.edi").write_text(content_a)
        check("doublon: source supprimee sans reupload", wait_for(lambda: not (s1 / "out" / "ORDERS_A.edi").exists(), 20))
        time.sleep(2)
        check("doublon: dest inchange, aucun .dup", (s2 / "in" / "ORDERS_A.edi").stat().st_mtime_ns == mtime_before
              and not list((s2 / "in").glob("ORDERS_A.edi.dup-*")) and not list((arch / "flow_a").glob("*.dup-*")))
        (s2 / "out" / "DESADV_B.edi").write_text(content_b + "CONFLIT\n")
        check("conflit: upload en .dup-<ts>", wait_for(lambda: list((s1 / "in").glob("DESADV_B.edi.dup-*")), 20))

        print("-- monitoring HTTPS", flush=True)
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(f"https://127.0.0.1:{mon_port}/api/status", context=ctx, timeout=5) as r:
            status = json.loads(r.read().decode())
        check("monitoring: /api/status, 2 flows ok", all(f["status"] == "ok" for f in status["flows"].values()),
              json.dumps({k: v["counts"] for k, v in status["flows"].items()}))
        with urllib.request.urlopen(f"https://127.0.0.1:{mon_port}/health", context=ctx, timeout=5) as r:
            check("monitoring: /health = ok", r.read().decode().strip() == "ok")

        print("-- arret propre + reprise apres redemarrage", flush=True)
        daemon.send_signal(signal.SIGTERM)
        rc = daemon.wait(timeout=15)
        check("arret propre SIGTERM (rc=0)", rc == 0, f"rc={rc}")
        (s1 / "out" / "CRASH_D.edi").write_text("UNB+CRASH\nUNZ+1+1'\n")
        size = (s1 / "out" / "CRASH_D.edi").stat().st_size
        now = int(time.time())
        con = sqlite3.connect(str(state / "queue.db"))
        con.execute("INSERT OR IGNORE INTO transfers (flow, filename, size, src_mtime, status, attempts, next_attempt, created_at, updated_at)"
                    " VALUES ('flow_a', 'CRASH_D.edi', ?, 0, 'pending', 0, 0, ?, ?)", (size, now, now))
        con.commit()
        con.close()
        daemon = subprocess.Popen([sys.executable, str(SRC), "--config", str(cfg_path)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        check("reprise: pending repris apres redemarrage", wait_for(lambda: (s2 / "in" / "CRASH_D.edi").exists(), 25)
              and not (s1 / "out" / "CRASH_D.edi").exists() and (arch / "flow_a" / "CRASH_D.edi").exists())
        daemon.send_signal(signal.SIGTERM)
        daemon.wait(timeout=15)
    finally:
        for p in (daemon, srv):
            try:
                p.send_signal(signal.SIGTERM)
                p.wait(timeout=5)
            except Exception:
                p.kill()

    ok = all(RESULTS)
    print()
    print("=" * 64)
    print(("✅ E2E LOCAL VALIDE — " if ok else "❌ E2E LOCAL EN ECHEC — ") + f"{sum(RESULTS)}/{len(RESULTS)} checks OK")
    print("=" * 64)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
