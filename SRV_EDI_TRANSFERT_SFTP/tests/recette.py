#!/usr/bin/env python3
"""Recette de fonctionnement du daemon edi-sync (a lancer sur le serveur EDI).

Usage :
  python3 tests/recette.py --config /opt/EDI/edi-sync/config.ini
  python3 tests/recette.py --config ... --flow flow_keane_to_babelway --monitoring-password '***'

Verifie : config valide, service actif, monitoring /health + /api/status, heartbeats,
puis un vrai transfert de test (fichier TEST_EDI_SYNC_<ts>.edi) par flow :
depot source -> livraison dest -> suppression source -> archive locale.
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
    print(("PASS" if cond else "FAIL"), "-", name, ("| " + str(detail)) if detail else "", flush=True)
    return bool(cond)


def skip(name, detail=""):
    print("SKIP -", name, ("| " + str(detail)) if detail else "", flush=True)


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
    fname = f"TEST_EDI_SYNC_{ts}.edi"
    content = f"UNB+UNOA:1+RECETTE+TEST+{ts}++++\nUNH+1+TEST:D:96A:UN'\nUNT+2+1'\nUNZ+1+1'\n"
    src_path = posixpath.join(flow.source_dir, fname)
    dst_path = posixpath.join(flow.dest_dir, fname)
    with tempfile.NamedTemporaryFile("w", suffix=".edi", delete=False) as tf:
        tf.write(content)
        tmp = tf.name
    try:
        src.put(tmp, src_path)
        check(f"[{flow.name}] depot du fichier test dans {flow.source_dir}", True, fname)
    except OSError as e:
        if e.errno == 13:
            skip(f"[{flow.name}] depot du fichier test",
                 f"read-only sur {flow.source_dir} (compte SFTP) - tester ce flow via depot manuel plateforme ou l'autre sens")
            return True
        return check(f"[{flow.name}] depot du fichier test", False, str(e))
    finally:
        os.unlink(tmp)
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
    ok = check(f"[{flow.name}] transfert OK (source supprimee)", src_gone, src_path)
    if dst_visible:
        ok &= check(f"[{flow.name}] fichier visible dans {flow.dest_dir}", True, dst_path)
    else:
        ok &= check(f"[{flow.name}] fichier livre (deplace par la plateforme, non visible au stat)", src_gone, dst_path)
    if cfg.local_archive_dir:
        arch = Path(cfg.local_archive_dir) / flow.name / fname
        ok &= check(f"[{flow.name}] archive locale presente", arch.exists(), arch)
    return ok


def main():
    ap = argparse.ArgumentParser(description="Recette de fonctionnement du daemon edi-sync")
    ap.add_argument("--config", default="/opt/EDI/edi-sync/config.ini")
    ap.add_argument("--flow", action="append", help="flow a tester (defaut: tous)")
    ap.add_argument("--monitoring-password", default="", help="mot de passe Basic Auth du monitoring")
    ap.add_argument("--timeout", type=int, default=90, help="delai d'attente par transfert (s)")
    ap.add_argument("--no-service-check", action="store_true", help="ignorer le check systemctl (tests locaux)")
    args = ap.parse_args()

    try:
        cfg = es.load_config(args.config)
        check("config valide", True, f"{len(cfg.sftps)} endpoint(s) sftp, flows={[f.name for f in cfg.flows]}")
    except Exception as e:
        check("config valide", False, str(e))
        print("RECETTE: ECHEC")
        return 1

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
        with urllib.request.urlopen(base + "/health", context=ctx, timeout=5, ) as r:
            check("GET /health", r.status == 200 and r.read().decode().strip() == "ok")
    except urllib.error.HTTPError as e:
        check("GET /health repond", e.code == 401,
              f"HTTP {e.code} (401 = Basic Auth active -> relancer avec --monitoring-password)")
    except Exception as e:
        check("GET /health", False, str(e))
    try:
        req = urllib.request.Request(base + "/api/status", headers=headers)
        with urllib.request.urlopen(req, context=ctx, timeout=5) as r:
            status = json.loads(r.read().decode())
        for name, fs in status["flows"].items():
            check(f"flow {name}: statut", fs["status"] in ("ok", "stale"),
                  f"{fs['status']} counts={fs['counts']} heartbeat_age={fs['heartbeat_age_sec']}s")
    except Exception as e:
        check("GET /api/status", False, str(e))

    for f in cfg.flows:
        p = Path(cfg.state_dir) / f"heartbeat_{f.name}"
        age = time.time() - p.stat().st_mtime if p.exists() else None
        check(f"heartbeat local {f.name}", age is not None and age < 60,
              f"age={None if age is None else round(age, 1)}s")

    flows = [f for f in cfg.flows if not args.flow or f.name in args.flow]
    for flow in flows:
        test_flow(cfg, flow, args.timeout)

    ok = all(RESULTS)
    print(f"\nRECETTE: {'OK' if ok else 'ECHEC'} ({sum(RESULTS)}/{len(RESULTS)} checks)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
