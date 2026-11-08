"""Связь с Graylog: журнал и новые устройства по GELF, выгрузка инвентаря для Lookup Tables.

Запуск: PYTHONPATH=lib python3 -m unittest tests/test_graylog.py
"""
import csv
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zlib
from http.server import ThreadingHTTPServer

from homerepo import config, db, gelf, web


def recv_all(sock, wait=1.0):
    sock.settimeout(wait)
    parts, out = {}, []
    while True:
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            return out
        if data[:2] == b"\x1e\x0f":
            mid, seq, cnt = data[2:10], data[10], data[11]
            parts.setdefault(mid, {})[seq] = data[12:]
            if len(parts[mid]) == cnt:
                got = parts.pop(mid)
                out.append(json.loads(zlib.decompress(b"".join(got[i] for i in range(cnt)))))
        else:
            out.append(json.loads(zlib.decompress(data)))


class Graylog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="hr-gl-")
        cls.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        cls.sock.bind(("127.0.0.1", 0))
        conf = os.path.join(cls.tmp, "homerepo.conf")
        with open(conf, "w") as f:
            f.write("[paths]\nstate_dir = %s/state\n[graylog]\ngelf_host = 127.0.0.1\ngelf_port = %d\n"
                    "export_token = s3cr3t\n" % (cls.tmp, cls.sock.getsockname()[1]))
        cls.cfg = config.Config(conf)
        gelf.reset(gelf.Sender.from_config(cls.cfg))
        web.APP = web.App(cls.cfg)
        con = web.C()
        now = time.time()
        con.execute("INSERT INTO hosts(name, fqdn, ip, os_name, os_version, secret_hash, enrolled) VALUES(?,?,?,?,?,?,?)",
                    ("redos-server", "redos-server.corp.example", "192.0.2.10", "RED OS", "8.0", "x", now))
        # агент за роутером: подключается с адреса роутера, а его настоящий адрес — в сведениях об интерфейсах
        con.execute("INSERT INTO hosts(name, fqdn, ip, os_name, os_version, secret_hash, enrolled, facts) VALUES(?,?,?,?,?,?,?,?)",
                    ("ws-1", "", "192.0.2.1", "RED OS", "8.0", "y", now, json.dumps({"interfaces": [
                        {"name": "eth0", "addr": "192.0.2.45/24", "mac": "aa"}, {"name": "podman0", "addr": "10.88.0.1/16", "mac": ""},
                        {"name": "eth0", "addr": "fe80::1/64", "mac": "aa"}]})))
        con.execute("INSERT INTO discovered(ip, mac, vendor, hostname, first_seen, last_seen, os_family) VALUES(?,?,?,?,?,?,?)",
                    ("192.0.2.10", "aa:bb:cc:00:00:01", "Intel", "srv", now, now, "linux"))
        con.execute("INSERT INTO discovered(ip, mac, vendor, hostname, first_seen, last_seen, os_family, manual_name) "
                    "VALUES(?,?,?,?,?,?,?,?)", ("192.0.2.1", "aa:bb:cc:00:00:02", "Keenetic", "", now, now, "router", "router-1"))
        con.execute("INSERT INTO discovered(ip, first_seen, last_seen) VALUES(?,?,?)", ("192.0.2.99", now, now))
        for ip in ("192.0.2.130", "192.0.2.131"):        # два устройства с одинаковой подписью
            con.execute("INSERT INTO discovered(ip, first_seen, last_seen, manual_name) VALUES(?,?,?,?)", (ip, now, now, "Android"))
        con.commit()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        gelf.reset()

    def get(self, path):
        try:
            r = urllib.request.urlopen(self.base + path, timeout=10)
            return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_audit_to_gelf(self):
        con = web.C()
        db.audit(con, "admin", "198.51.100.7", "login_failed", "")
        db.audit(con, "admin", "198.51.100.7", "resign_foreign", {"files": ["a.rpm"], "count": 1})
        db.audit(con, "admin", "198.51.100.7", "login", "")
        gelf.sender().flush()
        msgs = {m["_hr_action"]: m for m in recv_all(self.sock) if m.get("_hr_event") == "audit"}
        self.assertEqual(msgs["login_failed"]["level"], 4)
        self.assertEqual(msgs["login_failed"]["_hr_client_ip"], "198.51.100.7")
        self.assertEqual(msgs["login"]["level"], 6)
        self.assertNotIn(":  —", msgs["login"]["short_message"])       # у входа нет описания — берётся название действия
        self.assertEqual(msgs["resign_foreign"]["_hr_detail_count"], 1)
        self.assertEqual(msgs["resign_foreign"]["_hr_source"], "homerepo")
        self.assertTrue(msgs["resign_foreign"]["short_message"].startswith("HomeRepo: "))

    def test_device_new(self):
        gelf.device_new({"ip": "192.0.2.55", "mac": "aa:bb:cc:00:00:55", "vendor": "Raspberry", "hostname": "pi",
                         "ports": [22, 80], "os_family": "linux", "network": "192.0.2.0/24"})
        gelf.sender().flush()
        m = [x for x in recv_all(self.sock) if x.get("_hr_event") == "device_new"][0]
        self.assertEqual((m["_hr_ip"], m["_hr_ports"], m["level"]), ("192.0.2.55", "22, 80", 4))

    def test_export_inventory(self):
        self.assertEqual(self.get("/api/export/inventory.csv")[0], 403)
        self.assertEqual(self.get("/api/export/inventory.csv?token=bad")[0], 403)
        code, text = self.get("/api/export/inventory.csv?token=s3cr3t")
        self.assertEqual(code, 200)
        rows = list(csv.DictReader(io.StringIO(text)))
        by = {r["key"]: r for r in rows}
        self.assertEqual(by["192.0.2.10"]["name"], "redos-server")          # агент важнее сканера
        self.assertEqual(by["192.0.2.10"]["os"], "RED OS 8.0")
        self.assertEqual(by["192.0.2.10"]["mac"], "aa:bb:cc:00:00:01")      # MAC подтянут из сканера
        self.assertEqual(by["192.0.2.1"]["name"], "router-1")
        self.assertEqual(by["192.0.2.1"]["origin"], "scan")
        self.assertNotIn("192.0.2.99", by)                                  # без имени — не нужен справочнику
        self.assertEqual(by["192.0.2.45"]["name"], "ws-1")                 # адрес интерфейса, а не адрес подключения
        self.assertEqual(by["192.0.2.1"]["name"], "router-1")              # адрес роутера остаётся за роутером
        self.assertNotIn("10.88.0.1", by)                                   # интерфейсы контейнеров не попадают
        code, text = self.get("/api/export/inventory.csv?token=s3cr3t&key=name&header=0")
        keys = [ln.split(",")[0] for ln in text.splitlines()]
        self.assertIn("redos-server.corp.example", keys)
        self.assertIn("redos-server", keys)
        self.assertNotIn("key", keys)
        self.assertEqual(len(keys), len({k.lower() for k in keys}))         # ключи без повторов — иначе Graylog не загрузит таблицу
        self.assertEqual(keys.count("Android"), 1)
        self.assertEqual(self.get("/api/export/inventory.csv?token=s3cr3t&key=mac")[0], 400)

    def test_export_off_without_token(self):
        tok, self.cfg.export_token = self.cfg.export_token, ""
        try:
            self.assertEqual(self.get("/api/export/inventory.csv?token=")[0], 404)
        finally:
            self.cfg.export_token = tok

    def test_levels(self):
        self.assertEqual(gelf.level_for("token_create"), 4)
        self.assertEqual(gelf.level_for("upload"), 5)
        self.assertEqual(gelf.level_for("report"), 6)


class SenderOff(unittest.TestCase):
    def test_disabled_without_host(self):
        self.assertIsNone(gelf.Sender.from_config(config.Config(os.devnull)))


if __name__ == "__main__":
    unittest.main()
