"""Сведения об устройствах: классификация установленных пакетов, обновления из HomeRepo."""

import functools
import json
import time

from . import config, db, rpmutil


def host_arch(arch):
    return config.ARCH_MAP.get(arch or "", arch or "")


def repo_index(con):
    """name -> список пакетов репозитория (dict), от новых к старым."""
    idx = {}
    for r in con.execute("SELECT id, branch, filename, name, epoch, version, release, arch, arches, sigclass "
                         "FROM packages"):
        idx.setdefault(r["name"], []).append(dict(r))
    for lst in idx.values():
        lst.sort(key=functools.cmp_to_key(_cmp_pkg), reverse=True)
    return idx


def _cmp_pkg(a, b):
    return rpmutil.evr_cmp(a["epoch"], a["version"], a["release"], b["epoch"], b["version"], b["release"])


def evr_of(p):
    return rpmutil.fmt_evr(p["epoch"], p["version"], p["release"])


def compatible(repo_pkg, harch):
    return harch in repo_pkg["arches"].split()


def best_for(idx, name, harch, inst_arch=None, evr=None):
    for p in idx.get(name, []):
        if not compatible(p, harch):
            continue
        if inst_arch and p["arch"] != inst_arch and p["arch"] != "noarch" and inst_arch != "noarch":
            continue
        if evr and evr_of(p) != evr and "%s-%s" % (p["version"], p["release"]) != evr:
            continue
        return p
    return None


def classify(pkg, homerepo_ids, os_repos, os_keys, our_keys):
    fr = (pkg.get("from_repo") or "").strip()
    key = (pkg.get("sigkey") or "").lower()[-16:]
    if fr.lstrip("@").lower().startswith("homerepo") or fr in homerepo_ids:
        return "homerepo"
    if fr in os_repos:
        return "os"
    if key and key in our_keys:
        return "homerepo"
    if fr in ("", "@System", "System", "anaconda", "@anaconda", "installed") and key and key in os_keys:
        return "os"
    if pkg.get("name") in ("gpg-pubkey",):
        return "os"
    return "external"


def updates(idx, harch, pkgs):
    out = []
    for p in pkgs:
        b = best_for(idx, p["name"], harch, p["arch"])
        if not b:
            continue
        if rpmutil.evr_cmp(b["epoch"], b["version"], b["release"], p["epoch"], p["version"], p["release"]) > 0:
            out.append({"name": p["name"], "arch": p["arch"], "installed": evr_of(p), "available": evr_of(b),
                        "branch": b["branch"], "cls": p["cls"], "from_repo": p.get("from_repo") or ""})
    out.sort(key=lambda x: x["name"])
    return out


def host_status(h, interval):
    lp = h.get("last_poll") or 0
    age = time.time() - lp
    if age < max(3 * interval, 360):
        return "online"
    if age < 86400:
        return "late"
    return "offline"


def os_updates_of(h):
    """Обновления ОС из отчёта агента; обновления из самого HomeRepo считаются отдельно (они в «Обновлениях»)."""
    try:
        u = json.loads(h.get("os_updates") or "null")
    except ValueError:
        u = None
    if not u:
        return None
    f = h.get("facts") or "{}"
    try:
        hr = set((f if isinstance(f, dict) else json.loads(f)).get("homerepo_ids") or [])
    except (ValueError, AttributeError):
        hr = set()
    lst = [{"name": x[0], "arch": x[1], "evr": x[2], "repo": x[3], "security": x[4] if len(x) > 4 else ""}
           for x in u.get("list") or [] if isinstance(x, list) and len(x) >= 4]
    sysl = [x for x in lst if x["repo"] not in hr and not x["repo"].startswith("homerepo")]
    return {"checked": u.get("checked"), "error": u.get("error") or "", "reboot": u.get("reboot"),
            "list": sysl, "count": len(sysl), "security": sum(1 for x in sysl if x["security"])}


def host_summary(con, cfg, idx=None):
    idx = idx or repo_index(con)
    hosts = db.rows(con.execute("SELECT * FROM hosts ORDER BY name"))
    pend = {r[0]: r[1] for r in con.execute(
        "SELECT host_id, count(*) FROM tasks WHERE status IN ('pending','sent','processing') GROUP BY host_id")}
    errs = {r[0]: r[1] for r in con.execute(
        "SELECT host_id, count(*) FROM tasks WHERE status='error' AND finished>? GROUP BY host_id",
        (time.time() - 7 * 86400,))}
    for h in hosts:
        harch = host_arch(h["arch"])
        pk = db.rows(con.execute("SELECT name, epoch, version, release, arch, cls, from_repo FROM host_packages "
                                 "WHERE host_id=?", (h["id"],)))
        h["n_homerepo"] = sum(1 for p in pk if p["cls"] == "homerepo")
        h["n_external"] = sum(1 for p in pk if p["cls"] == "external")
        h["n_os"] = sum(1 for p in pk if p["cls"] == "os")
        h["n_updates"] = len(updates(idx, harch, pk)) if pk else None
        h["n_pending"] = pend.get(h["id"], 0)
        h["n_errors"] = errs.get(h["id"], 0)
        h["status"] = host_status(h, cfg.agent_interval)
        ou = os_updates_of(h)
        h["n_os_updates"] = ou["count"] if ou and not ou["error"] else None
        h["n_security"] = ou["security"] if ou and not ou["error"] else None
        h["need_reboot"] = ou["reboot"] if ou else None
        h.pop("os_updates", None)
        h.pop("secret_hash", None)
        h.pop("facts", None)
        try:
            h["metrics"] = json.loads(h.get("metrics") or "null")
        except ValueError:
            h["metrics"] = None
        if h["metrics"]:
            h["metrics"].pop("disks", None)
        try:
            hw = json.loads(h.get("hw") or "{}")
        except ValueError:
            hw = {}
        h["hw"] = {k: hw.get(k) for k in ("model", "virt", "pretty_name", "cores", "mem_total")}
    return hosts
