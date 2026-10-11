"""Веб-панель HomeRepo: HTTP-сервер на стандартной библиотеке за nginx/httpd.

Панель не имеет доступа к ключу подписи и не пишет в дерево репозитория: все изменения
репозитория — задания для службы homerepo-signer.
"""

import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import socket
import ssl
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import VERSION, auth, config, db, inventory, rpmutil

log = logging.getLogger("homerepo.web")

CHUNK = 8 * 1024 * 1024
ROUTES = []


class HTTPError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def route(method, pattern, perm="viewer", csrf=True):
    rx = re.compile("^" + pattern + "$")

    def deco(fn):
        ROUTES.append((method, rx, fn, perm, csrf))
        return fn

    return deco


class Req:
    def __init__(self, h):
        self.h = h
        self.method = h.command
        u = urllib.parse.urlsplit(h.path)
        self.path = u.path
        self.query = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query, keep_blank_values=True).items()}
        self.headers = h.headers
        self.user = None
        self.session = None
        self.host = None
        self._json = None
        self.consumed = False
        peer = h.client_address[0]
        ip = peer
        if peer in ("127.0.0.1", "::1"):
            ip = (h.headers.get("X-Real-IP") or h.headers.get("X-Forwarded-For", "").split(",")[0].strip() or peer)
        self.ip = ip
        self.https = h.headers.get("X-Forwarded-Proto", "") == "https"

    @property
    def length(self):
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            raise HTTPError(411, "нужен Content-Length")
        try:
            return int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise HTTPError(400, "неверный Content-Length")

    def json(self):
        if self._json is None:
            n = self.length
            if n > 4 * 1024 * 1024:
                raise HTTPError(413, "слишком большой запрос")
            raw = self.h.rfile.read(n) if n else b"{}"
            self.consumed = True
            try:
                self._json = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                raise HTTPError(400, "неверный JSON")
            if not isinstance(self._json, dict):
                raise HTTPError(400, "ожидался объект JSON")
        return self._json

    def stream_to(self, f, limit):
        n = self.length
        if n > limit:
            raise HTTPError(413, "слишком большой фрагмент")
        left = n
        self.consumed = True
        while left > 0:
            b = self.h.rfile.read(min(1024 * 1024, left))
            if not b:
                raise HTTPError(400, "соединение оборвалось")
            f.write(b)
            left -= len(b)
        return n

    @property
    def uname(self):
        if self.user:
            return self.user["name"]
        if self.host:
            return "agent:%s" % self.host["name"]
        return None


class Resp:
    def __init__(self, status=200, body=b"", ctype="application/json; charset=utf-8", headers=None):
        self.status = status
        self.body = body
        self.ctype = ctype
        self.headers = headers or []


def jresp(obj, status=200, headers=None):
    return Resp(status, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"), headers=headers)


# ====================================================================== приложение
class App:
    def __init__(self, cfg):
        self.cfg = cfg
        db.init(cfg.db_path).close()
        os.makedirs(cfg.incoming_dir, exist_ok=True)
        self.lock = threading.Lock()

    def con(self):
        return db.thread_con(self.cfg.db_path)


APP = None


def C():
    return APP.con()


class Handler(BaseHTTPRequestHandler):
    server_version = "HomeRepo/" + VERSION
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def do_PUT(self):
        self.dispatch()

    def do_DELETE(self):
        self.dispatch()

    def dispatch(self):
        req = Req(self)
        try:
            resp = self.route(req)
        except HTTPError as e:
            resp = jresp({"error": e.message}, e.status)
        except BrokenPipeError:
            return
        except Exception as e:  # noqa
            log.error("ошибка %s %s: %s\n%s", req.method, req.path, e, traceback.format_exc())
            resp = jresp({"error": "внутренняя ошибка сервера: %s" % e}, 500)
        self.send(resp, req)

    def send(self, resp, req):
        try:
            body = resp.body if isinstance(resp.body, bytes) else resp.body.encode("utf-8")
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "same-origin")
            if resp.ctype.startswith("text/html"):
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline' "
                                 "https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
                                 "frame-ancestors 'none'")
            for k, v in resp.headers:
                self.send_header(k, v)
            if not req.consumed and self.headers.get("Content-Length", "0") not in ("", "0"):
                # тело запроса не прочитано — соединение дальше использовать нельзя
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if req.method != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def route(self, req):
        path = req.path
        if not path.startswith("/api/") and not path.startswith("/agent/"):
            return static(req)
        allowed = False
        for method, rx, fn, perm, csrf in ROUTES:
            m = rx.match(path)
            if not m:
                continue
            allowed = True
            if method != req.method:
                continue
            if perm == "agent":
                agent_auth(req)
            elif perm is not None:
                user_auth(req, perm, csrf and req.method != "GET")
            return fn(req, *m.groups())
        if allowed:
            raise HTTPError(405, "метод не поддерживается")
        raise HTTPError(404, "не найдено")


# ====================================================================== аутентификация
def user_auth(req, perm, need_csrf):
    con = C()
    tok = None
    for part in (req.headers.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "hr_s":
            tok = v
    if not tok:
        raise HTTPError(401, "требуется вход")
    sid = auth.sha256(tok)
    s = con.execute("SELECT s.*, u.name, u.role, u.must_change, u.disabled, u.id AS uid FROM sessions s "
                    "JOIN users u ON u.id=s.user_id WHERE s.id=?", (sid,)).fetchone()
    now = time.time()
    if not s or s["disabled"] or now - s["last_seen"] > APP.cfg.session_hours * 3600:
        if s:
            con.execute("DELETE FROM sessions WHERE id=?", (sid,))
        raise HTTPError(401, "сессия истекла, войдите снова")
    if now - s["last_seen"] > 60:
        con.execute("UPDATE sessions SET last_seen=? WHERE id=?", (now, sid))
    req.session = dict(s)
    req.user = {"id": s["uid"], "name": s["name"], "role": s["role"], "must_change": bool(s["must_change"])}
    if need_csrf and not secrets.compare_digest(req.headers.get("X-CSRF", ""), s["csrf"]):
        raise HTTPError(403, "неверный CSRF-токен, обновите страницу")
    if req.user["must_change"] and req.path not in ("/api/me", "/api/password", "/api/logout"):
        raise HTTPError(403, "сначала смените пароль")
    if auth.ROLE_LEVEL.get(req.user["role"], -1) < auth.ROLE_LEVEL[perm]:
        raise HTTPError(403, "недостаточно прав (нужна роль %s)" % perm)


def agent_auth(req):
    h = req.headers.get("Authorization", "")
    if not h.startswith("Bearer "):
        raise HTTPError(401, "нет токена агента")
    hid, _, secret = h[7:].partition(":")
    if not hid.isdigit() or not secret:
        raise HTTPError(401, "неверный токен агента")
    host = C().execute("SELECT * FROM hosts WHERE id=?", (int(hid),)).fetchone()
    if not host or not secrets.compare_digest(host["secret_hash"], auth.sha256(secret)):
        raise HTTPError(401, "агент не зарегистрирован — переподключите хост")
    req.host = dict(host)


def audit(req, action, detail=""):
    db.audit(C(), req.uname, req.ip, action, detail)


LOCK_N, LOCK_WINDOW, LOCK_TIME = 5, 900, 900


def _fail_check(con, keys):
    now = time.time()
    for k in keys:
        r = con.execute("SELECT * FROM login_fail WHERE key=?", (k,)).fetchone()
        if r and r["until"] > now:
            return int(r["until"] - now)
    return 0


def _fail_add(con, keys):
    now = time.time()
    for k in keys:
        r = con.execute("SELECT * FROM login_fail WHERE key=?", (k,)).fetchone()
        if not r or now - r["first"] > LOCK_WINDOW:
            con.execute("INSERT OR REPLACE INTO login_fail(key, n, first, until) VALUES(?,?,?,0)", (k, 1, now))
        else:
            n = r["n"] + 1
            until = now + LOCK_TIME if n >= LOCK_N else 0
            con.execute("UPDATE login_fail SET n=?, until=? WHERE key=?", (n, until, k))


@route("POST", "/api/login", perm=None)
def api_login(req):
    con = C()
    d = req.json()
    name = str(d.get("user", "")).strip()
    pw = str(d.get("password", ""))
    keys = ["u:" + name.lower(), "ip:" + req.ip]
    wait = _fail_check(con, keys)
    if wait:
        raise HTTPError(429, "слишком много неудачных попыток, вход заблокирован ещё на %d мин" % (wait // 60 + 1))
    u = con.execute("SELECT * FROM users WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    if not u or u["disabled"] or not auth.verify_password(pw, u["pw_hash"]):
        _fail_add(con, keys)
        db.audit(con, name or "?", req.ip, "login_failed", "")
        time.sleep(0.5)
        raise HTTPError(401, "неверное имя пользователя или пароль")
    con.execute("DELETE FROM login_fail WHERE key IN (?,?)", keys)
    tok = auth.new_token()
    now = time.time()
    con.execute("INSERT INTO sessions(id, user_id, csrf, created, last_seen, ip, ua) VALUES(?,?,?,?,?,?,?)",
                (auth.sha256(tok), u["id"], auth.new_token(18), now, now, req.ip,
                 (req.headers.get("User-Agent") or "")[:200]))
    con.execute("UPDATE users SET last_login=? WHERE id=?", (now, u["id"]))
    con.execute("DELETE FROM sessions WHERE last_seen<?", (now - 7 * 86400,))
    db.audit(con, u["name"], req.ip, "login", "")
    cookie = "hr_s=%s; Path=/; HttpOnly; SameSite=Strict%s" % (tok, "; Secure" if req.https else "")
    return jresp({"ok": True, "must_change": bool(u["must_change"])}, headers=[("Set-Cookie", cookie)])


@route("POST", "/api/logout")
def api_logout(req):
    C().execute("DELETE FROM sessions WHERE id=?", (req.session["id"],))
    audit(req, "logout")
    return jresp({"ok": True}, headers=[("Set-Cookie", "hr_s=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")])


@route("GET", "/api/me")
def api_me(req):
    return jresp({"user": req.user["name"], "role": req.user["role"], "must_change": req.user["must_change"],
                  "csrf": req.session["csrf"], "version": VERSION, "branches": APP.cfg.branches,
                  "arches": APP.cfg.arches, "default_branch": APP.cfg.default_branch, "chunk": CHUNK,
                  "upload_max": APP.cfg.upload_max})


@route("POST", "/api/password")
def api_password(req):
    con = C()
    d = req.json()
    u = con.execute("SELECT * FROM users WHERE id=?", (req.user["id"],)).fetchone()
    if not auth.verify_password(str(d.get("old", "")), u["pw_hash"]):
        raise HTTPError(400, "текущий пароль указан неверно")
    new = str(d.get("new", ""))
    prob = auth.password_problem(new, u["name"])
    if prob:
        raise HTTPError(400, prob)
    if auth.verify_password(new, u["pw_hash"]):
        raise HTTPError(400, "новый пароль совпадает со старым")
    con.execute("UPDATE users SET pw_hash=?, must_change=0 WHERE id=?", (auth.hash_password(new), u["id"]))
    con.execute("DELETE FROM sessions WHERE user_id=? AND id<>?", (u["id"], req.session["id"]))
    audit(req, "password_changed")
    return jresp({"ok": True})


# ====================================================================== сводка и пакеты
def signer_state(con):
    s = db.get_setting(con, "signer", {}) or {}
    s["alive"] = bool(s.get("ts")) and time.time() - s["ts"] < 30
    return s


@route("GET", "/api/summary")
def api_summary(req):
    con = C()
    cfg = APP.cfg
    pk = db.rows(con.execute("SELECT sigclass, arch, arches, size FROM packages"))
    by = {"ours": 0, "foreign": 0, "none": 0}
    per_arch = {a: 0 for a in cfg.arches}
    for p in pk:
        by[p["sigclass"]] = by.get(p["sigclass"], 0) + 1
        for a in p["arches"].split():
            per_arch[a] = per_arch.get(a, 0) + 1
    hosts = db.rows(con.execute("SELECT last_poll FROM hosts"))
    online = sum(1 for h in hosts if inventory.host_status(h, cfg.agent_interval) == "online")
    junk = con.execute("SELECT count(*), coalesce(sum(size),0) FROM junk").fetchone()
    key = db.get_setting(con, "key", {}) or {}
    return jresp({
        "packages": len(pk), "sig": by, "size": sum(p["size"] for p in pk), "per_arch": per_arch,
        "hosts": len(hosts), "online": online,
        "tasks_pending": con.execute("SELECT count(*) FROM tasks WHERE status IN ('pending','sent')").fetchone()[0],
        "queue": con.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0],
        "signer": signer_state(con), "key": {k: key.get(k) for k in ("fpr", "uid", "error", "keyids")},
        "junk": {"count": junk[0], "size": junk[1]}, "dirty": len(db.get_setting(con, "dirty_dirs", []) or []),
        "last_scan": db.get_setting(con, "last_scan"),
    })


@route("GET", "/api/packages")
def api_packages(req):
    con = C()
    rows = db.rows(con.execute("SELECT * FROM packages ORDER BY name, arch"))
    idx = inventory.repo_index(con)
    hosts = {r["id"]: inventory.host_arch(r["arch"]) for r in con.execute("SELECT id, arch FROM hosts")}
    inst = {}
    for r in con.execute("SELECT host_id, name, arch FROM host_packages WHERE name IN (SELECT name FROM packages)"):
        inst.setdefault(r["name"], set()).add((r["host_id"], r["arch"]))
    for r in rows:
        latest = None
        for p in idx.get(r["name"], []):
            if p["arch"] == r["arch"]:
                latest = p
                break
        r["latest"] = bool(latest and latest["id"] == r["id"])
        arches = r["arches"].split()
        elig = [hid for hid, ha in hosts.items() if ha in arches]
        on = {hid for hid, a in inst.get(r["name"], set()) if hid in elig and (a == r["arch"] or r["arch"] == "noarch"
                                                                               or a == "noarch")}
        r["hosts_on"] = len(on)
        r["hosts_eligible"] = len(elig)
        r["evr"] = rpmutil.fmt_evr(r["epoch"], r["version"], r["release"])
    return jresp({"packages": rows})


@route("GET", r"/api/packages/(\d+)")
def api_package(req, pid):
    con = C()
    r = con.execute("SELECT * FROM packages WHERE id=?", (int(pid),)).fetchone()
    if not r:
        raise HTTPError(404, "пакет не найден")
    r = dict(r)
    hs = db.rows(con.execute(
        "SELECT h.id, h.name, h.arch, hp.epoch, hp.version, hp.release, hp.arch AS parch, hp.cls, hp.from_repo "
        "FROM host_packages hp JOIN hosts h ON h.id=hp.host_id WHERE hp.name=? ORDER BY h.name", (r["name"],)))
    for h in hs:
        h["evr"] = rpmutil.fmt_evr(h["epoch"], h["version"], h["release"])
        h["cmp"] = rpmutil.evr_cmp(h["epoch"], h["version"], h["release"], r["epoch"], r["version"], r["release"])
    r["hosts"] = hs
    r["url"] = "%s/%s/%s/%s" % (APP.cfg.repo_url, r["arches"].split()[0], r["branch"], r["filename"])
    return jresp(r)


def _ids(d):
    ids = d.get("ids") or []
    if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids) or not ids:
        raise HTTPError(400, "не выбраны пакеты")
    return ids


def _names(con, ids):
    q = "SELECT filename FROM packages WHERE id IN (%s)" % ",".join("?" * len(ids))
    return [r[0] for r in con.execute(q, ids)]


@route("POST", "/api/packages/action", perm="operator")
def api_pkg_action(req):
    con = C()
    d = req.json()
    op = d.get("op")
    if op == "resign_foreign":
        n = con.execute("SELECT count(*) FROM packages WHERE sigclass<>'ours'").fetchone()[0]
        if not n:
            raise HTTPError(400, "все пакеты уже подписаны ключом HomeRepo")
        jid = db.add_job(con, "resign", {"all_foreign": True}, "переподписать чужие и неподписанные (%d)" % n, req.uname)
        audit(req, "resign_foreign", "%d пакетов" % n)
        return jresp({"job": jid})
    ids = _ids(d)
    names = _names(con, ids)
    if op == "resign":
        jid = db.add_job(con, "resign", {"ids": ids}, "переподписать: " + ", ".join(names[:3]) +
                         (" и ещё %d" % (len(names) - 3) if len(names) > 3 else ""), req.uname)
    elif op == "delete":
        jid = db.add_job(con, "delete", {"ids": ids}, "удалить: " + ", ".join(names[:3]) +
                         (" и ещё %d" % (len(names) - 3) if len(names) > 3 else ""), req.uname)
    elif op == "move":
        to = d.get("to")
        if to not in APP.cfg.branches:
            raise HTTPError(400, "неизвестная ветка")
        jid = db.add_job(con, "move", {"ids": ids, "to": to}, "переместить в %s: %s" % (to, ", ".join(names[:3])),
                         req.uname)
    else:
        raise HTTPError(400, "неизвестное действие")
    audit(req, "package_" + op, {"files": names, "to": d.get("to")})
    return jresp({"job": jid})


@route("POST", "/api/repo/(metadata|rescan|cleanup|key)", perm="operator")
def api_repo_op(req, op):
    con = C()
    titles = {"metadata": "пересобрать метаданные всех веток", "rescan": "пересканировать репозиторий",
              "cleanup": "удалить мусорные файлы", "key": "проверить ключ подписи"}
    jid = db.add_job(con, op, {}, titles[op], req.uname)
    audit(req, "repo_" + op)
    return jresp({"job": jid})


@route("GET", "/api/junk")
def api_junk(req):
    con = C()
    root = APP.cfg.repo_root
    return jresp({"junk": [dict(r, rel=os.path.relpath(r["path"], root)) for r in con.execute("SELECT * FROM junk")]})


@route("GET", "/api/key")
def api_key(req):
    con = C()
    key = db.get_setting(con, "key", {}) or {}
    return jresp({"key": key, "pub_url": APP.cfg.repo_url + "/RPM-GPG-KEY-HOMEREPO", "signer": signer_state(con),
                  "repo_state": db.get_setting(con, "repo_state", {}) or {},
                  "dirty": db.get_setting(con, "dirty_dirs", []) or [],
                  "sign_metadata": APP.cfg.sign_metadata})


@route("GET", "/api/jobs")
def api_jobs(req):
    con = C()
    lim = min(int(req.query.get("limit", "40") or 40), 300)
    rows = db.rows(con.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (lim,)))
    for r in rows:
        r.pop("params", None)
    today = time.time() - 86400
    st = con.execute("SELECT sum(status='done'), sum(status='error') FROM jobs WHERE finished>?", (today,)).fetchone()
    return jresp({"jobs": rows, "day_done": st[0] or 0, "day_error": st[1] or 0, "signer": signer_state(con)})


@route("POST", r"/api/jobs/(\d+)/cancel", perm="operator")
def api_job_cancel(req, jid):
    con = C()
    with db.tx(con):
        r = con.execute("SELECT * FROM jobs WHERE id=?", (int(jid),)).fetchone()
        if not r or r["status"] != "queued":
            raise HTTPError(400, "отменить можно только задание в очереди")
        con.execute("UPDATE jobs SET status='cancelled', finished=?, message='отменено' WHERE id=?",
                    (time.time(), int(jid)))
    p = json.loads(r["params"] or "{}")
    _drop_incoming(p.get("path"))
    audit(req, "job_cancel", "#%s %s" % (jid, r["title"]))
    return jresp({"ok": True})


def _drop_incoming(path):
    if path and os.path.realpath(path).startswith(os.path.realpath(APP.cfg.incoming_dir) + os.sep):
        try:
            os.unlink(path)
        except OSError:
            pass


# ====================================================================== загрузка
def _upload_opts(d):
    branch = d.get("branch") or APP.cfg.default_branch
    if branch not in APP.cfg.branches:
        raise HTTPError(400, "неизвестная ветка")
    return {"branch": branch, "sign": bool(d.get("sign", True)), "keep": max(0, min(int(d.get("keep") or 0), 20))}


@route("POST", "/api/uploads", perm="operator")
def api_upload_start(req):
    d = req.json()
    fn = os.path.basename(str(d.get("filename", "")))
    size = int(d.get("size") or 0)
    if not fn.lower().endswith(".rpm"):
        raise HTTPError(400, "можно загружать только файлы .rpm")
    if size <= 0 or size > APP.cfg.upload_max:
        raise HTTPError(400, "размер файла вне допустимого (до %d ГБ)" % (APP.cfg.upload_max >> 30))
    free = shutil.disk_usage(APP.cfg.incoming_dir).free
    if free < size * 2 + (500 << 20):
        raise HTTPError(507, "мало места на диске: свободно %d МБ" % (free >> 20))
    uid = secrets.token_hex(12)
    open(os.path.join(APP.cfg.incoming_dir, uid + ".upload"), "wb").close()
    C().execute("INSERT INTO uploads(id, filename, size, received, user, created) VALUES(?,?,?,0,?,?)",
                (uid, fn, size, req.uname, time.time()))
    return jresp({"id": uid, "chunk": CHUNK})


def _upload(req, uid):
    if not re.match(r"^[0-9a-f]{24}$", uid):
        raise HTTPError(404, "загрузка не найдена")
    u = C().execute("SELECT * FROM uploads WHERE id=?", (uid,)).fetchone()
    if not u:
        raise HTTPError(404, "загрузка не найдена или устарела")
    if u["user"] != req.uname and req.user["role"] != "admin":
        raise HTTPError(403, "чужая загрузка")
    return dict(u)


@route("GET", r"/api/uploads/([0-9a-f]+)", perm="operator")
def api_upload_get(req, uid):
    u = _upload(req, uid)
    return jresp({"id": uid, "received": u["received"], "size": u["size"]})


@route("PUT", r"/api/uploads/([0-9a-f]+)", perm="operator")
def api_upload_put(req, uid):
    u = _upload(req, uid)
    off = int(req.query.get("offset", "-1"))
    if off != u["received"]:
        return jresp({"error": "неверное смещение", "received": u["received"]}, 409)
    path = os.path.join(APP.cfg.incoming_dir, uid + ".upload")
    with open(path, "r+b") as f:
        f.seek(off)
        f.truncate()
        n = req.stream_to(f, CHUNK + 1024 * 1024)
    if off + n > u["size"]:
        raise HTTPError(400, "получено больше, чем объявлено")
    C().execute("UPDATE uploads SET received=? WHERE id=?", (off + n, uid))
    return jresp({"received": off + n})


@route("POST", r"/api/uploads/([0-9a-f]+)/finish", perm="operator")
def api_upload_finish(req, uid):
    con = C()
    u = _upload(req, uid)
    opts = _upload_opts(req.json())
    if u["received"] != u["size"]:
        raise HTTPError(400, "файл загружен не полностью (%d из %d байт)" % (u["received"], u["size"]))
    src = os.path.join(APP.cfg.incoming_dir, uid + ".upload")
    with open(src, "rb") as f:
        magic = f.read(4)
    if magic != b"\xed\xab\xee\xdb":
        os.unlink(src)
        con.execute("DELETE FROM uploads WHERE id=?", (uid,))
        raise HTTPError(400, "%s — не RPM-пакет" % u["filename"])
    dst = os.path.join(APP.cfg.incoming_dir, uid + ".rpm")
    os.replace(src, dst)
    con.execute("DELETE FROM uploads WHERE id=?", (uid,))
    params = dict(opts, path=dst, original=u["filename"])
    jid = db.add_job(con, "add", params, u["filename"], req.uname)
    audit(req, "upload", {"file": u["filename"], "size": u["size"], "branch": opts["branch"], "sign": opts["sign"]})
    return jresp({"job": jid})


@route("DELETE", r"/api/uploads/([0-9a-f]+)", perm="operator")
def api_upload_del(req, uid):
    _upload(req, uid)
    _drop_incoming(os.path.join(APP.cfg.incoming_dir, uid + ".upload"))
    C().execute("DELETE FROM uploads WHERE id=?", (uid,))
    return jresp({"ok": True})


@route("POST", "/api/fetch", perm="operator")
def api_fetch(req):
    d = req.json()
    url = str(d.get("url", "")).strip()
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise HTTPError(400, "нужна ссылка http:// или https://")
    opts = _upload_opts(d)
    name = os.path.basename(u.path) or "package.rpm"
    jid = db.add_job(C(), "fetch", dict(opts, url=url), name, req.uname, worker="web")
    audit(req, "fetch", {"url": url, "branch": opts["branch"]})
    return jresp({"job": jid})


def fetch_worker():
    """Скачивание по ссылке выполняет панель (у службы подписи нет выхода в сеть);
    готовый файл передаётся службе подписи тем же заданием."""
    con = db.connect(APP.cfg.db_path)
    con.execute("UPDATE jobs SET status='queued' WHERE status='running' AND worker='web' AND op='fetch'")
    con.execute("UPDATE jobs SET status='error', message='прервано перезапуском панели' "
                "WHERE status IN ('running','queued') AND worker='web' AND op IN ('scan','ssh_install')")
    while True:
        try:
            with db.tx(con):
                j = con.execute("SELECT * FROM jobs WHERE status='queued' AND worker='web' ORDER BY id LIMIT 1").fetchone()
                if j:
                    con.execute("UPDATE jobs SET status='running', started=?, step=CASE op WHEN 'fetch' THEN 'download' "
                                "ELSE 'run' END WHERE id=?",
                                (time.time(), j["id"]))
            if not j:
                _cleanup_uploads(con)
                time.sleep(1)
                continue
            j = dict(j)
            from . import scanner
            if j["op"] == "scan":
                threading.Thread(target=_guard, args=(scanner.do_scan, j), daemon=True).start()
            elif j["op"] == "ssh_install":
                threading.Thread(target=_guard, args=(scanner.do_install, j), daemon=True).start()
            else:
                _do_fetch(con, j)
        except Exception:  # noqa
            log.exception("fetch worker")
            time.sleep(5)


def _guard(fn, j):
    """Сканирование и установка по SSH идут параллельно в своих потоках."""
    con = db.connect(APP.cfg.db_path)
    try:
        fn(con, j)
    except Exception as e:  # noqa
        log.exception("%s #%d", j["op"], j["id"])
        con.execute("UPDATE jobs SET status='error', finished=?, message=? WHERE id=?",
                    (time.time(), "внутренняя ошибка: %s" % e, j["id"]))
    finally:
        con.close()


def _do_fetch(con, j):
    p = json.loads(j["params"])
    dst = os.path.join(APP.cfg.incoming_dir, "fetch-%d.rpm" % j["id"])
    try:
        ctx = ssl.create_default_context()
        rq = urllib.request.Request(p["url"], headers={"User-Agent": "HomeRepo/" + VERSION})
        with urllib.request.urlopen(rq, timeout=60, context=ctx) as r, open(dst, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            if total > APP.cfg.upload_max:
                raise ValueError("файл больше допустимого")
            got = 0
            last = 0
            first = True
            while True:
                b = r.read(1024 * 1024)
                if not b:
                    break
                if first:
                    if b[:4] != b"\xed\xab\xee\xdb":
                        raise ValueError("по ссылке не RPM-пакет (получено %s)" % (r.headers.get("Content-Type") or "?"))
                    first = False
                f.write(b)
                got += len(b)
                if got > APP.cfg.upload_max:
                    raise ValueError("файл больше допустимого")
                if time.time() - last > 1:
                    last = time.time()
                    st = con.execute("SELECT status FROM jobs WHERE id=?", (j["id"],)).fetchone()
                    if st and st["status"] == "cancelled":
                        raise ValueError("отменено")
                    con.execute("UPDATE jobs SET progress=?, message=? WHERE id=?",
                                (got / total if total else None, "скачано %.1f МБ" % (got / 1048576), j["id"]))
            if first:
                raise ValueError("пустой ответ")
            if total and got != total:
                raise ValueError("загрузка оборвалась (%d из %d байт)" % (got, total))
    except Exception as e:  # noqa
        try:
            os.unlink(dst)
        except OSError:
            pass
        con.execute("UPDATE jobs SET status='error', message=?, finished=?, progress=NULL WHERE id=? AND status<>'cancelled'",
                    ("не удалось скачать: %s" % e, time.time(), j["id"]))
        return
    params = {"path": dst, "branch": p["branch"], "sign": p["sign"], "keep": p.get("keep", 0), "original": p["url"]}
    con.execute("UPDATE jobs SET op='add', worker='signer', status='queued', step=NULL, progress=NULL, "
                "params=?, message='скачано, ждёт службу подписи' WHERE id=?",
                (json.dumps(params, ensure_ascii=False), j["id"]))


def _cleanup_uploads(con):
    old = time.time() - 86400
    for r in con.execute("SELECT id FROM uploads WHERE created<?", (old,)).fetchall():
        _drop_incoming(os.path.join(APP.cfg.incoming_dir, r["id"] + ".upload"))
        con.execute("DELETE FROM uploads WHERE id=?", (r["id"],))


# ====================================================================== устройства
@route("GET", "/api/hosts")
def api_hosts(req):
    con = C()
    return jresp({"hosts": inventory.host_summary(con, APP.cfg), "interval": APP.cfg.agent_interval})


@route("GET", r"/api/hosts/(\d+)")
def api_host(req, hid):
    return jresp(host_detail(C(), hid, float(req.query.get("hours", "24") or 24)))


def host_detail(con, hid, hours=24):
    h = con.execute("SELECT * FROM hosts WHERE id=?", (int(hid),)).fetchone()
    if not h:
        raise HTTPError(404, "устройство не найдено")
    h = dict(h)
    h.pop("secret_hash", None)
    h["facts"] = json.loads(h.get("facts") or "{}")
    h["hw"] = json.loads(h.get("hw") or "{}")
    h["metrics"] = json.loads(h.get("metrics") or "null")
    since = time.time() - hours * 3600
    hist = con.execute("SELECT ts, cpu, mem, swap, load1, disk, temp FROM host_metrics WHERE host_id=? AND ts>? "
                       "ORDER BY ts", (h["id"], since)).fetchall()
    step = max(1, len(hist) // 360)
    h["history"] = [list(r) for r in hist[::step]]
    h["status"] = inventory.host_status(h, APP.cfg.agent_interval)
    harch = inventory.host_arch(h["arch"])
    idx = inventory.repo_index(con)
    pk = db.rows(con.execute("SELECT * FROM host_packages WHERE host_id=? ORDER BY name", (h["id"],)))
    locks = [str(x).strip() for x in (h["facts"].get("versionlock") or [])]
    for p in pk:
        p["evr"] = rpmutil.fmt_evr(p["epoch"], p["version"], p["release"])
        pre = re.compile(re.escape(p["name"]) + r"-(\d+:)?\d")
        p["held"] = any(pre.match(x) for x in locks)
        p.pop("host_id", None)
        b = inventory.best_for(idx, p["name"], harch, p["arch"])
        p["repo_evr"] = inventory.evr_of(b) if b else None
        p["repo_branch"] = b["branch"] if b else None
        p["repo_cmp"] = (rpmutil.evr_cmp(b["epoch"], b["version"], b["release"], p["epoch"], p["version"],
                                         p["release"]) if b else None)
    h["packages"] = pk
    h["updates"] = inventory.updates(idx, harch, pk)
    h["tasks"] = db.rows(con.execute("SELECT * FROM tasks WHERE host_id=? ORDER BY id DESC LIMIT 60", (h["id"],)))
    return h


@route("POST", r"/api/hosts/(\d+)/report", perm="operator")
def api_host_report(req, hid):
    C().execute("UPDATE hosts SET want_report=1 WHERE id=?", (int(hid),))
    return jresp({"ok": True, "interval": APP.cfg.agent_interval})


@route("DELETE", r"/api/hosts/(\d+)", perm="admin")
def api_host_delete(req, hid):
    con = C()
    h = con.execute("SELECT name FROM hosts WHERE id=?", (int(hid),)).fetchone()
    if not h:
        raise HTTPError(404, "устройство не найдено")
    con.execute("DELETE FROM hosts WHERE id=?", (int(hid),))
    audit(req, "host_delete", h["name"])
    return jresp({"ok": True})


@route("POST", r"/api/hosts/(\d+)/collect", perm="operator")
def api_host_collect(req, hid):
    """Забрать установленный «извне» пакет в HomeRepo: агент скачает тот же пакет из его источника."""
    con = C()
    d = req.json()
    h = con.execute("SELECT * FROM hosts WHERE id=?", (int(hid),)).fetchone()
    if not h:
        raise HTTPError(404, "устройство не найдено")
    p = con.execute("SELECT * FROM host_packages WHERE host_id=? AND name=? AND arch=?",
                    (h["id"], str(d.get("name")), str(d.get("arch")))).fetchone()
    if not p:
        raise HTTPError(404, "пакет на устройстве не найден")
    branch = d.get("branch") or "extras"
    if branch not in APP.cfg.branches:
        branch = APP.cfg.default_branch
    evr = rpmutil.fmt_evr(p["epoch"], p["version"], p["release"])
    if (p["from_repo"] or "") == "@commandline":
        raise HTTPError(400, "%s установлен вручную из файла — забрать его с хоста нельзя. Загрузите этот .rpm в разделе "
                             "«Подпись»." % p["name"])
    con.execute("INSERT INTO tasks(host_id, action, name, evr, arch, params, created_by, created) "
                "VALUES(?,?,?,?,?,?,?,?)", (h["id"], "collect", p["name"], evr, p["arch"],
                                            json.dumps({"branch": branch, "from_repo": p["from_repo"] or ""}),
                                            req.uname, time.time()))
    con.execute("UPDATE hosts SET want_report=1 WHERE id=?", (h["id"],))
    audit(req, "collect", "%s %s-%s.%s → %s" % (h["name"], p["name"], evr, p["arch"], branch))
    return jresp({"ok": True})


# ====================================================================== назначения
ACTIONS = ("install", "update", "remove", "hold", "unhold")


@route("GET", "/api/tasks")
def api_tasks(req):
    con = C()
    st = req.query.get("status", "")
    q = ("SELECT t.*, h.name AS host FROM tasks t JOIN hosts h ON h.id=t.host_id %s ORDER BY t.id DESC LIMIT 300")
    if st == "active":
        rows = db.rows(con.execute(q % "WHERE t.status IN ('pending','sent','processing')"))
    elif st in ("pending", "sent", "processing", "done", "error", "skipped", "cancelled"):
        rows = db.rows(con.execute(q % "WHERE t.status=?", (st,)))
    else:
        rows = db.rows(con.execute(q % ""))
    return jresp({"tasks": rows})


@route("POST", "/api/tasks", perm="operator")
def api_tasks_create(req):
    con = C()
    d = req.json()
    action = d.get("action")
    if action not in ACTIONS:
        raise HTTPError(400, "неизвестное действие")
    items = d.get("items") or []
    host_ids = d.get("hosts") or []
    if not items or not host_ids:
        raise HTTPError(400, "выберите пакеты и устройства")
    sched = d.get("schedule") or "now"
    nb = None
    if sched == "at":
        try:
            nb = float(d.get("at"))
        except (TypeError, ValueError):
            raise HTTPError(400, "не указано время")
    elif sched not in ("now", "night"):
        raise HTTPError(400, "неизвестный режим запуска")
    idx = inventory.repo_index(con)
    hosts = {r["id"]: dict(r) for r in con.execute(
        "SELECT * FROM hosts WHERE id IN (%s)" % ",".join("?" * len(host_ids)), [int(i) for i in host_ids])}
    batch = (con.execute("SELECT max(batch) FROM tasks").fetchone()[0] or 0) + 1
    created, skipped = [], []
    now = time.time()
    with db.tx(con):
        for hid in host_ids:
            h = hosts.get(int(hid))
            if not h:
                continue
            harch = inventory.host_arch(h["arch"])
            for it in items:
                name = str(it.get("name", "")).strip()
                evr = (it.get("evr") or "").strip() or None
                if not name:
                    continue
                arch = None
                if action in ("install", "update"):
                    b = inventory.best_for(idx, name, harch, evr=evr)
                    if not b:
                        skipped.append("%s: %s — нет сборки для %s" % (h["name"], name, harch))
                        continue
                    evr = inventory.evr_of(b)
                    arch = b["arch"]
                dup = con.execute("SELECT id FROM tasks WHERE host_id=? AND name=? AND action=? AND status IN "
                                  "('pending','sent')", (h["id"], name, action)).fetchone()
                if dup:
                    con.execute("UPDATE tasks SET status='cancelled', finished=?, output='заменено новым назначением' "
                                "WHERE id=?", (now, dup["id"]))
                params = {"switch": True} if (action == "install" and it.get("switch")) else {}
                con.execute("INSERT INTO tasks(batch, host_id, action, name, evr, arch, params, schedule, not_before, "
                            "created_by, created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (batch, h["id"], action, name, evr, arch, json.dumps(params), sched, nb, req.uname, now))
                created.append("%s: %s" % (h["name"], name))
    audit(req, "assign", {"action": action, "created": created, "skipped": skipped, "schedule": sched})
    return jresp({"created": len(created), "skipped": skipped, "batch": batch})


@route("POST", r"/api/tasks/(\d+)/(cancel|retry)", perm="operator")
def api_task_op(req, tid, op):
    con = C()
    t = con.execute("SELECT t.*, h.name AS host FROM tasks t JOIN hosts h ON h.id=t.host_id WHERE t.id=?",
                    (int(tid),)).fetchone()
    if not t:
        raise HTTPError(404, "назначение не найдено")
    if op == "cancel":
        if t["status"] not in ("pending", "sent"):
            raise HTTPError(400, "назначение уже выполнено")
        con.execute("UPDATE tasks SET status='cancelled', finished=?, output='отменено' WHERE id=?",
                    (time.time(), t["id"]))
    else:
        if t["status"] not in ("error", "cancelled", "skipped"):
            raise HTTPError(400, "повторить можно только неудачное или отменённое назначение")
        con.execute("UPDATE tasks SET status='pending', tries=0, output=NULL, sent=NULL, finished=NULL, "
                    "schedule='now' WHERE id=?", (t["id"],))
    audit(req, "task_" + op, "#%d %s %s %s" % (t["id"], t["host"], t["action"], t["name"]))
    return jresp({"ok": True})


# ====================================================================== подключение
@route("GET", "/api/tokens", perm="operator")
def api_tokens(req):
    rows = db.rows(C().execute("SELECT t.id, t.note, t.created_by, t.created, t.expires, t.multi, t.uses, t.used, "
                               "h.name AS host FROM tokens t LEFT JOIN hosts h ON h.id=t.host_id "
                               "ORDER BY t.id DESC LIMIT 50"))
    return jresp({"tokens": rows})


@route("POST", "/api/tokens", perm="operator")
def api_token_new(req):
    d = req.json()
    hours = max(1, min(int(d.get("hours") or 24), 24 * 14))
    tok = auth.enroll_token()
    now = time.time()
    C().execute("INSERT INTO tokens(hash, note, created_by, created, expires, multi) VALUES(?,?,?,?,?,?)",
                (auth.sha256(auth.norm_enroll(tok)), str(d.get("note") or "")[:100], req.uname, now,
                 now + hours * 3600, 1 if d.get("multi") else 0))
    audit(req, "token_create", {"hours": hours, "multi": bool(d.get("multi")), "note": d.get("note")})
    return jresp({"token": tok, "command": setup_command(tok), "expires": now + hours * 3600})


@route("DELETE", r"/api/tokens/(\d+)", perm="operator")
def api_token_del(req, tid):
    C().execute("DELETE FROM tokens WHERE id=?", (int(tid),))
    audit(req, "token_delete", "#" + tid)
    return jresp({"ok": True})


def setup_url():
    return APP.cfg.repo_url + "/setup.sh"


def setup_command(tok=None):
    if tok:
        return "curl -fsS %s | sudo bash -s -- --token %s" % (setup_url(), tok)
    return "curl -fsS %s | sudo bash -s -- --repo-only" % setup_url()


def repo_file_text():
    cfg = APP.cfg
    out = ["# HomeRepo — домашний репозиторий (%s)" % cfg.repo_url]
    for b in cfg.branches:
        out += [
            "[homerepo-%s]" % b,
            "name=HomeRepo %s ($basearch)" % b,
            "baseurl=%s/$basearch/%s" % (cfg.repo_url, b),
            "enabled=1",
            "gpgcheck=1",
            "repo_gpgcheck=%d" % (1 if cfg.repo_gpgcheck else 0),
            "gpgkey=%s/RPM-GPG-KEY-HOMEREPO" % cfg.repo_url,
            "skip_if_unavailable=True",
            "metadata_expire=300",
            "",
        ]
    return "\n".join(out)


@route("GET", "/api/connect")
def api_connect(req):
    key = db.get_setting(C(), "key", {}) or {}
    return jresp({"repo_file": repo_file_text(), "repo_only_command": setup_command(), "setup_url": setup_url(),
                  "key_fpr": key.get("fpr"), "pub_url": APP.cfg.repo_url + "/RPM-GPG-KEY-HOMEREPO",
                  "api_url": APP.cfg.agent_api_url})


# ====================================================================== журнал и пользователи
@route("GET", "/api/audit")
def api_audit(req):
    con = C()
    q = req.query.get("q", "").strip()
    lim = min(int(req.query.get("limit", "200") or 200), 1000)
    off = int(req.query.get("offset", "0") or 0)
    if q:
        like = "%" + q + "%"
        rows = con.execute("SELECT * FROM audit WHERE user LIKE ? OR action LIKE ? OR detail LIKE ? OR ip LIKE ? "
                           "ORDER BY id DESC LIMIT ? OFFSET ?", (like, like, like, like, lim, off))
    else:
        rows = con.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ? OFFSET ?", (lim, off))
    return jresp({"audit": db.rows(rows)})


@route("GET", "/api/users", perm="admin")
def api_users(req):
    rows = db.rows(C().execute("SELECT id, name, role, must_change, disabled, created, last_login FROM users "
                               "ORDER BY name"))
    return jresp({"users": rows})


NAME_RE = re.compile(r"^[\w.-]{2,32}$", re.UNICODE)


@route("POST", "/api/users", perm="admin")
def api_user_new(req):
    con = C()
    d = req.json()
    name = str(d.get("name", "")).strip()
    role = d.get("role", "viewer")
    if not NAME_RE.match(name):
        raise HTTPError(400, "имя: 2–32 символа, буквы, цифры, . _ -")
    if role not in auth.ROLES:
        raise HTTPError(400, "неизвестная роль")
    if con.execute("SELECT 1 FROM users WHERE name=? COLLATE NOCASE", (name,)).fetchone():
        raise HTTPError(400, "такой пользователь уже есть")
    pw = auth.temp_password()
    con.execute("INSERT INTO users(name, pw_hash, role, must_change, created) VALUES(?,?,?,1,?)",
                (name, auth.hash_password(pw), role, time.time()))
    audit(req, "user_create", "%s (%s)" % (name, role))
    return jresp({"name": name, "password": pw})


def _admins_left(con, excluding):
    return con.execute("SELECT count(*) FROM users WHERE role='admin' AND disabled=0 AND id<>?",
                       (excluding,)).fetchone()[0]


@route("PUT", r"/api/users/(\d+)", perm="admin")
def api_user_edit(req, uid):
    con = C()
    uid = int(uid)
    d = req.json()
    u = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        raise HTTPError(404, "пользователь не найден")
    out = {"ok": True}
    if "role" in d:
        if d["role"] not in auth.ROLES:
            raise HTTPError(400, "неизвестная роль")
        if u["role"] == "admin" and d["role"] != "admin" and not _admins_left(con, uid):
            raise HTTPError(400, "нельзя убрать последнего администратора")
        con.execute("UPDATE users SET role=? WHERE id=?", (d["role"], uid))
        audit(req, "user_role", "%s → %s" % (u["name"], d["role"]))
    if "disabled" in d:
        dis = 1 if d["disabled"] else 0
        if dis and uid == req.user["id"]:
            raise HTTPError(400, "нельзя заблокировать себя")
        if dis and u["role"] == "admin" and not _admins_left(con, uid):
            raise HTTPError(400, "нельзя заблокировать последнего администратора")
        con.execute("UPDATE users SET disabled=? WHERE id=?", (dis, uid))
        if dis:
            con.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        audit(req, "user_disable" if dis else "user_enable", u["name"])
    if d.get("reset_password"):
        pw = auth.temp_password()
        con.execute("UPDATE users SET pw_hash=?, must_change=1 WHERE id=?", (auth.hash_password(pw), uid))
        con.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        audit(req, "user_reset_password", u["name"])
        out["password"] = pw
    return jresp(out)


@route("DELETE", r"/api/users/(\d+)", perm="admin")
def api_user_del(req, uid):
    con = C()
    uid = int(uid)
    u = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        raise HTTPError(404, "пользователь не найден")
    if uid == req.user["id"]:
        raise HTTPError(400, "нельзя удалить себя")
    if u["role"] == "admin" and not _admins_left(con, uid):
        raise HTTPError(400, "нельзя удалить последнего администратора")
    con.execute("DELETE FROM users WHERE id=?", (uid,))
    audit(req, "user_delete", u["name"])
    return jresp({"ok": True})


# ====================================================================== статика
def static(req):
    if req.method not in ("GET", "HEAD"):
        raise HTTPError(405, "метод не поддерживается")
    p = req.path
    if p.startswith("/repos-web"):
        # старые закладки на PHP-панель ведут на новую
        return Resp(302, b"", "text/plain", [("Location", "/")])
    if p in ("/", "/index.html"):
        p = "/index.html"
    root = os.path.realpath(APP.cfg.web_dir)
    full = os.path.realpath(os.path.join(root, p.lstrip("/")))
    if not full.startswith(root + os.sep) or not os.path.isfile(full):
        raise HTTPError(404, "не найдено")
    with open(full, "rb") as f:
        data = f.read()
    ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
        ctype += "; charset=utf-8"
    if full.endswith(".js"):
        ctype = "application/javascript; charset=utf-8"
    if p == "/index.html":
        data = data.replace(b"{{VERSION}}", VERSION.encode())
        cache = "no-cache"
    else:
        cache = "public, max-age=3600" if "v" in req.query else "no-cache"
    return Resp(200, data, ctype, [("Cache-Control", cache)])


# ====================================================================== запуск
def main():
    global APP
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    os.umask(0o002)
    cfg = config.get()
    APP = App(cfg)
    from . import agentapi, errors, reports, scanner  # noqa: F401  (регистрируют свои маршруты)

    threading.Thread(target=fetch_worker, daemon=True, name="fetch").start()
    fam = socket.AF_INET6 if ":" in cfg.listen else socket.AF_INET

    class Server(ThreadingHTTPServer):
        address_family = fam
        daemon_threads = True
        request_queue_size = 64

    srv = Server((cfg.listen, cfg.port), Handler)
    log.info("HomeRepo %s: панель на http://%s:%d", VERSION, cfg.listen, cfg.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    # запуск как python3 -m homerepo.web: работаем через канонический модуль homerepo.web,
    # иначе маршруты агента зарегистрируются в другой копии модуля
    from homerepo import web as _web

    _web.main()
