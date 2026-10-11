#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""HomeRepo agent: сообщает панели, какие пакеты установлены и откуда, и выполняет назначения.

Работает от root по таймеру systemd. Входящих подключений не принимает: сам опрашивает панель
по HTTPS (сертификат панели проверяется по /etc/homerepo-agent/ca.pem).
Совместим с Python 3.6+ и dnf 4 (РЕД ОС 7.3, РЕД ОС 8 и другие системы семейства RHEL).
"""

import configparser
import glob
import json
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

AGENT_VERSION = "1.2.0"
CONF_DIR = "/etc/homerepo-agent"
CONF = os.path.join(CONF_DIR, "agent.json")
CA = os.path.join(CONF_DIR, "ca.pem")
LOCK = "/run/homerepo-agent.lock"
STATE_DIR = "/var/lib/homerepo-agent"

SIG_QF = ("%|DSAHEADER?{%{DSAHEADER:pgpsig}}:{%|RSAHEADER?{%{RSAHEADER:pgpsig}}:"
          "{%|SIGGPG?{%{SIGGPG:pgpsig}}:{%|SIGPGP?{%{SIGPGP:pgpsig}}:{(none)}|}|}|}|")
KEYID_RE = re.compile(r"Key ID ([0-9a-fA-F]{8,40})")


def log(*a):
    msg = "homerepo-agent: " + " ".join(str(x) for x in a) + "\n"
    try:
        sys.stdout.buffer.write(msg.encode("utf-8", "replace"))
        sys.stdout.flush()
    except Exception:  # noqa
        pass


# ---------------------------------------------------------------- HTTP
class Api:
    def __init__(self, server, host_id=None, secret=None, ca=CA):
        self.server = server.rstrip("/")
        self.host_id = host_id
        self.secret = secret
        # доверяем и системному хранилищу (например, CA FreeIPA на клиентах домена),
        # и сертификату панели, полученному при подключении (самоподписанный по умолчанию)
        ctx = ssl.create_default_context()
        if ca and os.path.isfile(ca) and os.path.getsize(ca) > 0:
            try:
                ctx.load_verify_locations(cafile=ca)
            except (ssl.SSLError, OSError):
                pass
        self.ctx = ctx

    def _req(self, method, path, data=None, body=None, length=None, ctype="application/json", timeout=120):
        url = self.server + path
        headers = {"User-Agent": "homerepo-agent/" + AGENT_VERSION}
        if self.host_id:
            headers["Authorization"] = "Bearer %s:%s" % (self.host_id, self.secret)
        if data is not None:
            body = json.dumps(data).encode("utf-8")
        if body is not None:
            headers["Content-Type"] = ctype
            headers["Content-Length"] = str(length if length is not None else len(body))
        rq = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(rq, timeout=timeout, context=self.ctx) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")
            try:
                msg = json.loads(msg).get("error", msg)
            except ValueError:
                pass
            raise RuntimeError("панель ответила %s: %s" % (e.code, msg))
        except urllib.error.URLError as e:
            raise RuntimeError("нет связи с панелью %s: %s" % (self.server, e.reason))
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return raw

    def post(self, path, data):
        return self._req("POST", path, data=data)

    def get_raw(self, path):
        return self._req("GET", path)

    def put_file(self, path, fpath):
        size = os.path.getsize(fpath)
        with open(fpath, "rb") as f:
            return self._req("PUT", path, body=f, length=size, ctype="application/x-rpm", timeout=3600)


def load_conf():
    with open(CONF) as f:
        return json.load(f)


def save_conf(c):
    os.makedirs(CONF_DIR, mode=0o700, exist_ok=True)
    tmp = CONF + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(c, f, indent=1)
    os.replace(tmp, CONF)


# ---------------------------------------------------------------- сведения о системе
def run(cmd, timeout=3600, env=None):
    e = dict(os.environ)
    e.update({"LC_ALL": "C", "LANG": "C"})
    if env:
        e.update(env)
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, env=e)
        return p.returncode, (p.stdout or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired as ex:
        return 124, "превышено время ожидания: %s" % ex
    except OSError as ex:
        return 127, str(ex)


def os_release():
    d = {}
    try:
        with open("/etc/os-release") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k:
                    d[k] = v.strip().strip('"')
    except OSError:
        pass
    return d


def machine_id():
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(p) as f:
                v = f.read().strip()
                if v:
                    return v
        except OSError:
            pass
    return socket.gethostname()


def hostname():
    # имя как есть, без каких-либо доменных суффиксов
    return socket.gethostname().strip()


def keyid(s):
    m = KEYID_RE.search(s or "")
    return m.group(1).lower()[-16:] if m else ""


def rpm_db():
    """(name, arch, epoch, version, release) -> (sigkey, installtime); плюс файл->владелец не нужен."""
    out = {}
    try:
        import rpm  # noqa
        ts = rpm.TransactionSet()
        for h in ts.dbMatch():
            def s(t):
                v = h[t]
                return v.decode("utf-8", "replace") if isinstance(v, bytes) else (v or "")
            k = (s(rpm.RPMTAG_NAME), s(rpm.RPMTAG_ARCH) or "noarch", int(h[rpm.RPMTAG_EPOCH] or 0),
                 s(rpm.RPMTAG_VERSION), s(rpm.RPMTAG_RELEASE))
            sig = h.format(SIG_QF)
            sig = sig.decode() if isinstance(sig, bytes) else sig
            out[k] = (keyid(sig), int(h[rpm.RPMTAG_INSTALLTIME] or 0))
        return out
    except Exception:  # noqa
        pass
    rc, txt = run(["rpm", "-qa", "--qf", "%{NAME}\t%{ARCH}\t%{EPOCHNUM}\t%{VERSION}\t%{RELEASE}\t%{INSTALLTIME}\t"
                   + SIG_QF + "\n"], timeout=600)
    for line in txt.splitlines():
        f = line.split("\t")
        if len(f) < 7:
            continue
        out[(f[0], f[1] or "noarch", int(f[2] or 0), f[3], f[4])] = (keyid(f[6]), int(f[5] or 0))
    return out


def from_repos():
    """(name, arch, epoch, version, release) -> репозиторий, из которого пакет установлен (история dnf)."""
    out = {}
    try:
        import dnf  # noqa
        base = dnf.Base()
        try:
            base.fill_sack(load_system_repo=True, load_available_repos=False)
            for p in base.sack.query().installed():
                try:
                    fr = p.from_repo or ""
                except Exception:  # noqa
                    fr = ""
                out[(p.name, p.arch, int(p.epoch or 0), p.version, p.release)] = fr
        finally:
            base.close()
        return out, "python-dnf"
    except Exception as e:  # noqa
        pass
    rc, txt = run(["dnf", "-q", "repoquery", "--installed", "--qf",
                   "%{name}\t%{arch}\t%{epoch}\t%{version}\t%{release}\t%{from_repo}"], timeout=900)
    if rc == 0:
        for line in txt.splitlines():
            f = line.split("\t")
            if len(f) >= 6:
                out[(f[0], f[1], int(f[2] or 0), f[3], f[4])] = f[5]
        return out, "dnf-repoquery"
    return out, "rpm"


def repos_info():
    repos = []
    files = sorted(glob.glob("/etc/yum.repos.d/*.repo"))
    for fp in files:
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            cp.read(fp, encoding="utf-8")
        except Exception:  # noqa
            continue
        for sec in cp.sections():
            bu = (cp.get(sec, "baseurl", fallback="") or cp.get(sec, "metalink", fallback="")
                  or cp.get(sec, "mirrorlist", fallback=""))
            repos.append({"id": sec, "name": cp.get(sec, "name", fallback=sec), "baseurl": bu.strip().split()[0] if bu.strip() else "",
                          "enabled": cp.get(sec, "enabled", fallback="1").strip() in ("1", "true", "yes", "True"),
                          "file": fp})
    return repos


def os_keys_and_repos(repos, rpmdb):
    """Ключ подписи ОС = ключ пакета, предоставляющего system-release. Репозитории ОС = объявленные в
    файлах, принадлежащих пакетам с этим ключом (так zabbix-release и т. п. не считаются ОС)."""
    keys = set()
    rc, txt = run(["rpm", "-q", "--whatprovides", "system-release", "--qf", SIG_QF + "\n"], timeout=60)
    for line in txt.splitlines():
        k = keyid(line)
        if k:
            keys.add(k)
    os_repos = set()
    files = sorted(set(r["file"] for r in repos))
    if files:
        rc, txt = run(["rpm", "-qf", "--qf", "%{NAME}\t" + SIG_QF + "\n"] + files, timeout=120)
        lines = txt.splitlines()
        if len(lines) == len(files):
            for fp, line in zip(files, lines):
                k = keyid(line)
                if k and k in keys:
                    os_repos.update(r["id"] for r in repos if r["file"] == fp)
    return sorted(keys), sorted(os_repos)


# ---------------------------------------------------------------- метрики и железо
def _read(p, default=""):
    try:
        with open(p, "rb") as f:
            return f.read().decode("utf-8", "replace").replace("\x00", "").strip()
    except OSError:
        return default


def _cpu_times():
    f = _read("/proc/stat").splitlines()[0].split()[1:]
    v = [int(x) for x in f]
    idle = v[3] + (v[4] if len(v) > 4 else 0)
    return sum(v), idle


def cpu_percent():
    """Загрузка CPU в среднем с прошлого запуска агента (раз в ~2 минуты)."""
    try:
        total, idle = _cpu_times()
    except (IndexError, ValueError):
        return None
    sf = os.path.join(STATE_DIR, "cpu.json")
    prev = None
    try:
        with open(sf) as f:
            prev = json.load(f)
    except (OSError, ValueError):
        pass
    if not prev or total <= prev[0] or time.time() - prev[2] > 1800:
        time.sleep(1.0)
        prev = [total, idle, time.time()]
        total, idle = _cpu_times()
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        with open(sf, "w") as f:
            json.dump([total, idle, time.time()], f)
    except OSError:
        pass
    dt, di = total - prev[0], idle - prev[1]
    return round(100.0 * (dt - di) / dt, 1) if dt > 0 else None


def meminfo():
    m = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        try:
            m[k] = int(v.split()[0]) * 1024
        except (IndexError, ValueError):
            pass
    return m


REAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "vfat", "f2fs", "zfs", "jfs", "reiserfs", "nfs", "nfs4", "cifs"}


def disks():
    out, seen = [], set()
    for line in _read("/proc/mounts").splitlines():
        f = line.split()
        if len(f) < 3 or f[2] not in REAL_FS or f[0] in seen:
            continue
        mnt = f[1].replace("\\040", " ")
        if mnt.startswith(("/var/lib/docker", "/var/lib/containers", "/run", "/snap")):
            continue
        try:
            st = os.statvfs(mnt)
        except OSError:
            continue
        size = st.f_blocks * st.f_frsize
        if size < 64 * 1024 * 1024:
            continue
        seen.add(f[0])
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        out.append({"mount": mnt, "dev": f[0], "fs": f[2], "size": size, "used": used,
                    "pct": round(100.0 * used / size, 1) if size else 0})
    return out


def temperature():
    best = None
    for p in glob.glob("/sys/class/thermal/thermal_zone*/temp") + glob.glob("/sys/class/hwmon/hwmon*/temp1_input"):
        try:
            t = int(_read(p, "0")) / 1000.0
        except ValueError:
            continue
        if 0 < t < 150 and (best is None or t > best):
            best = t
    return round(best, 1) if best is not None else None


def metrics():
    m = meminfo()
    tot, avail = m.get("MemTotal", 0), m.get("MemAvailable", m.get("MemFree", 0))
    st, sf = m.get("SwapTotal", 0), m.get("SwapFree", 0)
    try:
        l1, l5, l15 = os.getloadavg()
    except OSError:
        l1 = l5 = l15 = None
    up = 0
    try:
        up = int(float(_read("/proc/uptime", "0").split()[0]))
    except (IndexError, ValueError):
        pass
    dk = disks()
    root = next((d for d in dk if d["mount"] == "/"), dk[0] if dk else None)
    return {
        "ts": int(time.time()), "cpu": cpu_percent(), "cores": os.cpu_count(),
        "load": [round(x, 2) for x in (l1, l5, l15)] if l1 is not None else None,
        "mem_total": tot, "mem_used": tot - avail, "mem_pct": round(100.0 * (tot - avail) / tot, 1) if tot else None,
        "swap_total": st, "swap_used": st - sf, "swap_pct": round(100.0 * (st - sf) / st, 1) if st else None,
        "disk_pct": root["pct"] if root else None, "disks": dk, "temp": temperature(), "uptime": up,
    }


def hw_facts():
    osr = os_release()
    cpu_model = ""
    for line in _read("/proc/cpuinfo").splitlines():
        k, _, v = line.partition(":")
        k = k.strip().lower()
        if k in ("model name", "hardware", "cpu model") and v.strip():
            cpu_model = v.strip()
            break
    model = _read("/sys/firmware/devicetree/base/model")
    vendor = ""
    if not model:
        vendor = _read("/sys/class/dmi/id/sys_vendor")
        prod = _read("/sys/class/dmi/id/product_name")
        model = (vendor + " " + prod).strip()
    if not cpu_model:
        rc, out = run(["lscpu"], timeout=30)
        for line in out.splitlines():
            if line.lower().startswith("model name"):
                cpu_model = line.split(":", 1)[1].strip()
                break
    rc_v, virt = run(["systemd-detect-virt"], timeout=30)
    virt = virt.strip() if rc_v == 0 and virt.strip() not in ("", "none") else ""
    rc_se, se = run(["getenforce"], timeout=30)
    ifaces = []
    rc, out = run(["ip", "-o", "addr", "show"], timeout=30)
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 4 and f[2] in ("inet", "inet6") and f[1] != "lo" and not f[3].startswith("fe80"):
            mac = _read("/sys/class/net/%s/address" % f[1].split("@")[0])
            ifaces.append({"name": f[1], "addr": f[3], "mac": mac})
    m = meminfo()
    boot = None
    try:
        boot = int(time.time() - float(_read("/proc/uptime", "0").split()[0]))
    except (IndexError, ValueError):
        pass
    return {
        "pretty_name": osr.get("PRETTY_NAME", ""), "model": model, "virt": virt, "cpu_model": cpu_model,
        "cores": os.cpu_count(), "mem_total": m.get("MemTotal", 0), "swap_total": m.get("SwapTotal", 0),
        "selinux": se.strip() if rc_se == 0 else "", "interfaces": ifaces, "boot": boot,
        "timezone": time.strftime("%Z"),
    }


def collect_report():
    osr = os_release()
    rpmdb = rpm_db()
    fr, method = from_repos()
    repos = repos_info()
    os_keys, os_repos = os_keys_and_repos(repos, rpmdb)
    homerepo_ids = sorted(r["id"] for r in repos if "/homerepo/" in r["baseurl"] or r["id"].startswith("homerepo"))
    pk = []
    for k, (sig, itime) in rpmdb.items():
        name, arch, epoch, ver, rel = k
        if name == "gpg-pubkey":
            continue
        pk.append([name, epoch, ver, rel, arch, fr.get(k, ""), sig, itime])
    up = 0
    try:
        with open("/proc/uptime") as f:
            up = int(float(f.read().split()[0]))
    except OSError:
        pass
    rc, vl = run(["dnf", "-q", "versionlock", "list"], timeout=120)
    return {
        "hostname": hostname(), "machine_id": machine_id(),
        "os_name": osr.get("NAME", platform.system()), "os_version": osr.get("VERSION_ID", ""),
        "arch": platform.machine(), "kernel": platform.release(), "agent_version": AGENT_VERSION,
        "uptime": up, "python": platform.python_version(), "dnf": method,
        "repos": [{k: r[k] for k in ("id", "name", "baseurl", "enabled")} for r in repos],
        "homerepo_ids": homerepo_ids, "os_repos": os_repos, "os_keys": os_keys,
        "versionlock": [x for x in vl.splitlines() if x.strip()] if rc == 0 else None,
        "hw": hw_facts(), "packages": pk,
    }


# ---------------------------------------------------------------- назначения
def installed(name):
    rc, txt = run(["rpm", "-q", "--qf", "%{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}\n", name], timeout=60)
    return [l.strip() for l in txt.splitlines() if l.strip()] if rc == 0 else []


def spec(t):
    evr = t.get("evr") or ""
    s = t["name"]
    if evr:
        s += "-" + evr
    if t.get("arch") and evr:
        s += "." + t["arch"]
    return s


def short(out, n=4000):
    out = out.strip()
    return out if len(out) <= n else "…" + out[-n:]


def ensure_versionlock():
    rc, _ = run(["dnf", "-q", "versionlock", "list"], timeout=120)
    if rc == 0:
        return True, ""
    for pkg in ("python3-dnf-plugin-versionlock", "dnf-plugin-versionlock", "python3-dnf-plugins-extras-versionlock"):
        rc, out = run(["dnf", "-y", "-q", "install", pkg], timeout=1800)
        if rc == 0:
            return True, "установлен %s\n" % pkg
    return False, "нет плагина dnf versionlock и его не удалось установить"


def do_task(api, t):
    a = t["action"]
    name = t["name"]
    before = installed(name)
    if a == "install":
        target = (t.get("evr") or "")
        same = [x for x in before if target and (":" + target.split(":")[-1] + ".") in x]
        if same and (t.get("params") or {}).get("switch"):
            # та же версия уже стоит из другого источника — переустановить из homerepo
            rc, out = run(["dnf", "-y", "--refresh", "--disablerepo=*", "--enablerepo=homerepo*", "reinstall", spec(t)])
            return ("done" if rc == 0 else "error"), ("переустановлено из homerepo: %s\n" % ", ".join(same) if rc == 0 else "") + short(out)
        rc, out = run(["dnf", "-y", "--refresh", "install", spec(t)])
        after = installed(name)
        hit = [x for x in after if target and (":" + target.split(":")[-1] + ".") in x]
        if rc == 0 and after:
            if after == before:
                return "done", "уже установлено: %s\n%s" % (", ".join(after), short(out, 1500))
            return "done", "установлено: %s\n%s" % (", ".join(after), short(out))
        if hit:
            return "done", "установлено: %s\n%s" % (", ".join(after), short(out))
        return "error", short(out)
    if a == "update":
        if not before:
            return "skipped", "пакет не установлен — обновлять нечего"
        rc, out = run(["dnf", "-y", "--refresh", "upgrade", spec(t)])
        after = installed(name)
        if rc == 0:
            return "done", ("обновлено: %s → %s\n" % (", ".join(before), ", ".join(after)) if after != before
                            else "уже актуально: %s\n" % ", ".join(after)) + short(out, 2000)
        return "error", short(out)
    if a == "remove":
        if not before:
            return "skipped", "пакет не установлен"
        rc, out = run(["dnf", "-y", "remove", name])
        return ("done" if rc == 0 and not installed(name) else "error"), short(out)
    if a in ("hold", "unhold"):
        ok, msg = ensure_versionlock()
        if not ok:
            return "error", msg
        if a == "hold":
            if not before:
                return "skipped", msg + "пакет не установлен — фиксировать нечего"
            run(["dnf", "-q", "versionlock", "delete", name], timeout=300)
            rc, out = run(["dnf", "-q", "versionlock", "add", name], timeout=300)
            return ("done" if rc == 0 else "error"), msg + (out.strip() or "версия зафиксирована: %s" % ", ".join(before))
        rc, out = run(["dnf", "-q", "versionlock", "delete", name], timeout=300)
        return ("done" if rc == 0 else "error"), msg + (out.strip() or "фиксация снята")
    if a == "collect":
        return do_collect(api, t)
    return "error", "неизвестное действие %s" % a


def _rpm_files(d, name):
    files = glob.glob(os.path.join(d, "**", "*.rpm"), recursive=True)
    mine = [f for f in files if os.path.basename(f).startswith(name + "-")]
    return mine or files


def _rpm_nevra(path):
    rc, out = run(["rpm", "-qp", "--nosignature", "--qf", "%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}", path],
                  timeout=60)
    return out.strip().split("\t") if rc == 0 else ["", ""]


def do_collect(api, t):
    """Забрать установленный пакет в HomeRepo: скачать его из того же источника и отправить в панель.
    Если точной версии у источника уже нет (сторонние репозитории часто хранят только последнюю),
    берётся последняя версия из того же репозитория."""
    name, arch = t["name"], t.get("arch") or ""
    params = t.get("params") or {}
    src_repo = params.get("from_repo") or ""
    exact = spec(t)
    if src_repo in ("@commandline",):
        return "error", ("%s установлен вручную из файла .rpm — скачать его неоткуда. Загрузите этот файл в панели "
                         "(раздел «Подпись»), и он появится в HomeRepo." % exact)
    have_dl = run(["dnf", "download", "--help"], timeout=120)[0] == 0
    log_out = []

    def note(msg):
        try:
            api.post("/api/agent/result", {"task_id": t["id"], "status": "running", "output": msg})
        except Exception:  # noqa
            pass

    tmp = tempfile.mkdtemp(prefix="homerepo-collect-")
    try:
        note("скачиваю %s из источника…" % exact)
        if have_dl:
            rc, out = run(["dnf", "-q", "download", "--destdir", tmp, exact], timeout=3600)
        else:
            rc, out = run(["dnf", "-y", "-q", "reinstall", "--downloadonly", "--downloaddir", tmp, exact], timeout=3600)
        log_out.append(out)
        files = _rpm_files(tmp, name)
        took = "точная версия"
        if not files:
            want = "%s-%s.%s.rpm" % (name, (t.get("evr") or "").split(":")[-1], arch)
            for p in glob.glob("/var/cache/dnf/**/" + want, recursive=True) + \
                    glob.glob("/var/cache/yum/**/" + want, recursive=True):
                shutil.copy(p, tmp)
                files = _rpm_files(tmp, name)
                took = "из кэша dnf"
                break
        if not files:
            # точной версии нет — последняя из того же репозитория (или из любого подключённого)
            target = "%s.%s" % (name, arch) if arch else name
            repo_opts = (["--disablerepo=*", "--enablerepo=" + src_repo]
                         if src_repo and src_repo not in ("", "@System", "anaconda", "System") else [])
            note("точной версии у источника нет, ищу последнюю%s…" % (" в " + src_repo if repo_opts else ""))
            if have_dl:
                rc, out = run(["dnf", "-q", "download", "--destdir", tmp] + repo_opts + [target], timeout=3600)
            else:
                rc, out = run(["dnf", "-y", "-q", "upgrade", "--downloadonly", "--downloaddir", tmp] + repo_opts + [target],
                              timeout=3600)
            log_out.append(out)
            files = _rpm_files(tmp, name)
            took = "последняя версия у источника"
        if not files:
            why = ("репозиторий %s больше не отдаёт этот пакет" % src_repo) if src_repo else "источник пакета неизвестен"
            return "error", ("не удалось получить %s: %s. Если у вас есть файл .rpm — загрузите его в разделе «Подпись».\n%s"
                             % (exact, why, short("\n".join(x for x in log_out if x.strip()), 1500)))
        f = files[0]
        got = _rpm_nevra(f)[1]
        size = os.path.getsize(f)
        note("отправляю в панель %s (%.1f МБ, %s)…" % (os.path.basename(f), size / 1048576.0, took))
        r = api.put_file("/api/agent/upload/%d?got=%s" % (t["id"], urllib.parse.quote(got or "")), f)
        log("  передан в панель: %s, задание службы подписи #%s" % (os.path.basename(f), (r or {}).get("job")))
        return None, None  # дальше назначение ведёт панель и служба подписи
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- самообновление
def self_update(api, want):
    if not want or want == AGENT_VERSION:
        return False
    try:
        src = api.get_raw("/agent/homerepo-agent.py")
        if isinstance(src, dict):
            return False
        if b"AGENT_VERSION" not in src:
            return False
        compile(src, "homerepo-agent.py", "exec")
        me = os.path.realpath(__file__)
        tmp = me + ".new"
        with open(tmp, "wb") as f:
            f.write(src)
        os.chmod(tmp, 0o755)
        os.replace(tmp, me)
        log("агент обновлён: %s → %s" % (AGENT_VERSION, want))
        return True
    except Exception as e:  # noqa
        log("не удалось обновить агента:", e)
        return False


# ---------------------------------------------------------------- команды
def cmd_enroll(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="homerepo-agent enroll")
    ap.add_argument("--server", required=True)
    ap.add_argument("--token", required=True)
    a = ap.parse_args(argv)
    api = Api(a.server)
    r = api.post("/api/agent/enroll", {"token": a.token, "hostname": hostname(), "machine_id": machine_id(),
                                       "arch": platform.machine()})
    save_conf({"server": a.server, "host_id": r["host_id"], "secret": r["secret"]})
    log("хост зарегистрирован, id %s" % r["host_id"])
    return 0


def cmd_run(argv, force_report=False):
    import fcntl
    lf = open(LOCK, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("уже выполняется другой экземпляр")
        return 0
    c = load_conf()
    api = Api(c["server"], c["host_id"], c["secret"])
    try:
        met = metrics()
    except Exception as e:  # noqa
        log("метрики:", e)
        met = None
    r = api.post("/api/agent/poll", {"agent_version": AGENT_VERSION, "metrics": met})
    if self_update(api, r.get("agent_version")):
        lf.close()
        os.execv(sys.executable, [sys.executable, os.path.realpath(__file__)] + sys.argv[1:])
    tasks = r.get("tasks") or []
    for t in tasks:
        log("назначение #%d: %s %s" % (t["id"], t["action"], spec(t)))
        try:
            api.post("/api/agent/result", {"task_id": t["id"], "status": "running", "output": "выполняется…"})
            st, out = do_task(api, t)
        except Exception as e:  # noqa
            st, out = "error", "ошибка агента: %s" % e
        if st:
            api.post("/api/agent/result", {"task_id": t["id"], "status": st, "output": out})
            log("  → %s" % st)
    if tasks or r.get("report") or force_report:
        rep = collect_report()
        res = api.post("/api/agent/report", rep)
        log("отчёт отправлен: %s пакетов" % res.get("packages"))
    return 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("использование: homerepo-agent.py enroll --server URL --token T | run | report | version")
        return 0
    cmd, argv = sys.argv[1], sys.argv[2:]
    if cmd == "version":
        print(AGENT_VERSION)
        return 0
    if os.geteuid() != 0:
        print("нужны права root", file=sys.stderr)
        return 1
    try:
        if cmd == "enroll":
            return cmd_enroll(argv)
        if cmd == "run":
            return cmd_run(argv)
        if cmd == "report":
            return cmd_run(argv, force_report=True)
    except RuntimeError as e:
        log(str(e))
        return 1
    print("неизвестная команда", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
