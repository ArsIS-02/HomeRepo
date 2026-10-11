"""SQLite: схема и общие помощники. БД общая для панели, службы подписи и CLI."""

import json
import os
import sqlite3
import threading
import time

SCHEMA = [
    # v1
    """
    CREATE TABLE users(
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        pw_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'viewer',
        must_change INTEGER NOT NULL DEFAULT 1,
        disabled INTEGER NOT NULL DEFAULT 0,
        created REAL NOT NULL,
        last_login REAL
    );
    CREATE TABLE sessions(
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        csrf TEXT NOT NULL,
        created REAL NOT NULL,
        last_seen REAL NOT NULL,
        ip TEXT, ua TEXT
    );
    CREATE TABLE login_fail(
        key TEXT PRIMARY KEY,
        n INTEGER NOT NULL,
        first REAL NOT NULL,
        until REAL NOT NULL DEFAULT 0
    );
    CREATE TABLE audit(
        id INTEGER PRIMARY KEY,
        ts REAL NOT NULL,
        user TEXT, ip TEXT,
        action TEXT NOT NULL,
        detail TEXT
    );
    CREATE INDEX audit_ts ON audit(ts);
    CREATE TABLE settings(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE jobs(
        id INTEGER PRIMARY KEY,
        op TEXT NOT NULL,
        worker TEXT NOT NULL DEFAULT 'signer',
        params TEXT NOT NULL DEFAULT '{}',
        title TEXT,
        status TEXT NOT NULL DEFAULT 'queued',
        step TEXT,
        progress REAL,
        message TEXT,
        created_by TEXT,
        created REAL NOT NULL,
        started REAL,
        finished REAL
    );
    CREATE INDEX jobs_status ON jobs(status, id);
    CREATE TABLE packages(
        id INTEGER PRIMARY KEY,
        branch TEXT NOT NULL,
        filename TEXT NOT NULL,
        name TEXT NOT NULL,
        epoch INTEGER NOT NULL DEFAULT 0,
        version TEXT NOT NULL,
        release TEXT NOT NULL,
        arch TEXT NOT NULL,
        arches TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime REAL NOT NULL,
        buildtime INTEGER,
        summary TEXT,
        sigkey TEXT,
        sigclass TEXT NOT NULL DEFAULT 'none',
        added REAL NOT NULL,
        UNIQUE(branch, filename)
    );
    CREATE INDEX packages_name ON packages(name);
    CREATE TABLE junk(
        path TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        size INTEGER,
        reason TEXT
    );
    CREATE TABLE uploads(
        id TEXT PRIMARY KEY,
        filename TEXT NOT NULL,
        size INTEGER NOT NULL,
        received INTEGER NOT NULL DEFAULT 0,
        user TEXT,
        created REAL NOT NULL
    );
    CREATE TABLE hosts(
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        fqdn TEXT,
        machine_id TEXT UNIQUE,
        ip TEXT,
        os_name TEXT, os_version TEXT, arch TEXT, kernel TEXT,
        agent_version TEXT,
        secret_hash TEXT NOT NULL,
        enrolled REAL NOT NULL,
        last_poll REAL,
        last_report REAL,
        want_report INTEGER NOT NULL DEFAULT 1,
        pkg_total INTEGER,
        facts TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE host_packages(
        host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        epoch INTEGER NOT NULL DEFAULT 0,
        version TEXT NOT NULL,
        release TEXT NOT NULL,
        arch TEXT NOT NULL,
        from_repo TEXT,
        sigkey TEXT,
        cls TEXT NOT NULL,
        installtime INTEGER
    );
    CREATE INDEX host_packages_host ON host_packages(host_id);
    CREATE INDEX host_packages_name ON host_packages(name);
    CREATE TABLE tokens(
        id INTEGER PRIMARY KEY,
        hash TEXT UNIQUE NOT NULL,
        note TEXT,
        created_by TEXT,
        created REAL NOT NULL,
        expires REAL NOT NULL,
        multi INTEGER NOT NULL DEFAULT 0,
        uses INTEGER NOT NULL DEFAULT 0,
        used REAL,
        host_id INTEGER
    );
    CREATE TABLE tasks(
        id INTEGER PRIMARY KEY,
        batch INTEGER,
        host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
        action TEXT NOT NULL,
        name TEXT NOT NULL,
        evr TEXT,
        arch TEXT,
        params TEXT NOT NULL DEFAULT '{}',
        schedule TEXT NOT NULL DEFAULT 'now',
        not_before REAL,
        status TEXT NOT NULL DEFAULT 'pending',
        tries INTEGER NOT NULL DEFAULT 0,
        output TEXT,
        created_by TEXT,
        created REAL NOT NULL,
        sent REAL,
        finished REAL
    );
    CREATE INDEX tasks_host ON tasks(host_id, status);
    """,
    # v2: метрики хостов, сканер сети, журнал выполнения заданий
    """
    ALTER TABLE hosts ADD COLUMN metrics TEXT;
    ALTER TABLE hosts ADD COLUMN metrics_ts REAL;
    ALTER TABLE hosts ADD COLUMN hw TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE jobs ADD COLUMN log TEXT;
    CREATE TABLE host_metrics(
        host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
        ts REAL NOT NULL,
        cpu REAL, mem REAL, swap REAL, load1 REAL, disk REAL, temp REAL
    );
    CREATE INDEX host_metrics_host ON host_metrics(host_id, ts);
    CREATE TABLE discovered(
        ip TEXT PRIMARY KEY,
        mac TEXT, vendor TEXT, hostname TEXT,
        ports TEXT NOT NULL DEFAULT '[]',
        banner TEXT, os_guess TEXT,
        first_seen REAL NOT NULL, last_seen REAL NOT NULL,
        alive INTEGER NOT NULL DEFAULT 1,
        note TEXT
    );
    """,
    # v3: тип системы найденных устройств (автоопределение и ручное указание)
    """
    ALTER TABLE discovered ADD COLUMN os_family TEXT;
    ALTER TABLE discovered ADD COLUMN manual_family TEXT;
    ALTER TABLE discovered ADD COLUMN manual_name TEXT;
    """,
    # v4: просмотренные (скрытые) ошибки
    """
    ALTER TABLE jobs ADD COLUMN ack INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE tasks ADD COLUMN ack INTEGER NOT NULL DEFAULT 0;
    """,
    # v5: плейбуки Ansible, запуски и их события; доступ Ansible и обновления ОС на хостах
    """
    CREATE TABLE playbooks(
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT,
        model TEXT,
        yaml TEXT NOT NULL DEFAULT '',
        raw INTEGER NOT NULL DEFAULT 0,
        source TEXT,
        created_by TEXT, created REAL NOT NULL,
        updated_by TEXT, updated REAL NOT NULL
    );
    CREATE TABLE runs(
        id INTEGER PRIMARY KEY,
        playbook_id INTEGER,
        name TEXT,
        yaml TEXT NOT NULL,
        targets TEXT NOT NULL DEFAULT '[]',
        mode TEXT NOT NULL DEFAULT 'run',
        opts TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'queued',
        created_by TEXT, created REAL NOT NULL,
        started REAL, finished REAL,
        rc INTEGER, message TEXT, recap TEXT,
        ack INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX runs_status ON runs(status, id);
    CREATE TABLE run_events(
        id INTEGER PRIMARY KEY,
        run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        ts REAL, kind TEXT, host TEXT, task TEXT, task_name TEXT, status TEXT, data TEXT
    );
    CREATE INDEX run_events_run ON run_events(run_id, id);
    CREATE TABLE run_log(
        id INTEGER PRIMARY KEY,
        run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        text TEXT NOT NULL
    );
    CREATE INDEX run_log_run ON run_log(run_id, id);
    ALTER TABLE hosts ADD COLUMN ansible TEXT;
    ALTER TABLE hosts ADD COLUMN os_updates TEXT;
    """,
]

_local = threading.local()


def connect(path):
    con = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init(path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    con = connect(path)
    fix_perms(path)
    ver = con.execute("PRAGMA user_version").fetchone()[0]
    for i in range(ver, len(SCHEMA)):
        con.execute("BEGIN IMMEDIATE")
        try:
            for stmt in SCHEMA[i].split(";"):
                if stmt.strip():
                    con.execute(stmt)
            con.execute("PRAGMA user_version=%d" % (i + 1))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    return con


def fix_perms(path):
    """БД общая для пользователей homerepo и homerepo-signer (группа homerepo): права 0660."""
    for f in (path, path + "-wal", path + "-shm"):
        try:
            st = os.stat(f)
            if st.st_uid == os.geteuid() and (st.st_mode & 0o777) != 0o660:
                os.chmod(f, 0o660)
        except OSError:
            pass


def thread_con(path):
    """Отдельное соединение на поток (панель многопоточная)."""
    con = getattr(_local, "con", None)
    if con is None or getattr(_local, "path", None) != path:
        con = connect(path)
        _local.con = con
        _local.path = path
    return con


class tx:
    """with tx(con): ... — транзакция BEGIN IMMEDIATE."""

    def __init__(self, con):
        self.con = con

    def __enter__(self):
        self.con.execute("BEGIN IMMEDIATE")
        return self.con

    def __exit__(self, et, ev, tb):
        if et is None:
            self.con.execute("COMMIT")
        else:
            self.con.execute("ROLLBACK")
        return False


def get_setting(con, k, default=None):
    r = con.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    if not r:
        return default
    try:
        return json.loads(r[0])
    except ValueError:
        return default


def set_setting(con, k, v):
    con.execute(
        "INSERT INTO settings(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (k, json.dumps(v, ensure_ascii=False)),
    )


def audit(con, user, ip, action, detail=""):
    con.execute(
        "INSERT INTO audit(ts, user, ip, action, detail) VALUES(?,?,?,?,?)",
        (time.time(), user, ip, action, detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)),
    )


def add_job(con, op, params, title, user, worker="signer"):
    cur = con.execute(
        "INSERT INTO jobs(op, worker, params, title, created_by, created) VALUES(?,?,?,?,?,?)",
        (op, worker, json.dumps(params, ensure_ascii=False), title, user, time.time()),
    )
    return cur.lastrowid


def rows(cur):
    return [dict(r) for r in cur]
