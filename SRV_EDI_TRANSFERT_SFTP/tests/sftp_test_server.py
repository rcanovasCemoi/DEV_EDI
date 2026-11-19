#!/usr/bin/env python3
"""Deux petits serveurs SFTP de test en loopback (paramiko), pour le test E2E local.

Usage : python3 tests/sftp_test_server.py --root /tmp/xxx [--port1 2221] [--port2 2222]
Si la variable d'environnement PLATFORM_WATCHER=1 est définie, un thread simule une
plateforme EDI qui déplace instantanément les fichiers .tmp déposés dans in/.
"""

import argparse
import glob
import os
import socket
import threading
import time

import paramiko


class StubSFTPServer(paramiko.SFTPServerInterface):
    def __init__(self, server, root):
        super().__init__(server)
        self.root = os.path.realpath(root)

    def _real(self, path):
        p = os.path.realpath(os.path.join(self.root, path.lstrip("/")))
        if p != self.root and not p.startswith(self.root + os.sep):
            raise PermissionError("path escape")
        return p

    def list_folder(self, path):
        real = self._real(path)
        out = []
        for fname in sorted(os.listdir(real)):
            attr = paramiko.SFTPAttributes.from_stat(os.stat(os.path.join(real, fname)))
            attr.filename = fname
            out.append(attr)
        return out

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(self._real(path)))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def lstat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.lstat(self._real(path)))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def open(self, path, flags, attr=None):
        real = self._real(path)
        try:
            flags |= getattr(os, "O_BINARY", 0)
            mode = getattr(attr, "st_mode", None) or 0o666
            fd = os.open(real, flags, mode)
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        if flags & os.O_TRUNC:
            os.ftruncate(fd, 0)
        if flags & os.O_WRONLY:
            fstr = "ab" if flags & os.O_APPEND else "wb"
        elif flags & os.O_RDWR:
            fstr = "a+b" if flags & os.O_APPEND else "r+b"
        else:
            fstr = "rb"
        f = os.fdopen(fd, fstr)
        h = paramiko.SFTPHandle(flags)
        h.filename = real
        h.readfile = f
        h.writefile = f
        return h

    def mkdir(self, path, attr=None):
        try:
            os.mkdir(self._real(path))
            return paramiko.SFTP_OK
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def remove(self, path):
        try:
            os.remove(self._real(path))
            return paramiko.SFTP_OK
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def rename(self, oldpath, newpath):
        try:
            os.rename(self._real(oldpath), self._real(newpath))
            return paramiko.SFTP_OK
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)


class Server(paramiko.ServerInterface):
    def __init__(self, authorized):
        self.authorized = authorized

    def check_auth_publickey(self, username, key):
        return paramiko.AUTH_SUCCESSFUL if key.get_fingerprint() in self.authorized else paramiko.AUTH_FAILED

    def check_auth_password(self, username, password):
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "publickey"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


def handle(conn, host_key, root, authorized):
    try:
        t = paramiko.Transport(conn)
        t.add_server_key(host_key)
        t.set_subsystem_handler("sftp", paramiko.SFTPServer, StubSFTPServer, root)
        t.start_server(server=Server(authorized))
        chan = t.accept(30)
        if chan is None:
            return
        while True:
            time.sleep(1)
    except Exception:
        pass


def serve(port, host_key, root, authorized, ready):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(16)
    ready.set()
    print(f"SFTP up on 127.0.0.1:{port} root={root}", flush=True)
    while True:
        conn, _ = sock.accept()
        threading.Thread(target=handle, args=(conn, host_key, root, authorized), daemon=True).start()


def platform_watcher(root, stop):
    while not stop.is_set():
        for d in (os.path.join(root, "sftp1", "in"), os.path.join(root, "sftp2", "in")):
            for p in glob.glob(os.path.join(d, ".*.tmp")):
                try:
                    os.rename(p, p + ".traite")
                except OSError:
                    pass
        time.sleep(0.15)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--port1", type=int, default=2221)
    ap.add_argument("--port2", type=int, default=2222)
    ap.add_argument("--keys", required=True, help="dossier des cles (host_sftp1, host_sftp2, client_sftp1, client_sftp2)")
    args = ap.parse_args()

    os.makedirs(os.path.join(args.root, "sftp1", "out"), exist_ok=True)
    os.makedirs(os.path.join(args.root, "sftp1", "in"), exist_ok=True)
    os.makedirs(os.path.join(args.root, "sftp2", "out"), exist_ok=True)
    os.makedirs(os.path.join(args.root, "sftp2", "in"), exist_ok=True)

    r1, r2 = threading.Event(), threading.Event()
    threading.Thread(target=serve, args=(args.port1, paramiko.RSAKey(filename=os.path.join(args.keys, "host_sftp1")),
                                         os.path.join(args.root, "sftp1"), {paramiko.RSAKey(filename=os.path.join(args.keys, "client_sftp1")).get_fingerprint()}, r1), daemon=True).start()
    threading.Thread(target=serve, args=(args.port2, paramiko.RSAKey(filename=os.path.join(args.keys, "host_sftp2")),
                                         os.path.join(args.root, "sftp2"), {paramiko.RSAKey(filename=os.path.join(args.keys, "client_sftp2")).get_fingerprint()}, r2), daemon=True).start()
    r1.wait(5)
    r2.wait(5)
    if os.environ.get("PLATFORM_WATCHER") == "1":
        threading.Thread(target=platform_watcher, args=(args.root, threading.Event()), daemon=True).start()
        print("platform watcher ON", flush=True)
    print("ready", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
