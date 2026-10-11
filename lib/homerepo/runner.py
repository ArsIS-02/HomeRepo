"""Служба-исполнитель homerepo-runner: запускает плейбуки Ansible из очереди.

Как и служба подписи, это отдельный процесс под своим пользователем. Только у него есть SSH-ключ,
которым Ansible входит на хосты; панель лишь ставит запуск в таблицу runs и показывает события.

Ход выполнения: плейбук и инвентарь пишутся во временный каталог запуска, ansible-playbook
выполняется с плагином homerepo_events; события (JSON Lines) и вывод построчно переносятся в БД,
откуда панель показывает их в реальном времени. Каталог запуска удаляется по завершении.
"""

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

from . import VERSION, config, db

log = logging.getLogger("homerepo.runner")

PLUGIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ansible_plugins", "callback")
SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def group_name(s):
    g = SAFE.sub("_", (s or "").strip().lower())
    g = re.sub(r"_+", "_", g).strip("_")
    return g if g and not g[0].isdigit() else "g_" + g


def ansible_version(cmd="ansible-playbook"):
    try:
        out = subprocess.run([cmd, "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             timeout=60).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"\[core ([^\]]+)\]", out) or re.search(r"ansible-playbook ([0-9][^\s]*)", out)
    return m.group(1) if m else None


def inventory_text(targets, playbook_yaml=""):
    """INI-инвентарь из выбранных хостов.

    Группы строятся по ОС и архитектуре. Кроме того, все выбранные хосты попадают в группы,
    которые упомянуты в hosts: плейбука (например, clients из старого инвентаря), — так
    импортированный плейбук выполняется ровно на тех хостах, что выбраны в панели.
    """
    lines = ["[all]"]
    groups = {}
    names = set()
    for t in targets:
        name = SAFE.sub("_", t["name"]) or "host"
        names.add(name)
        hv = []
        if t.get("connection") == "local":
            hv.append("ansible_connection=local")
            hv.append("ansible_python_interpreter=auto_silent")
        else:
            if t.get("host") and t["host"] != name:
                hv.append("ansible_host=%s" % t["host"])
            if t.get("user"):
                hv.append("ansible_user=%s" % t["user"])
        lines.append(" ".join([name] + hv))
        for g in t.get("groups") or []:
            groups.setdefault(group_name(g), []).append(name)
    referenced = set()
    for m in re.finditer(r"^\s*-?\s*hosts:\s*['\"]?([^'\"#\n]+)", playbook_yaml or "", re.M):
        for part in re.split(r"[:,]", m.group(1)):
            p = part.strip().lstrip("!&").strip()
            if p and p not in ("all", "localhost") and p not in names and "{{" not in p and "*" not in p:
                referenced.add(p)
    for g in referenced:
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", g):
            groups.setdefault(g, []).extend(sorted(names))
    for g, members in sorted(groups.items()):
        lines.append("")
        lines.append("[%s]" % g)
        lines.extend(sorted(set(members)))
    return "\n".join(lines) + "\n"


class Runner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.con = db.init(cfg.db_path)
        self.stop = False
        self.ansible = None
        self.pubkey = ""
        self.state = "idle"

    @property
    def key_path(self):
        return os.path.join(self.cfg.runner_home, ".ssh", "id_ed25519")

    def ensure_key(self):
        """SSH-ключ исполнителя: создаётся при первом запуске, открытая часть публикуется в настройках."""
        kp = self.key_path
        if not os.path.exists(kp):
            os.makedirs(os.path.dirname(kp), mode=0o700, exist_ok=True)
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "homerepo-runner", "-f", kp],
                           check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            with open(kp + ".pub", encoding="utf-8") as f:
                self.pubkey = f.read().strip()
        except OSError:
            self.pubkey = ""

    def heartbeat(self, state=None):
        if state:
            self.state = state
        db.set_setting(self.con, "runner", {"ts": time.time(), "state": self.state, "version": VERSION,
                                            "pid": os.getpid(), "ansible": self.ansible, "pubkey": self.pubkey,
                                            "remote_user": self.cfg.ansible_user})

    # ------------------------------------------------------------ очередь
    def claim(self):
        r = self.con.execute("SELECT * FROM runs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        if not r:
            return None
        cur = self.con.execute("UPDATE runs SET status='running', started=? WHERE id=? AND status='queued'",
                               (time.time(), r["id"]))
        return dict(r) if cur.rowcount else None

    def add_log(self, run_id, text):
        if text:
            self.con.execute("INSERT INTO run_log(run_id, text) VALUES(?,?)", (run_id, text))

    def add_events(self, run_id, evs):
        if not evs:
            return
        rows = []
        for e in evs:
            data = {k: v for k, v in e.items() if k not in ("kind", "ts", "host", "task", "name", "status")}
            rows.append((run_id, e.get("ts"), e.get("kind"), e.get("host"), e.get("task"), e.get("name"),
                         e.get("status"), json.dumps(data, ensure_ascii=False)))
        with db.tx(self.con):
            self.con.executemany("INSERT INTO run_events(run_id, ts, kind, host, task, task_name, status, data) "
                                 "VALUES(?,?,?,?,?,?,?,?)", rows)

    def finish(self, run_id, status, rc, message, recap=None):
        self.con.execute("UPDATE runs SET status=?, rc=?, message=?, recap=?, finished=? WHERE id=?",
                         (status, rc, message, json.dumps(recap, ensure_ascii=False) if recap is not None else None,
                          time.time(), run_id))

    # ------------------------------------------------------------ выполнение
    def execute(self, run):
        rid = run["id"]
        self.heartbeat("run #%d" % rid)
        if not self.ansible:
            self.ansible = ansible_version(self.cfg.playbook_cmd)
        if not self.ansible:
            self.add_log(rid, "Ansible не найден на сервере HomeRepo (%s). Установите: dnf install ansible-core\n"
                         % self.cfg.playbook_cmd)
            return self.finish(rid, "failed", None, "Ansible не установлен на сервере")
        opts = json.loads(run["opts"] or "{}")
        targets = json.loads(run["targets"] or "[]")
        if not targets:
            self.add_log(rid, "Не выбрано ни одного хоста.\n")
            return self.finish(rid, "failed", None, "не выбрано ни одного хоста")
        wd = os.path.join(self.cfg.runner_home, "runs", str(rid))
        shutil.rmtree(wd, ignore_errors=True)
        os.makedirs(os.path.join(wd, "tmp"), mode=0o700)
        os.chmod(wd, 0o700)
        events = os.path.join(wd, "events.jsonl")
        try:
            with open(os.path.join(wd, "playbook.yml"), "w", encoding="utf-8") as f:
                f.write(run["yaml"])
            with open(os.path.join(wd, "inventory.ini"), "w", encoding="utf-8") as f:
                f.write(inventory_text(targets, run["yaml"]))
            cmd = [self.cfg.playbook_cmd, "-i", "inventory.ini", "playbook.yml", "--forks",
                   str(max(1, min(int(opts.get("forks") or self.cfg.ansible_forks), 50)))]
            mode = run["mode"]
            if mode == "check":
                cmd += ["--check", "--diff"]
            elif mode == "syntax":
                cmd += ["--syntax-check"]
            elif opts.get("diff"):
                cmd += ["--diff"]
            if opts.get("vars"):
                with open(os.path.join(wd, "vars.json"), "w", encoding="utf-8") as f:
                    json.dump(opts["vars"], f, ensure_ascii=False)
                cmd += ["-e", "@vars.json"]
            sec = os.path.join(self.cfg.secrets_dir, "%d.json" % rid)
            if os.path.exists(sec):
                dst = os.path.join(wd, "secret.json")
                with open(sec, encoding="utf-8") as f, open(os.open(dst, os.O_WRONLY | os.O_CREAT, 0o600), "w") as g:
                    g.write(f.read())
                try:
                    os.unlink(sec)
                except OSError:
                    pass
                cmd += ["-e", "@secret.json"]
            ssh_args = ("-o ControlMaster=auto -o ControlPersist=60s -o StrictHostKeyChecking=accept-new "
                        "-o UserKnownHostsFile=%s -o ConnectTimeout=15"
                        % os.path.join(self.cfg.runner_home, ".ssh", "known_hosts"))
            env = {
                "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                "HOME": self.cfg.runner_home,
                "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                "ANSIBLE_CALLBACK_PLUGINS": PLUGIN_DIR,
                "ANSIBLE_CALLBACKS_ENABLED": "homerepo_events",
                "ANSIBLE_CALLBACK_WHITELIST": "homerepo_events",
                "HR_EVENTS": events,
                "ANSIBLE_NOCOLOR": "1", "ANSIBLE_FORCE_COLOR": "0",
                "ANSIBLE_RETRY_FILES_ENABLED": "0",
                "ANSIBLE_DEPRECATION_WARNINGS": "0",
                "ANSIBLE_LOCAL_TEMP": os.path.join(wd, "tmp"),
                "ANSIBLE_REMOTE_USER": self.cfg.ansible_user,
                "ANSIBLE_PRIVATE_KEY_FILE": self.key_path,
                "ANSIBLE_SSH_ARGS": ssh_args,
                "ANSIBLE_TIMEOUT": "20",
                "ANSIBLE_PYTHON_INTERPRETER": "auto_silent",
                "ANSIBLE_CONFIG": os.path.join(wd, "ansible.cfg"),
                "PYTHONUNBUFFERED": "1",
            }
            with open(env["ANSIBLE_CONFIG"], "w") as f:
                f.write("[defaults]\n")  # пустой: глобальный /etc/ansible/ansible.cfg не влияет на запуск
            self.add_log(rid, "$ %s\n" % " ".join(cmd))
            p = subprocess.Popen(cmd, cwd=wd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
            rc, cancelled, timed_out = self._watch(rid, p, events)
            recap = None
            r = self.con.execute("SELECT data FROM run_events WHERE run_id=? AND kind='stats' ORDER BY id DESC LIMIT 1",
                                 (rid,)).fetchone()
            if r:
                recap = json.loads(r["data"] or "{}").get("recap")
            if cancelled:
                return self.finish(rid, "cancelled", rc, "остановлено пользователем", recap)
            if timed_out:
                return self.finish(rid, "failed", rc, "превышено время выполнения (%d мин)" %
                                   (self.cfg.ansible_timeout // 60), recap)
            msg = summary(recap, rc, mode)
            if rc and not recap:
                err = self.con.execute("SELECT text FROM run_log WHERE run_id=? AND text LIKE '%ERROR!%' ORDER BY id DESC "
                                       "LIMIT 1", (rid,)).fetchone()
                if err:
                    line = [ln for ln in err["text"].splitlines() if "ERROR!" in ln][-1]
                    msg = line.split("ERROR!", 1)[1].strip()[:300] or msg
            self.finish(rid, "done" if rc == 0 else "failed", rc, msg, recap)
        except Exception as e:  # noqa: BLE001
            log.exception("запуск #%d", rid)
            self.add_log(rid, "\nошибка исполнителя: %s\n" % e)
            self.finish(rid, "failed", None, "ошибка исполнителя: %s" % e)
        finally:
            shutil.rmtree(wd, ignore_errors=True)
            self.heartbeat("idle")

    def _watch(self, rid, p, events):
        lines = []
        lock = threading.Lock()

        def reader():
            for raw in iter(p.stdout.readline, b""):
                with lock:
                    lines.append(raw.decode("utf-8", "replace"))
            p.stdout.close()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        pos = 0
        buf = ""
        cancelled = timed_out = False
        t0 = time.time()
        last_hb = 0
        killed_at = None
        while True:
            done = p.poll() is not None
            with lock:
                chunk, lines[:] = "".join(lines), []
            self.add_log(rid, chunk)
            try:
                with open(events, encoding="utf-8") as f:
                    f.seek(pos)
                    data = f.read()
                    pos = f.tell()
            except OSError:
                data = ""
            if data:
                buf += data
                *full, buf = buf.split("\n")
                evs = []
                for ln in full:
                    try:
                        evs.append(json.loads(ln))
                    except ValueError:
                        pass
                self.add_events(rid, evs)
            if done and not t.is_alive():
                break
            if not cancelled:
                st = self.con.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()
                if st and st["status"] == "cancel":
                    cancelled = True
                    self._kill(p, signal.SIGTERM)
                    killed_at = time.time()
                    self.add_log(rid, "\n=== остановка по запросу ===\n")
            if not timed_out and time.time() - t0 > self.cfg.ansible_timeout:
                timed_out = True
                self._kill(p, signal.SIGTERM)
                killed_at = time.time()
            if killed_at and time.time() - killed_at > 10 and p.poll() is None:
                self._kill(p, signal.SIGKILL)
            if time.time() - last_hb > 5:
                self.heartbeat()
                last_hb = time.time()
            if self.stop and p.poll() is None and not cancelled:
                cancelled = True
                self._kill(p, signal.SIGTERM)
                killed_at = time.time()
            time.sleep(0.4)
        return p.wait(), cancelled, timed_out

    @staticmethod
    def _kill(p, sig):
        try:
            os.killpg(p.pid, sig)
        except OSError:
            pass

    # ------------------------------------------------------------ цикл
    def cleanup_old(self):
        """Храним события и вывод последних 300 запусков."""
        r = self.con.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1 OFFSET 300").fetchone()
        if r:
            self.con.execute("DELETE FROM run_events WHERE run_id<=?", (r["id"],))
            self.con.execute("DELETE FROM run_log WHERE run_id<=?", (r["id"],))

    def run(self):
        signal.signal(signal.SIGTERM, self._sigterm)
        os.umask(0o007)
        os.makedirs(os.path.join(self.cfg.runner_home, "runs"), mode=0o700, exist_ok=True)
        self.ensure_key()
        self.ansible = ansible_version(self.cfg.playbook_cmd)
        # запуски, прерванные перезапуском службы
        self.con.execute("UPDATE runs SET status='failed', message='прервано перезапуском исполнителя', finished=? "
                         "WHERE status IN ('running','cancel')", (time.time(),))
        log.info("homerepo-runner %s запущен, Ansible %s", VERSION, self.ansible or "не найден")
        last_ver = last_clean = time.time()
        while not self.stop:
            self.heartbeat("idle")
            run = self.claim() if self.cfg.ansible_enabled else None
            if run:
                self.execute(run)
                continue
            # отменённые до начала выполнения
            self.con.execute("UPDATE runs SET status='cancelled', message='отменено до запуска', finished=? "
                             "WHERE status='cancel' AND started IS NULL", (time.time(),))
            now = time.time()
            if now - last_ver > 600:
                self.ansible = ansible_version(self.cfg.playbook_cmd)
                last_ver = now
            if now - last_clean > 3600:
                self.cleanup_old()
                last_clean = now
            time.sleep(1.0)
        log.info("остановлен")

    def _sigterm(self, *a):
        self.stop = True


def summary(recap, rc, mode):
    if mode == "syntax":
        return "синтаксис в порядке" if rc == 0 else "ошибка синтаксиса"
    if not recap:
        return "готово" if rc == 0 else "ошибка (код %s)" % rc
    n = len(recap)
    bad = sum(1 for v in recap.values() if v.get("failures") or v.get("unreachable"))
    ch = sum(1 for v in recap.values() if v.get("changed"))
    parts = ["хостов: %d" % n]
    if mode == "check":
        parts.append("изменилось бы на %d" % ch)
    else:
        parts.append("изменения на %d" % ch)
    if bad:
        parts.append("с ошибками: %d" % bad)
    return ", ".join(parts)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    cfg = config.get()
    Runner(cfg).run()


if __name__ == "__main__":
    main()
