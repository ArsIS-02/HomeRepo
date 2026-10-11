"""Командная строка homerepo (для root на сервере репозитория)."""

import argparse
import grp
import json
import os
import pwd
import secrets
import shutil
import sys
import time

from . import VERSION, auth, config, db

PANEL_USER = "homerepo"


def drop_privs():
    """CLI работает с БД от имени пользователя панели, чтобы не плодить root-файлы в /var/lib/homerepo."""
    if os.geteuid() != 0:
        return
    try:
        pw = pwd.getpwnam(PANEL_USER)
    except KeyError:
        return
    groups = [g.gr_gid for g in grp.getgrall() if PANEL_USER in g.gr_mem] + [pw.pw_gid]
    os.setgroups(sorted(set(groups)))
    os.setgid(pw.pw_gid)
    os.setuid(pw.pw_uid)
    os.umask(0o002)


def con():
    return db.init(config.get().db_path)


def fmt_ts(t):
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(t)) if t else "—"


def cmd_status(a):
    c = con()
    cfg = config.get()
    s = db.get_setting(c, "signer", {}) or {}
    k = db.get_setting(c, "key", {}) or {}
    alive = s.get("ts") and time.time() - s["ts"] < 30
    n = c.execute("SELECT count(*), coalesce(sum(size),0) FROM packages").fetchone()
    by = dict(c.execute("SELECT sigclass, count(*) FROM packages GROUP BY sigclass").fetchall())
    print("HomeRepo %s" % VERSION)
    print("  репозиторий:   %s (%s)" % (cfg.repo_root, cfg.repo_url))
    print("  служба подписи: %s%s" % ("работает" if alive else "НЕ РАБОТАЕТ", " — %s" % s.get("state") if alive else ""))
    print("  ключ:          %s %s" % (k.get("fpr") or "—", ("ОШИБКА: " + k["error"]) if k.get("error") else ""))
    print("  пакетов:       %d (%.1f ГБ); homerepo %d, чужая подпись %d, без подписи %d" % (
        n[0], n[1] / 1024 ** 3, by.get("ours", 0), by.get("foreign", 0), by.get("none", 0)))
    print("  устройств:     %d" % c.execute("SELECT count(*) FROM hosts").fetchone()[0])
    print("  очередь:       %d" % c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0])
    d = db.get_setting(c, "dirty_dirs", []) or []
    if d:
        print("  ждут пересборки метаданных: %s" % ", ".join(os.path.relpath(x, cfg.repo_root) for x in d))
    return 0


def cmd_user(a):
    c = con()
    if a.op == "list":
        for r in c.execute("SELECT * FROM users ORDER BY name"):
            print("%-20s %-9s %s%s  последний вход: %s" % (r["name"], r["role"], "заблокирован " if r["disabled"] else "",
                                                          "сменить пароль " if r["must_change"] else "",
                                                          fmt_ts(r["last_login"])))
        return 0
    if not a.name:
        print("укажите имя пользователя", file=sys.stderr)
        return 2
    if a.op == "add":
        if c.execute("SELECT 1 FROM users WHERE name=? COLLATE NOCASE", (a.name,)).fetchone():
            print("пользователь уже есть", file=sys.stderr)
            return 1
        pw = auth.temp_password()
        c.execute("INSERT INTO users(name, pw_hash, role, must_change, created) VALUES(?,?,?,1,?)",
                  (a.name, auth.hash_password(pw), a.role, time.time()))
        db.audit(c, "cli", "localhost", "user_create", "%s (%s)" % (a.name, a.role))
        print("пользователь %s (%s) создан\nвременный пароль: %s\n(при первом входе панель попросит его сменить)"
              % (a.name, a.role, pw))
        return 0
    u = c.execute("SELECT * FROM users WHERE name=? COLLATE NOCASE", (a.name,)).fetchone()
    if not u:
        print("пользователь не найден", file=sys.stderr)
        return 1
    if a.op == "passwd":
        pw = auth.temp_password()
        c.execute("UPDATE users SET pw_hash=?, must_change=1, disabled=0 WHERE id=?", (auth.hash_password(pw), u["id"]))
        c.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))
        c.execute("DELETE FROM login_fail WHERE key=?", ("u:" + u["name"].lower(),))
        db.audit(c, "cli", "localhost", "user_reset_password", u["name"])
        print("временный пароль для %s: %s" % (u["name"], pw))
        return 0
    if a.op == "del":
        c.execute("DELETE FROM users WHERE id=?", (u["id"],))
        db.audit(c, "cli", "localhost", "user_delete", u["name"])
        print("удалён")
        return 0
    if a.op == "role":
        c.execute("UPDATE users SET role=? WHERE id=?", (a.role, u["id"]))
        print("роль %s: %s" % (u["name"], a.role))
        return 0
    return 2


def wait_jobs(c, ids):
    pending = set(ids)
    last = {}
    while pending:
        for jid in list(pending):
            r = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            line = "#%d %s: %s %s" % (jid, r["title"], r["status"], r["message"] or r["step"] or "")
            if last.get(jid) != line:
                print(line)
                last[jid] = line
            if r["status"] in ("done", "error", "cancelled"):
                pending.discard(jid)
        time.sleep(1)
    bad = c.execute("SELECT count(*) FROM jobs WHERE status<>'done' AND id IN (%s)" % ",".join("?" * len(ids)),
                    ids).fetchone()[0]
    return 1 if bad else 0


def cmd_add(a):
    cfg = config.get()
    staged = []
    for f in a.files:
        if not os.path.isfile(f):
            print("нет файла %s" % f, file=sys.stderr)
            return 1
        dst = os.path.join(cfg.incoming_dir, "cli-%s.rpm" % secrets.token_hex(8))
        shutil.copyfile(f, dst)
        if os.geteuid() == 0:
            try:
                pw = pwd.getpwnam(PANEL_USER)
                os.chown(dst, pw.pw_uid, pw.pw_gid)
            except KeyError:
                pass
        os.chmod(dst, 0o660)
        staged.append((f, dst))
    drop_privs()
    c = con()
    ids = []
    for f, dst in staged:
        ids.append(db.add_job(c, "add", {"path": dst, "branch": a.branch or cfg.default_branch, "sign": not a.no_sign,
                                         "keep": a.keep, "original": os.path.basename(f)},
                              os.path.basename(f), "cli"))
        db.audit(c, "cli", "localhost", "upload", os.path.basename(f))
    print("в очереди: %s" % ", ".join("#%d" % i for i in ids))
    return wait_jobs(c, ids) if not a.no_wait else 0


def simple_job(op, params, title):
    def run(a):
        c = con()
        jid = db.add_job(c, op, params(a) if callable(params) else params, title, "cli")
        db.audit(c, "cli", "localhost", "repo_" + op, title)
        return wait_jobs(c, [jid]) if not getattr(a, "no_wait", False) else 0
    return run


def cmd_jobs(a):
    c = con()
    for r in c.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (a.n,)).fetchall()[::-1]:
        print("#%-5d %s %-9s %-40s %s" % (r["id"], fmt_ts(r["created"]), r["status"], (r["title"] or "")[:40],
                                          r["message"] or ""))
    return 0


def cmd_token(a):
    c = con()
    tok = auth.enroll_token()
    now = time.time()
    c.execute("INSERT INTO tokens(hash, note, created_by, created, expires, multi) VALUES(?,?,?,?,?,?)",
              (auth.sha256(auth.norm_enroll(tok)), a.note or "", "cli", now, now + a.hours * 3600, 1 if a.multi else 0))
    cfg = config.get()
    print("токен: %s (действует %d ч%s)" % (tok, a.hours, ", многоразовый" if a.multi else ""))
    print("на хосте:\n  curl -fsS %s/setup.sh | sudo bash -s -- --token %s" % (cfg.repo_url, tok))
    return 0


def _web_stub():
    from . import web

    class _A:
        cfg = config.get()
    web.APP = _A()
    return web


def cmd_inventory(a):
    """Инвентарь Ansible из HomeRepo: homerepo inventory --list (динамический, JSON) или --ini."""
    _web_stub()
    from . import ansibleapi
    c = con()
    if a.host:
        inv = ansibleapi.dynamic_inventory(c)
        print(json.dumps(inv["_meta"]["hostvars"].get(a.host, {}), ensure_ascii=False, indent=1))
        return 0
    inv = ansibleapi.dynamic_inventory(c)
    if a.ini:
        hv = inv["_meta"]["hostvars"]
        print("# инвентарь HomeRepo: %s" % time.strftime("%d.%m.%Y %H:%M"))
        print("[all]")
        for h, v in sorted(hv.items()):
            print(" ".join([h] + ["%s=%s" % (k, v[k]) for k in ("ansible_host", "ansible_user", "ansible_connection") if v.get(k)]))
        for g, d in sorted(inv.items()):
            if g in ("_meta", "all", "ungrouped") or not isinstance(d, dict) or not d.get("hosts"):
                continue
            print("\n[%s]" % g)
            for h in d["hosts"]:
                print(h)
        return 0
    print(json.dumps(inv, ensure_ascii=False, indent=1))
    return 0


def cmd_playbook(a):
    c = con()
    if a.op == "list":
        for r in c.execute("SELECT id, name, raw, source, updated FROM playbooks ORDER BY id"):
            print("#%-4d %-40s %-5s %s %s" % (r["id"], r["name"][:40], "YAML" if r["raw"] else "", fmt_ts(r["updated"]),
                                              r["source"] or ""))
        return 0
    if a.op == "import":
        if not a.files:
            print("укажите файлы .yml", file=sys.stderr)
            return 2
        import re
        n = 0
        for f in a.files:
            path = os.path.abspath(f)
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError as e:
                print("пропущен %s: %s" % (f, e), file=sys.stderr)
                continue
            src = "import:" + path
            if not a.force and c.execute("SELECT 1 FROM playbooks WHERE source=?", (src,)).fetchone():
                print("уже импортирован: %s" % path)
                continue
            m = re.search(r"^\s*-\s*name:\s*['\"]?(.+?)['\"]?\s*$", text, re.M)
            name = a.name or (m.group(1) if m else os.path.splitext(os.path.basename(path))[0])
            now = time.time()
            c.execute("INSERT INTO playbooks(name, description, model, yaml, raw, source, created_by, created, updated_by, "
                      "updated) VALUES(?,?,?,?,?,?,?,?,?,?)", (name[:120], "импортирован из %s" % path, None, text, 1, src,
                                                               "cli", now, "cli", now))
            db.audit(c, "cli", "localhost", "playbook_create", name[:120])
            print("импортирован: %s → «%s»" % (path, name))
            n += 1
        return 0
    return 2


def main():
    ap = argparse.ArgumentParser(prog="homerepo", description="HomeRepo %s — управление репозиторием" % VERSION)
    sp = ap.add_subparsers(dest="cmd")
    sp.add_parser("status", help="состояние репозитория и служб")
    u = sp.add_parser("user", help="пользователи панели")
    u.add_argument("op", choices=["list", "add", "passwd", "del", "role"])
    u.add_argument("name", nargs="?")
    u.add_argument("--role", choices=auth.ROLES, default="viewer")
    ad = sp.add_parser("add", help="добавить RPM-файлы (подпись и публикация через службу подписи)")
    ad.add_argument("files", nargs="+")
    ad.add_argument("--branch")
    ad.add_argument("--no-sign", action="store_true")
    ad.add_argument("--keep", type=int, default=0, help="сколько прошлых версий оставить (0 — все)")
    ad.add_argument("--no-wait", action="store_true")
    rs = sp.add_parser("resign", help="переподписать все пакеты с чужой подписью или без подписи")
    rs.add_argument("--no-wait", action="store_true")
    for name, h in (("rescan", "пересканировать файлы"), ("metadata", "пересобрать метаданные"),
                    ("cleanup", "удалить мусорные файлы"), ("key", "перечитать ключ подписи")):
        p = sp.add_parser(name, help=h)
        p.add_argument("--no-wait", action="store_true")
    j = sp.add_parser("jobs", help="последние задания")
    j.add_argument("-n", type=int, default=20)
    t = sp.add_parser("token", help="токен подключения устройства")
    t.add_argument("--hours", type=int, default=24)
    t.add_argument("--multi", action="store_true", help="для нескольких хостов")
    t.add_argument("--note")
    sp.add_parser("repo-file", help="вывести .repo для клиентов")
    iv = sp.add_parser("inventory", help="инвентарь Ansible из хостов HomeRepo (JSON для ansible -i, или --ini)")
    iv.add_argument("--list", action="store_true", help="динамический инвентарь (по умолчанию)")
    iv.add_argument("--host", help="переменные одного хоста")
    iv.add_argument("--ini", action="store_true", help="в формате INI")
    pbp = sp.add_parser("playbook", help="плейбуки: list | import FILE…")
    pbp.add_argument("op", choices=["list", "import"])
    pbp.add_argument("files", nargs="*")
    pbp.add_argument("--name", help="название при импорте одного файла")
    pbp.add_argument("--force", action="store_true", help="импортировать повторно")
    sp.add_parser("version")
    a = ap.parse_args()
    if not a.cmd:
        ap.print_help()
        return 0
    if a.cmd == "version":
        print(VERSION)
        return 0
    if a.cmd == "add":
        return cmd_add(a)
    drop_privs()
    if a.cmd == "repo-file":
        from . import web

        class _A:
            cfg = config.get()
        web.APP = _A()
        print(web.repo_file_text())
        return 0
    handlers = {
        "status": cmd_status, "user": cmd_user, "jobs": cmd_jobs, "token": cmd_token,
        "inventory": cmd_inventory, "playbook": cmd_playbook,
        "resign": simple_job("resign", {"all_foreign": True}, "переподписать чужие и неподписанные"),
        "rescan": simple_job("rescan", {}, "пересканировать репозиторий"),
        "metadata": simple_job("metadata", {}, "пересобрать метаданные всех веток"),
        "cleanup": simple_job("cleanup", {}, "удалить мусорные файлы"),
        "key": simple_job("key", {}, "проверить ключ подписи"),
    }
    try:
        return handlers[a.cmd](a)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
