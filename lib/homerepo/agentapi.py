"""API для агентов на хостах и скрипт подключения setup.sh."""

import base64
import datetime
import json
import os
import re
import time

from . import auth, db, inventory
from . import web
from .web import HTTPError, Resp, jresp, route

AGENT_TASK_TIMEOUT = 3600


def cfg():
    return web.APP.cfg


def agent_source():
    with open(cfg().agent_file, "rb") as f:
        return f.read()


def agent_version():
    m = re.search(rb'^AGENT_VERSION\s*=\s*"([^"]+)"', agent_source(), re.M)
    return m.group(1).decode() if m else "?"


def ca_pem():
    p = cfg().agent_ca_file
    if p and os.path.isfile(p):
        with open(p, "rb") as f:
            return f.read()
    return b""


# ---------------------------------------------------------------- публичные файлы
@route("GET", "/agent/homerepo-agent.py", perm=None)
def get_agent(req):
    return Resp(200, agent_source(), "text/x-python; charset=utf-8", [("Cache-Control", "no-cache")])


@route("GET", "/agent/ca.pem", perm=None)
def get_ca(req):
    return Resp(200, ca_pem(), "application/x-pem-file", [("Cache-Control", "no-cache")])


@route("GET", "/agent/homerepo.repo", perm=None)
def get_repo_file(req):
    return Resp(200, web.repo_file_text().encode("utf-8"), "text/plain; charset=utf-8",
                [("Cache-Control", "no-cache")])


@route("GET", "/agent/setup.sh", perm=None)
def get_setup(req):
    c = cfg()
    tpl = SETUP_SH
    repl = {
        "@REPO_URL@": c.repo_url,
        "@API_URL@": c.agent_api_url,
        "@REPO_FILE_B64@": base64.b64encode(web.repo_file_text().encode("utf-8")).decode(),
        "@AGENT_B64@": base64.b64encode(agent_source()).decode(),
        "@CA_B64@": base64.b64encode(ca_pem()).decode(),
        "@INTERVAL@": str(c.agent_interval),
    }
    for k, v in repl.items():
        tpl = tpl.replace(k, v)
    return Resp(200, tpl.encode("utf-8"), "text/x-shellscript; charset=utf-8", [("Cache-Control", "no-cache")])


# ---------------------------------------------------------------- регистрация
@route("POST", "/api/agent/enroll", perm=None)
def enroll(req):
    con = web.C()
    d = req.json()
    tok = auth.norm_enroll(d.get("token"))
    if not tok:
        raise HTTPError(400, "нет токена")
    now = time.time()
    keys = ["enroll:" + req.ip]
    wait = web._fail_check(con, keys)
    if wait:
        raise HTTPError(429, "слишком много неверных токенов, подождите %d мин" % (wait // 60 + 1))
    t = con.execute("SELECT * FROM tokens WHERE hash=?", (auth.sha256(tok),)).fetchone()
    if not t or t["expires"] < now or (t["used"] and not t["multi"]):
        web._fail_add(con, keys)
        raise HTTPError(403, "токен неверный, просрочен или уже использован — создайте новый в панели")
    name = _clean(d.get("hostname"), 200) or req.ip
    mid = _clean(d.get("machine_id"), 64)
    secret = auth.new_token(32)
    with db.tx(con):
        h = con.execute("SELECT id FROM hosts WHERE machine_id=?", (mid,)).fetchone() if mid else None
        if h:
            hid = h["id"]
            con.execute("UPDATE hosts SET name=?, fqdn=?, secret_hash=?, ip=?, want_report=1 WHERE id=?",
                        (name.split(".")[0], name, auth.sha256(secret), req.ip, hid))
        else:
            cur = con.execute("INSERT INTO hosts(name, fqdn, machine_id, ip, arch, secret_hash, enrolled) "
                              "VALUES(?,?,?,?,?,?,?)", (name.split(".")[0], name, mid or None, req.ip,
                                                         _clean(d.get("arch"), 20), auth.sha256(secret), now))
            hid = cur.lastrowid
        con.execute("UPDATE tokens SET used=?, uses=uses+1, host_id=? WHERE id=?", (now, hid, t["id"]))
    db.audit(con, "agent:" + name, req.ip, "host_enroll", "токен #%d" % t["id"])
    return jresp({"host_id": hid, "secret": secret, "interval": cfg().agent_interval})


def _num(v):
    try:
        f = float(v)
        return f if f == f and abs(f) < 1e18 else None
    except (TypeError, ValueError):
        return None


def _metrics(m):
    """Принимаем от агента только числа и короткие строки известных полей."""
    if not isinstance(m, dict):
        return None
    out = {k: _num(m.get(k)) for k in ("cpu", "cores", "mem_total", "mem_used", "mem_pct", "swap_total", "swap_used",
                                       "swap_pct", "disk_pct", "temp", "uptime")}
    ld = m.get("load")
    out["load"] = [_num(x) for x in ld[:3]] if isinstance(ld, list) else None
    dk = []
    for x in (m.get("disks") or [])[:20]:
        if isinstance(x, dict):
            dk.append({"mount": _clean(x.get("mount"), 200), "dev": _clean(x.get("dev"), 200), "fs": _clean(x.get("fs"), 20),
                       "size": _num(x.get("size")), "used": _num(x.get("used")), "pct": _num(x.get("pct"))})
    out["disks"] = dk
    return out


def _hw(h):
    if not isinstance(h, dict):
        return {}
    out = {k: _clean(h.get(k), 200) for k in ("pretty_name", "model", "virt", "cpu_model", "selinux", "timezone")}
    for k in ("cores", "mem_total", "swap_total", "boot"):
        out[k] = _num(h.get(k))
    out["interfaces"] = [{"name": _clean(i.get("name"), 40), "addr": _clean(i.get("addr"), 60), "mac": _clean(i.get("mac"), 40)}
                         for i in (h.get("interfaces") or [])[:32] if isinstance(i, dict)]
    return out


def _clean(v, n):
    return re.sub(r"[\x00-\x1f]", "", str(v or "")).strip()[:n]


# ---------------------------------------------------------------- опрос
def _due(t, now):
    if t["schedule"] == "night":
        hr = datetime.datetime.fromtimestamp(now).hour
        return 3 <= hr < 5
    if t["schedule"] == "at":
        return now >= (t["not_before"] or 0)
    return True


@route("POST", "/api/agent/poll", perm="agent")
def poll(req):
    con = web.C()
    h = req.host
    d = req.json()
    now = time.time()
    tasks = []
    with db.tx(con):
        # назначения, по которым агент не ответил
        for t in con.execute("SELECT * FROM tasks WHERE host_id=? AND status='sent' AND sent<?",
                             (h["id"], now - AGENT_TASK_TIMEOUT)).fetchall():
            if t["tries"] >= 3:
                con.execute("UPDATE tasks SET status='error', finished=?, output='агент не сообщил результат' "
                            "WHERE id=?", (now, t["id"]))
            else:
                con.execute("UPDATE tasks SET status='pending' WHERE id=?", (t["id"],))
        for t in con.execute("SELECT * FROM tasks WHERE host_id=? AND status='pending' ORDER BY id", (h["id"],)):
            if _due(t, now):
                tasks.append(dict(t))
        for t in tasks:
            con.execute("UPDATE tasks SET status='sent', sent=?, tries=tries+1 WHERE id=?", (now, t["id"]))
        con.execute("UPDATE hosts SET last_poll=?, ip=?, agent_version=? WHERE id=?",
                    (now, req.ip, _clean(d.get("agent_version"), 20), h["id"]))
        m = _metrics(d.get("metrics"))
        if m:
            con.execute("UPDATE hosts SET metrics=?, metrics_ts=? WHERE id=?",
                        (json.dumps(m, ensure_ascii=False), now, h["id"]))
            con.execute("INSERT INTO host_metrics(host_id, ts, cpu, mem, swap, load1, disk, temp) VALUES(?,?,?,?,?,?,?,?)",
                        (h["id"], now, m.get("cpu"), m.get("mem_pct"), m.get("swap_pct"),
                         (m.get("load") or [None])[0], m.get("disk_pct"), m.get("temp")))
            if int(now) % 50 == 0:
                con.execute("DELETE FROM host_metrics WHERE ts<?", (now - 7 * 86400,))
    want = bool(h["want_report"]) or not h["last_report"] or now - h["last_report"] > cfg().agent_report_every
    out = [{"id": t["id"], "action": t["action"], "name": t["name"], "evr": t["evr"], "arch": t["arch"],
            "params": json.loads(t["params"] or "{}")} for t in tasks]
    return jresp({"report": want, "tasks": out, "interval": cfg().agent_interval, "agent_version": agent_version(),
                  "server_time": now})


@route("POST", "/api/agent/report", perm="agent")
def report(req):
    con = web.C()
    h = req.host
    d = req.json()
    key = db.get_setting(con, "key", {}) or {}
    our = set(key.get("keyids") or [])
    homerepo_ids = set(d.get("homerepo_ids") or [])
    os_repos = set(d.get("os_repos") or [])
    os_keys = set(k.lower()[-16:] for k in (d.get("os_keys") or []) if k)
    pk = d.get("packages") or []
    rows = []
    for p in pk:
        if not isinstance(p, list) or len(p) < 8:
            continue
        name, epoch, ver, rel, arch, fr, sig, itime = p[:8]
        item = {"name": _clean(name, 200), "from_repo": _clean(fr, 100), "sigkey": _clean(sig, 40).lower()}
        cls = inventory.classify(item, homerepo_ids, os_repos, os_keys, our)
        rows.append((h["id"], item["name"], int(epoch or 0), _clean(ver, 100), _clean(rel, 100), _clean(arch, 20),
                     item["from_repo"], item["sigkey"], cls, int(itime or 0)))
    facts = {k: d.get(k) for k in ("repos", "homerepo_ids", "os_repos", "os_keys", "uptime", "dnf", "python",
                                   "versionlock") if k in d}
    hw = _hw(d.get("hw"))
    now = time.time()
    with db.tx(con):
        con.execute("DELETE FROM host_packages WHERE host_id=?", (h["id"],))
        con.executemany("INSERT INTO host_packages(host_id, name, epoch, version, release, arch, from_repo, sigkey, "
                        "cls, installtime) VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
        fq = _clean(d.get("hostname"), 200) or h["fqdn"]
        con.execute("UPDATE hosts SET name=?, fqdn=?, os_name=?, os_version=?, arch=?, kernel=?, agent_version=?, "
                    "last_report=?, last_poll=?, want_report=0, pkg_total=?, facts=?, ip=?, hw=? WHERE id=?",
                    (fq.split(".")[0], fq, _clean(d.get("os_name"), 100), _clean(d.get("os_version"), 50),
                     _clean(d.get("arch"), 20), _clean(d.get("kernel"), 100), _clean(d.get("agent_version"), 20),
                     now, now, len(rows), json.dumps(facts, ensure_ascii=False), req.ip,
                     json.dumps(hw, ensure_ascii=False) if hw else (h.get("hw") or "{}"), h["id"]))
    return jresp({"ok": True, "packages": len(rows)})


@route("POST", "/api/agent/result", perm="agent")
def result(req):
    con = web.C()
    d = req.json()
    st = d.get("status")
    if st not in ("done", "error", "skipped", "running"):
        raise HTTPError(400, "неверный статус")
    t = con.execute("SELECT * FROM tasks WHERE id=? AND host_id=?", (int(d.get("task_id") or 0), req.host["id"])).fetchone()
    if not t:
        raise HTTPError(404, "назначение не найдено")
    if t["status"] in ("cancelled",):
        return jresp({"ok": True, "ignored": True})
    out = str(d.get("output") or "")[-6000:]
    if st == "running":
        con.execute("UPDATE tasks SET output=? WHERE id=?", (out, t["id"]))
    else:
        con.execute("UPDATE tasks SET status=?, output=?, finished=? WHERE id=?", (st, out, time.time(), t["id"]))
        con.execute("UPDATE hosts SET want_report=1 WHERE id=?", (req.host["id"],))
    return jresp({"ok": True})


@route("PUT", r"/api/agent/upload/(\d+)", perm="agent")
def upload(req, tid):
    """Агент присылает пакет по назначению collect — он уходит службе подписи."""
    con = web.C()
    t = con.execute("SELECT * FROM tasks WHERE id=? AND host_id=? AND action='collect'",
                    (int(tid), req.host["id"])).fetchone()
    if not t or t["status"] not in ("sent", "pending"):
        raise HTTPError(404, "назначение не найдено")
    dst = os.path.join(cfg().incoming_dir, "agent-%d.rpm" % t["id"])
    with open(dst, "wb") as f:
        req.stream_to(f, cfg().upload_max)
    with open(dst, "rb") as f:
        if f.read(4) != b"\xed\xab\xee\xdb":
            os.unlink(dst)
            raise HTTPError(400, "это не RPM-пакет")
    p = json.loads(t["params"] or "{}")
    branch = p.get("branch") if p.get("branch") in cfg().branches else cfg().default_branch
    got = _clean(req.query.get("got"), 100)
    newer = bool(got) and not got.endswith(":%s.%s" % ((t["evr"] or "").split(":")[-1], t["arch"]))
    title = "%s-%s (с %s)" % (t["name"], got.split(":", 1)[-1] if got else "%s.%s" % (t["evr"], t["arch"]), req.host["name"])
    jid = db.add_job(con, "add", {"path": dst, "branch": branch, "sign": True, "keep": 0, "task": t["id"],
                                  "original": "host:%s" % req.host["name"]}, title, t["created_by"] or req.uname)
    note = ("точной версии у источника уже нет — взята последняя: %s. " % got.split(":", 1)[-1]) if newer else ""
    con.execute("UPDATE tasks SET status='processing', output=? WHERE id=?",
                (note + "пакет получен с хоста, служба подписи проверяет и публикует его (задание #%d)" % jid, t["id"]))
    db.audit(con, "agent:" + req.host["name"], req.ip, "collect_upload", title)
    return jresp({"ok": True, "job": jid})


SETUP_SH = r'''#!/bin/bash
# HomeRepo: подключение хоста к домашнему репозиторию и установка агента.
#   curl -fsS @REPO_URL@/setup.sh | sudo bash -s -- --token XXXX-XXXX-XXXX-XXXX
#   curl -fsS @REPO_URL@/setup.sh | sudo bash -s -- --repo-only     (только репозиторий, без агента)
#   curl -fsS @REPO_URL@/setup.sh | sudo bash -s -- --uninstall     (удалить агента, репозиторий оставить)
set -euo pipefail
REPO_URL="@REPO_URL@"
API_URL="@API_URL@"
TOKEN=""; MODE="agent"
while [ $# -gt 0 ]; do
  case "$1" in
    --token) TOKEN="${2:-}"; shift 2 ;;
    --repo-only) MODE="repo"; shift ;;
    --uninstall) MODE="uninstall"; shift ;;
    *) echo "неизвестный параметр: $1" >&2; exit 2 ;;
  esac
done
[ "$(id -u)" = 0 ] || { echo "запустите от root (sudo)" >&2; exit 1; }
say(){ printf '\033[36m==>\033[0m %s\n' "$*"; }

if [ "$MODE" = uninstall ]; then
  systemctl disable --now homerepo-agent.timer 2>/dev/null || true
  rm -f /etc/systemd/system/homerepo-agent.service /etc/systemd/system/homerepo-agent.timer
  systemctl daemon-reload
  rm -rf /usr/local/libexec/homerepo-agent /etc/homerepo-agent
  say "агент удалён (файл репозитория /etc/yum.repos.d/homerepo.repo оставлен)"
  exit 0
fi

say "ключ подписи репозитория"
install -d -m 755 /etc/pki/rpm-gpg
curl -fsS "$REPO_URL/RPM-GPG-KEY-HOMEREPO" -o /etc/pki/rpm-gpg/RPM-GPG-KEY-HOMEREPO.new
grep -q "BEGIN PGP PUBLIC KEY BLOCK" /etc/pki/rpm-gpg/RPM-GPG-KEY-HOMEREPO.new || { echo "ключ не скачался" >&2; exit 1; }
mv -f /etc/pki/rpm-gpg/RPM-GPG-KEY-HOMEREPO.new /etc/pki/rpm-gpg/RPM-GPG-KEY-HOMEREPO
rpm --import /etc/pki/rpm-gpg/RPM-GPG-KEY-HOMEREPO

say "файл репозитория /etc/yum.repos.d/homerepo.repo"
ts=$(date +%Y%m%d-%H%M%S)
for f in /etc/yum.repos.d/*.repo; do
  [ -f "$f" ] || continue
  [ "$f" = /etc/yum.repos.d/homerepo.repo ] && continue
  if grep -qE '^\s*baseurl\s*=.*/homerepo/' "$f"; then
    mv -f "$f" "$f.disabled-$ts"; echo "    старый файл $f → $f.disabled-$ts"
  fi
done
echo "@REPO_FILE_B64@" | base64 -d > /etc/yum.repos.d/homerepo.repo
chmod 644 /etc/yum.repos.d/homerepo.repo
dnf -q makecache --repo 'homerepo-*' >/dev/null 2>&1 || dnf -q makecache >/dev/null 2>&1 || true

if [ "$MODE" = repo ]; then say "готово: репозиторий подключён"; exit 0; fi
[ -n "$TOKEN" ] || { echo "нужен --token (создаётся в панели, раздел «Подключение»)" >&2; exit 2; }

PY=""
for p in /usr/libexec/platform-python /usr/bin/python3; do
  if [ -x "$p" ] && "$p" -c 'import dnf' 2>/dev/null; then PY="$p"; break; fi
done
[ -n "$PY" ] || for p in /usr/bin/python3 /usr/libexec/platform-python; do [ -x "$p" ] && { PY="$p"; break; }; done
[ -n "$PY" ] || { echo "не найден python3" >&2; exit 1; }

say "агент homerepo-agent ($PY)"
install -d -m 755 /usr/local/libexec/homerepo-agent
install -d -m 700 /etc/homerepo-agent
echo "@AGENT_B64@" | base64 -d > /usr/local/libexec/homerepo-agent/homerepo-agent.py
chmod 755 /usr/local/libexec/homerepo-agent/homerepo-agent.py
echo "@CA_B64@" | base64 -d > /etc/homerepo-agent/ca.pem
chmod 644 /etc/homerepo-agent/ca.pem

cat > /etc/systemd/system/homerepo-agent.service <<EOF
[Unit]
Description=HomeRepo agent (отчёт о пакетах и выполнение назначений)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$PY /usr/local/libexec/homerepo-agent/homerepo-agent.py run
TimeoutStartSec=3h
Nice=10
EOF
cat > /etc/systemd/system/homerepo-agent.timer <<EOF
[Unit]
Description=HomeRepo agent timer

[Timer]
OnBootSec=1min
OnUnitInactiveSec=@INTERVAL@s
RandomizedDelaySec=15s
Persistent=false

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload

say "регистрация в панели $API_URL"
"$PY" /usr/local/libexec/homerepo-agent/homerepo-agent.py enroll --server "$API_URL" --token "$TOKEN"
systemctl enable --now homerepo-agent.timer >/dev/null
systemctl start homerepo-agent.service || true
say "готово: хост появится в панели в течение минуты"
'''
