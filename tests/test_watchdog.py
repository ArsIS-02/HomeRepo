"""Сторож связи с агентами: молчание дольше суток → hr_event=agent_silent, возвращение → agent_recovered.

Запуск: PYTHONPATH=lib python3 -m unittest tests/test_watchdog.py
"""
import json
import os
import socket
import tempfile
import time
import unittest
import zlib

from homerepo import config, db, gelf, watchdog


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


class Watchdog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="hr-wd-")
        cls.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        cls.sock.bind(("127.0.0.1", 0))
        conf = os.path.join(cls.tmp, "homerepo.conf")
        with open(conf, "w") as f:
            f.write("[paths]\nstate_dir = %s/state\n[graylog]\ngelf_host = 127.0.0.1\ngelf_port = %d\n"
                    % (cls.tmp, cls.sock.getsockname()[1]))
        cls.cfg = config.Config(conf)
        gelf.reset(gelf.Sender.from_config(cls.cfg))
        cls.con = db.init(cls.cfg.db_path)

    @classmethod
    def tearDownClass(cls):
        gelf.reset()

    def setUp(self):
        self.con.execute("DELETE FROM hosts")
        self.con.execute("DELETE FROM settings WHERE k=?", (watchdog._KEY,))
        recv_all(self.sock, 0.2)                     # вычистить датаграммы прошлых тестов

    def add_host(self, name, ip, silent_for):
        now = time.time()
        self.con.execute("INSERT INTO hosts(name, ip, secret_hash, enrolled, last_poll) VALUES(?,?,?,?,?)",
                         (name, ip, "x", now - silent_for - 10, now - silent_for))

    def events(self):
        gelf.sender().flush()
        return [m for m in recv_all(self.sock) if m.get("_hr_event") in ("agent_silent", "agent_recovered")]

    def test_silent_host_alerted(self):
        self.add_host("ws-silent", "192.0.2.51", 90000)      # молчит 25 часов
        self.add_host("ws-ok", "192.0.2.52", 300)            # на связи
        sent, back = watchdog.check(self.con)
        self.assertEqual((sent, back), (1, 0))
        msgs = self.events()
        self.assertEqual(len(msgs), 1)
        m = msgs[0]
        self.assertEqual(m["_hr_event"], "agent_silent")
        self.assertEqual((m["_hr_agent_name"], m["_hr_ip"]), ("ws-silent", "192.0.2.51"))
        self.assertEqual(m["level"], 4)
        self.assertEqual(m["_hr_hours_silent"], 25)
        self.assertTrue(m["short_message"].startswith("HomeRepo: агент ws-silent молчит"))

    def test_no_duplicate_until_remind(self):
        self.add_host("ws-silent", "192.0.2.51", 90000)
        self.assertEqual(watchdog.check(self.con)[0], 1)
        self.events()
        self.assertEqual(watchdog.check(self.con), (0, 0))   # повтор сразу — без дубликата
        self.assertEqual(self.events(), [])

    def test_recovered_host_reported_once(self):
        self.add_host("ws-silent", "192.0.2.51", 90000)
        watchdog.check(self.con)
        self.events()
        self.con.execute("UPDATE hosts SET last_poll=?", (time.time(),))   # агент вернулся
        sent, back = watchdog.check(self.con)
        self.assertEqual((sent, back), (0, 1))
        msgs = self.events()
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["_hr_event"], "agent_recovered")
        self.assertEqual(msgs[0]["_hr_agent_name"], "ws-silent")
        self.assertEqual(msgs[0]["level"], 6)
        self.assertEqual(watchdog.check(self.con), (0, 0))   # возвращение сообщается один раз

    def test_deleted_host_forgotten(self):
        self.add_host("ws-silent", "192.0.2.51", 90000)
        watchdog.check(self.con)
        self.events()
        self.con.execute("DELETE FROM hosts")
        watchdog.check(self.con)                             # хост удалён — тревога забывается
        self.add_host("ws-new", "192.0.2.55", 90000)         # новый id, тишина засчитывается заново
        self.assertEqual(watchdog.check(self.con)[0], 1)

    def test_heartbeat_every_check(self):
        self.add_host("ws-silent", "192.0.2.51", 90000)
        self.add_host("ws-ok", "192.0.2.52", 300)
        watchdog.check(self.con)
        gelf.sender().flush()
        beats = [m for m in recv_all(self.sock) if m.get("_hr_event") == "heartbeat"]
        self.assertEqual(len(beats), 1)                      # ровно один пульс за проход
        self.assertEqual(beats[0]["_hr_hosts_total"], 2)
        self.assertEqual(beats[0]["_hr_hosts_silent"], 1)
        self.assertEqual(beats[0]["level"], 6)
        watchdog.check(self.con)                             # пульс идёт и при пустом проходе
        beats = [m for m in recv_all(self.sock) if m.get("_hr_event") == "heartbeat"]
        self.assertEqual(len(beats), 1)


if __name__ == "__main__":
    unittest.main()
