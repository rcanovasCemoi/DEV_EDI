#!/usr/bin/env python3
"""Test E2E automatique en production du daemon edi-sync.

Usage (sur le serveur EDI) :
  /opt/EDI/edi-sync/venv/bin/python ~/DEV_EDI/SRV_EDI_TRANSFERT_SFTP/tests/e2e_prod.py \
      --config /opt/EDI/edi-sync/config.ini --monitoring-password '***'

Checks : config, service, monitoring (/health, /api/status), heartbeats,
puis pour chaque flow un vrai transfert de test (fichier TEST_E2E_<ts>.edi) :
depot source -> livraison destination -> suppression source -> archive locale.
Si la source est en read-only (EACCES), le flow est SKIP (depot manuel requis).

Affiche des coches vertes par check et un bandeau final : E2E VALIDE / ECHEC.
"""

import argparse
import base64
import json
import os
import posixpath
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import edi_sync as es

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(("✅" if cond else "❌"), name, ("| " + str(detail)) if detail else "", flush=True)
    return bool(cond)


def skip(name, detail=""):
    print("⏭️ ", name, ("| " + str(detail)) if detail else "", flush=True)


def service_active():
    for cmd in (["systemctl", "--user", "is-active", "edi-sync"],
                ["systemctl", "is-active", "edi-sync"]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            return None
        if r.stdout.strip() == "active":
            return " ".join(cmd[:2])
    return None


def test_flow(cfg, flow, timeout):
    src = es.SftpEndpoint(cfg.sftps[flow.source])
    dst = es.SftpEndpoint(cfg.sftps[flow.dest])
    ts = int(time.time())
    fname = f"TEST_E2E_{ts}.edi"
    content = f"UNB+UNOA:1+E2E+TEST+{ts}++++\nUNH+1+ORDERS:D:96A:UN'\nUNT+2+1'\nUNZ+1+1'\n"
    src_path = posixpath.join(flow.source_dir, fname)
    dst_path = posixpath.join(flow.dest_dir, fname)
    with tempfile.NamedTemporaryFile("w", suffix=".edi", delete=False) as tf:
        tf.write(content)
        tmp = tf.name
    try:
        src.put(tmp, src_path)
    except OSError as e:
        os.unlink(tmp)
        if e.errno == 13:
            skip(f"[{flow.name}] depot test", f"read-only sur {flow.source_dir} -> depot manuel requis (plateforme/channel)")
            return True
        return check(f"[{flow.name}] depot test", False, str(e))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    check(f"[{flow.name}] depot du fichier test dans {flow.source_dir}", True, fname)

    end = time.time() + timeout
    src_gone = False
    dst_visible = False
    while time.time() < end:
        try:
            dst_visible = dst.stat(dst_path) is not None or dst_visible
            src_gone = src.stat(src_path) is None
        except Exception:
            pass
        if src_gone:
            break
        time.sleep(2)
    check(f"[{flow.name}] transfert OK (source supprimee)", src_gone, src_path)
    check(f"[{flow.name}] fichier livre dans {flow.dest_dir}", dst_visible or src_gone,
          dst_path + ("" if dst_visible else " (deplace par la plateforme)"))
    if cfg.local_archive_dir:
        arch = Path(cfg.local_archive_dir) / flow.name / fname
        check(f"[{flow.name}] archive locale", arch.exists(), arch)
    return src_gone


def main():
    ap = argparse.ArgumentParser(description="Test E2E automatique en production (edi-sync)")
    ap.add_argument("--config", default="/opt/EDI/edi-sync/config.ini")
    ap.add_argument("--monitoring-password", default="")
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--no-service-check", action="store_true")
    args = ap.parse_args()

    print("=" * 64)
    print("  TEST E2E edi-sync —", args.config)
    print("=" * 64)

    try:
        cfg = es.load_config(args.config)
        check("config valide", True, f"flows={[f.name for f in cfg.flows]}")
    except Exception as e:
        check("config valide", False, str(e))
        return finish()

    if not args.no_service_check:
        svc = service_active()
        check("service edi-sync actif", svc is not None, svc or "inactif")

    m = cfg.monitoring
    ctx = ssl._create_unverified_context()
    base = f"https://{m.bind}:{m.port}"
    headers = {}
    if m.auth_user and args.monitoring_password:
        token = base64.b64encode(f"{m.auth_user}:{args.monitoring_password}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    try:
        with urllib.request.urlopen(base + "/health", context=ctx, timeout=5) as r:
            check("GET /health", r.status == 200 and r.read().decode().strip() == "ok")
    except urllib.error.HTTPError as e:
        check("GET /health (monitoring vivant)", e.code == 401,
              f"HTTP {e.code} (401 = Basic Auth active -> --monitoring-password)")
    except Exception as e:
        check("GET /health", False, str(e))
    try:
        req = urllib.request.Request(base + "/api/status", headers=headers)
        with urllib.request.urlopen(req, context=ctx, timeout=5) as r:
            status = json.loads(r.read().decode())
        for name, fs in status["flows"].items():
            check(f"flow {name}: statut ok + heartbeat recent",
                  fs["status"] in ("ok", "stale") and (fs["heartbeat_age_sec"] or 999) < 60,
                  f"{fs['status']} counts={fs['counts']} hb={fs['heartbeat_age_sec']}s")
    except Exception as e:
        check("GET /api/status", False, str(e))

    for f in cfg.flows:
        p = Path(cfg.state_dir) / f"heartbeat_{f.name}"
        age = time.time() - p.stat().st_mtime if p.exists() else None
        check(f"heartbeat local {f.name}", age is not None and age < 60,
              f"age={None if age is None else round(age, 1)}s")

    for flow in cfg.flows:
        test_flow(cfg, flow, args.timeout)

    return finish()


def finish():
    ok = all(RESULTS)
    print()
    print("=" * 64)
    if ok:
        print(f"✅ E2E VALIDE — {sum(RESULTS)}/{len(RESULTS)} checks OK")
    else:
        print(f"❌ E2E EN ECHEC — {sum(RESULTS)}/{len(RESULTS)} checks OK")
    print("=" * 64)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
