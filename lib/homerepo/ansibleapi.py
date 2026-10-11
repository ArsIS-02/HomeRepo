"""API раздела «Плейбуки»: конструктор, запуски Ansible, инвентарь из HomeRepo, доступ Ansible через агента."""

import base64
import hashlib
import json
import os
import time

from . import db, inventory, playbooks
from . import web
from .web import HTTPError, jresp, route

LOCAL_IPS = ("127.0.0.1", "::1", "localhost")
RUN_MODES = ("check", "run", "syntax")


def cfg():
    return web.APP.cfg


def C():
    return web.C()


def key_fp(pub):
    try:
        raw = base64.b64decode(pub.split()[1])
    except (IndexError, ValueError):
        return ""
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def runner_info(con):
    r = db.get_setting(con, "runner", {}) or {}
    alive = bool(r.get("ts")) and time.time() - r["ts"] < 30
    pub = r.get("pubkey") or ""
    return {"alive": alive, "state": r.get("state"), "version": r.get("version"), "ansible": r.get("ansible"),
            "pubkey": pub, "fingerprint": key_fp(pub) if pub else "", "remote_user": r.get("remote_user") or cfg().ansible_user,
            "enabled": cfg().ansible_enabled}


def access_of(h, fp):
    try:
        a = json.loads(h.get("ansible") or "{}") or {}
    except ValueError:
        a = {}
    st = a.get("state") or "none"
    if st == "ok" and fp and a.get("key") and a["key"] != fp:
        st = "stale"  # ключ исполнителя сменился — доступ надо выдать заново
    a["state"] = st
    return a


def host_groups(h):
    g = ["homerepo"]
    if h.get("os_name"):
        g.append("os_" + (h["os_name"] or "").lower())
    if h.get("arch"):
        g.append("arch_" + h["arch"])
    return g


def targets(con):
    fp = runner_info(con)["fingerprint"]
    out = []
    for h in db.rows(con.execute("SELECT * FROM hosts ORDER BY name")):
        a = access_of(h, fp)
        out.append({"id": h["id"], "name": h["name"], "fqdn": h["fqdn"], "ip": h["ip"], "os_name": h["os_name"],
                    "os_version": h["os_version"], "arch": h["arch"],
                    "status": inventory.host_status(h, cfg().agent_interval),
                    "access": a, "groups": host_groups(h), "local": (h["ip"] or "") in LOCAL_IPS})
    return out


def target_spec(t):
    from .runner import group_name
    spec = {"name": t["name"], "host": t["ip"] or t["fqdn"] or t["name"],
            "groups": [group_name(g) for g in t["groups"]]}
    if t["local"]:
        spec["connection"] = "local"
    elif t["access"].get("user") and t["access"]["user"] != cfg().ansible_user:
        spec["user"] = t["access"]["user"]
    return spec


def render_ctx(con):
    return {"repo_url": cfg().repo_url, "branches": cfg().branches}


# ---------------------------------------------------------------------- состояние, каталог, инвентарь
@route("GET", "/api/ansible/status")
def api_status(req):
    con = C()
    ri = runner_info(con)
    ts = targets(con)
    counts = {}
    for t in ts:
        counts[t["access"]["state"]] = counts.get(t["access"]["state"], 0) + 1
    last = db.rows(con.execute("SELECT id, name, mode, status, message, created, finished, created_by FROM runs "
                               "ORDER BY id DESC LIMIT 1"))
    return jresp({"runner": ri, "hosts": len(ts), "access": counts, "last_run": last[0] if last else None,
                  "running": con.execute("SELECT count(*) FROM runs WHERE status IN ('queued','running','cancel')").fetchone()[0]})


@route("GET", "/api/ansible/catalog")
def api_catalog(req):
    c = playbooks.catalog()
    c["repo_packages"] = sorted({r[0] for r in C().execute("SELECT name FROM packages")})
    c["branches"] = cfg().branches
    return jresp(c)


@route("GET", "/api/ansible/recipes")
def api_recipes(req):
    return jresp({"recipes": [{"id": r["id"], "name": r["name"], "description": r["description"],
                               "steps": [playbooks.BY_TYPE[s["type"]]["title"] for s in r["steps"]]}
                              for r in playbooks.RECIPES]})


@route("GET", "/api/ansible/targets")
def api_targets(req):
    con = C()
    return jresp({"targets": targets(con), "runner": runner_info(con)})


def dynamic_inventory(con):
    inv = {"_meta": {"hostvars": {}}, "all": {"children": ["ungrouped"]}, "ungrouped": {"hosts": []}}
    for t in targets(con):
        s = target_spec(t)
        hv = {"ansible_host": s["host"], "homerepo_os": "%s %s" % (t["os_name"] or "", t["os_version"] or ""),
              "homerepo_arch": t["arch"], "homerepo_status": t["status"], "homerepo_ansible_access": t["access"]["state"]}
        if s.get("connection"):
            hv["ansible_connection"] = s["connection"]
        if s.get("user"):
            hv["ansible_user"] = s["user"]
        inv["_meta"]["hostvars"][s["name"]] = hv
        for g in s["groups"]:
            inv.setdefault(g, {"hosts": []})["hosts"].append(s["name"])
            if g not in inv["all"]["children"]:
                inv["all"]["children"].append(g)
    return inv


@route("GET", "/api/ansible/inventory", perm="operator")
def api_inventory(req):
    return jresp(dynamic_inventory(C()))


# ---------------------------------------------------------------------- доступ Ansible через агента
@route("POST", "/api/ansible/access", perm="admin")
def api_access(req):
    con = C()
    d = req.json()
    op = d.get("op") if d.get("op") in ("grant", "revoke") else None
    if not op:
        raise HTTPError(400, "op: grant или revoke")
    ri = runner_info(con)
    if op == "grant" and not ri["pubkey"]:
        raise HTTPError(409, "у исполнителя ещё нет SSH-ключа — проверьте службу homerepo-runner")
    ids = [int(x) for x in d.get("hosts") or []]
    if not ids:
        raise HTTPError(400, "не выбраны устройства")
    now = time.time()
    names = []
    for hid in ids:
        h = con.execute("SELECT id, name FROM hosts WHERE id=?", (hid,)).fetchone()
        if not h:
            continue
        params = {"op": op, "user": ri["remote_user"]}
        if op == "grant":
            params["pubkey"] = ri["pubkey"]
        con.execute("UPDATE tasks SET status='cancelled', finished=? WHERE host_id=? AND action='ansible_access' "
                    "AND status='pending'", (now, hid))
        con.execute("INSERT INTO tasks(host_id, action, name, params, created_by, created) VALUES(?,?,?,?,?,?)",
                    (hid, "ansible_access", ri["remote_user"], json.dumps(params), req.uname, now))
        con.execute("UPDATE hosts SET ansible=? WHERE id=?",
                    (json.dumps({"state": "pending", "op": op, "user": ri["remote_user"], "key": ri["fingerprint"],
                                 "ts": now}), hid))
        names.append(h["name"])
    web.audit(req, "ansible_access", {"op": op, "hosts": names, "user": ri["remote_user"]})
    return jresp({"ok": True, "hosts": names})


def on_access_result(con, task, status, output):
    """Вызывается из API агента, когда агент сообщил результат назначения ansible_access."""
    try:
        p = json.loads(task["params"] or "{}")
    except ValueError:
        p = {}
    fp = key_fp(p.get("pubkey") or "") if p.get("pubkey") else ""
    if status == "done":
        st = "ok" if p.get("op") == "grant" else "none"
    elif status == "error":
        st = "error"
    else:
        return
    con.execute("UPDATE hosts SET ansible=? WHERE id=?",
                (json.dumps({"state": st, "op": p.get("op"), "user": p.get("user"), "key": fp,
                             "ts": time.time(), "msg": (output or "")[-300:]}), task["host_id"]))


# ---------------------------------------------------------------------- плейбуки
def _pb_out(r, full=False):
    d = dict(r)
    try:
        d["model"] = json.loads(d.get("model") or "null")
    except ValueError:
        d["model"] = None
    if not full:
        d.pop("yaml", None)
        m = d.pop("model")
        d["steps"] = len((m or {}).get("steps") or []) if m else None
    return d


@route("GET", "/api/playbooks")
def api_playbooks(req):
    con = C()
    rows = db.rows(con.execute("SELECT * FROM playbooks ORDER BY name COLLATE NOCASE"))
    last = {}
    for r in con.execute("SELECT playbook_id, id, status, mode, finished, created, message FROM runs WHERE id IN "
                         "(SELECT max(id) FROM runs WHERE playbook_id IS NOT NULL GROUP BY playbook_id)"):
        last[r["playbook_id"]] = dict(r)
    out = []
    for r in rows:
        d = _pb_out(r)
        d["last_run"] = last.get(r["id"])
        out.append(d)
    return jresp({"playbooks": out})


def _save(con, req, d, pid=None):
    name = str(d.get("name") or "").strip()[:120]
    if not name:
        raise HTTPError(400, "назовите плейбук")
    desc = str(d.get("description") or "").strip()[:500]
    raw = bool(d.get("raw"))
    errors = {}
    if raw:
        text = str(d.get("yaml") or "")
        if not text.strip():
            raise HTTPError(400, "пустой YAML")
        if len(text) > 512 * 1024:
            raise HTTPError(400, "слишком большой плейбук")
        model = None
    else:
        model = d.get("model") or {}
        if not isinstance(model, dict):
            raise HTTPError(400, "неверная модель плейбука")
        model["name"], model["description"] = name, desc
        text, errors = playbooks.render(model, render_ctx(con))
    now = time.time()
    mj = json.dumps(model, ensure_ascii=False) if model is not None else None
    if pid:
        con.execute("UPDATE playbooks SET name=?, description=?, model=?, yaml=?, raw=?, updated_by=?, updated=? WHERE id=?",
                    (name, desc, mj, text, 1 if raw else 0, req.uname, now, pid))
    else:
        pid = con.execute("INSERT INTO playbooks(name, description, model, yaml, raw, source, created_by, created, "
                          "updated_by, updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (name, desc, mj, text, 1 if raw else 0, d.get("source") or None, req.uname, now, req.uname,
                           now)).lastrowid
    return pid, text, errors


@route("POST", "/api/playbooks", perm="operator")
def api_pb_create(req):
    con = C()
    d = req.json()
    if d.get("recipe"):
        m = playbooks.recipe_model(d["recipe"])
        if not m:
            raise HTTPError(404, "рецепт не найден")
        d = {"name": d.get("name") or m["name"], "description": m["description"], "model": m, "source": "recipe:" + d["recipe"]}
    pid, text, errors = _save(con, req, d)
    web.audit(req, "playbook_create", d.get("name") or "")
    return jresp({"id": pid, "yaml": text, "errors": errors})


@route("GET", r"/api/playbooks/(\d+)")
def api_pb_get(req, pid):
    con = C()
    r = con.execute("SELECT * FROM playbooks WHERE id=?", (int(pid),)).fetchone()
    if not r:
        raise HTTPError(404, "плейбук не найден")
    d = _pb_out(r, True)
    d["secrets"] = playbooks.secrets_needed(d["model"]) if d["model"] else []
    d["runs"] = db.rows(con.execute("SELECT id, mode, status, message, created_by, created, started, finished FROM runs "
                                    "WHERE playbook_id=? ORDER BY id DESC LIMIT 20", (int(pid),)))
    return jresp(d)


@route("PUT", r"/api/playbooks/(\d+)", perm="operator")
def api_pb_put(req, pid):
    con = C()
    if not con.execute("SELECT 1 FROM playbooks WHERE id=?", (int(pid),)).fetchone():
        raise HTTPError(404, "плейбук не найден")
    d = req.json()
    pid, text, errors = _save(con, req, d, int(pid))
    web.audit(req, "playbook_save", d.get("name") or "")
    return jresp({"id": pid, "yaml": text, "errors": errors})


@route("DELETE", r"/api/playbooks/(\d+)", perm="operator")
def api_pb_del(req, pid):
    con = C()
    r = con.execute("SELECT name FROM playbooks WHERE id=?", (int(pid),)).fetchone()
    if not r:
        raise HTTPError(404, "плейбук не найден")
    con.execute("DELETE FROM playbooks WHERE id=?", (int(pid),))
    web.audit(req, "playbook_delete", r["name"])
    return jresp({"ok": True})


@route("POST", r"/api/playbooks/(\d+)/copy", perm="operator")
def api_pb_copy(req, pid):
    con = C()
    r = con.execute("SELECT * FROM playbooks WHERE id=?", (int(pid),)).fetchone()
    if not r:
        raise HTTPError(404, "плейбук не найден")
    now = time.time()
    nid = con.execute("INSERT INTO playbooks(name, description, model, yaml, raw, source, created_by, created, updated_by, "
                      "updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (r["name"] + " (копия)", r["description"], r["model"], r["yaml"], r["raw"], "copy:%d" % r["id"],
                       req.uname, now, req.uname, now)).lastrowid
    return jresp({"id": nid})


@route("POST", "/api/playbooks/render", perm="operator")
def api_pb_render(req):
    d = req.json()
    m = d.get("model") or {}
    if not isinstance(m, dict):
        raise HTTPError(400, "неверная модель")
    text, errors = playbooks.render(m, render_ctx(C()))
    return jresp({"yaml": text, "errors": errors, "secrets": playbooks.secrets_needed(m)})


# ---------------------------------------------------------------------- запуски
def _run_out(r):
    d = dict(r)
    for k in ("targets", "opts", "recap"):
        try:
            d[k] = json.loads(d.get(k) or "null")
        except ValueError:
            d[k] = None
    d.pop("yaml", None)
    if d.get("opts"):
        d["opts"].pop("vars", None)
    d["hosts"] = [t["name"] for t in d.get("targets") or []]
    d.pop("targets", None)
    return d


@route("POST", "/api/runs", perm="admin")
def api_run_create(req):
    con = C()
    d = req.json()
    mode = d.get("mode") if d.get("mode") in RUN_MODES else "check"
    pb = con.execute("SELECT * FROM playbooks WHERE id=?", (int(d.get("playbook_id") or 0),)).fetchone()
    if not pb:
        raise HTTPError(404, "сначала сохраните плейбук")
    if not cfg().ansible_enabled:
        raise HTTPError(409, "раздел плейбуков выключен в настройках ([ansible] enabled = no)")
    ids = [int(x) for x in d.get("hosts") or []]
    ts = {t["id"]: t for t in targets(con)}
    chosen = [ts[i] for i in ids if i in ts]
    if not chosen and mode != "syntax":
        raise HTTPError(400, "выберите хосты")
    specs = [target_spec(t) for t in chosen] or [{"name": "localhost", "connection": "local", "groups": []}]
    try:
        model = json.loads(pb["model"] or "null")
    except ValueError:
        model = None
    need = [s["name"] for s in playbooks.secrets_needed(model)] if model else []
    secrets = d.get("secrets") or {}
    if mode != "syntax":
        miss = [n for n in need if not str(secrets.get(n) or "")]
        if miss:
            raise HTTPError(400, "введите секретные переменные: %s" % ", ".join(miss))
    vars_ = {}
    for k, v in (d.get("vars") or {}).items():
        if isinstance(k, str) and k.isidentifier():
            vars_[k] = v
    opts = {"vars": vars_, "diff": bool(d.get("diff")), "forks": int(d.get("forks") or 0) or None,
            "host_ids": [t["id"] for t in chosen]}
    now = time.time()
    rid = con.execute("INSERT INTO runs(playbook_id, name, yaml, targets, mode, opts, created_by, created) "
                      "VALUES(?,?,?,?,?,?,?,?)", (pb["id"], pb["name"], pb["yaml"], json.dumps(specs, ensure_ascii=False),
                                                  mode, json.dumps(opts, ensure_ascii=False), req.uname, now)).lastrowid
    sec = {k: str(v) for k, v in secrets.items() if isinstance(k, str) and k.isidentifier() and k in need}
    if sec:
        os.makedirs(cfg().secrets_dir, exist_ok=True)
        path = os.path.join(cfg().secrets_dir, "%d.json" % rid)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(sec, f)
    web.audit(req, "run_start", {"playbook": pb["name"], "mode": mode, "hosts": [s["name"] for s in specs], "run": rid})
    return jresp({"run": rid})


@route("GET", "/api/runs")
def api_runs(req):
    con = C()
    lim = min(int(req.query.get("limit") or 50), 300)
    if req.query.get("playbook"):
        rows = con.execute("SELECT * FROM runs WHERE playbook_id=? ORDER BY id DESC LIMIT ?", (int(req.query["playbook"]), lim))
    else:
        rows = con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (lim,))
    return jresp({"runs": [_run_out(r) for r in rows]})


@route("GET", r"/api/runs/(\d+)")
def api_run_get(req, rid):
    con = C()
    r = con.execute("SELECT * FROM runs WHERE id=?", (int(rid),)).fetchone()
    if not r:
        raise HTTPError(404, "запуск не найден")
    ev_after = int(req.query.get("ev") or 0)
    log_after = int(req.query.get("log") or 0)
    evs = []
    for e in con.execute("SELECT id, ts, kind, host, task, task_name, status, data FROM run_events WHERE run_id=? AND id>? "
                         "ORDER BY id LIMIT 2000", (int(rid), ev_after)):
        x = dict(e)
        try:
            x["data"] = json.loads(x["data"] or "{}")
        except ValueError:
            x["data"] = {}
        evs.append(x)
    logs = db.rows(con.execute("SELECT id, text FROM run_log WHERE run_id=? AND id>? ORDER BY id LIMIT 500",
                               (int(rid), log_after)))
    out = _run_out(r)
    if req.query.get("yaml"):
        out["yaml"] = r["yaml"]
    return jresp({"run": out, "events": evs, "log": logs})


@route("POST", r"/api/runs/(\d+)/cancel", perm="admin")
def api_run_cancel(req, rid):
    con = C()
    cur = con.execute("UPDATE runs SET status='cancel' WHERE id=? AND status IN ('queued','running')", (int(rid),))
    if not cur.rowcount:
        raise HTTPError(409, "запуск уже завершён")
    r = con.execute("SELECT name FROM runs WHERE id=?", (int(rid),)).fetchone()
    web.audit(req, "run_cancel", "#%s %s" % (rid, r["name"] if r else ""))
    return jresp({"ok": True})
