"""Сканер сети и установка агента по SSH.

Сканирование работает без root и без nmap: TCP-подключения к типовым портам (ответ «порт закрыт»
тоже значит, что узел жив), таблица ARP ядра для MAC-адресов, обратный DNS и баннер SSH.
Установка агента — через системный ssh-клиент; пароль не записывается ни в базу, ни на диск,
а живёт только в памяти процесса панели на время задания.
"""

import concurrent.futures
import errno
import ipaddress
import json
import os
import re
import shlex
import socket
import subprocess
import threading
import time

from . import auth, db, inventory
from . import web
from .web import HTTPError, jresp, route

QUICK_PORTS = [22, 80, 443, 445, 3389, 8080, 9090]
FULL_PORTS = [21, 22, 23, 53, 80, 88, 135, 139, 443, 445, 548, 631, 1883, 3000, 3306, 3389, 5000, 5432, 5900,
              5985, 6443, 8006, 8080, 8291, 8443, 8484, 8585, 9000, 9090, 9100, 9200, 62078]
PORT_NAMES = {21: "ftp", 22: "ssh", 23: "telnet", 53: "dns", 80: "http", 88: "kerberos", 135: "msrpc", 139: "netbios",
              443: "https", 445: "smb", 548: "afp", 631: "ipp", 1883: "mqtt", 3000: "web", 3306: "mysql",
              3389: "rdp", 8291: "winbox", 5000: "web", 5432: "postgres", 5900: "vnc", 5985: "winrm", 6443: "k8s", 8006: "proxmox",
              8080: "http-alt", 8443: "https-alt", 8484: "logatlas?", 8585: "zaslon?", 9000: "web", 9090: "cockpit",
              9100: "jetdirect/node-exp", 9200: "opensearch", 62078: "apple-sync"}
MAX_HOSTS = 1024

# немного распространённых OUI (первые три байта MAC)
OUI = {
    "b8:27:eb": "Raspberry Pi", "dc:a6:32": "Raspberry Pi", "e4:5f:01": "Raspberry Pi", "2c:cf:67": "Raspberry Pi",
    "d8:3a:dd": "Raspberry Pi", "28:cd:c1": "Raspberry Pi", "00:50:56": "VMware", "00:0c:29": "VMware",
    "00:05:69": "VMware", "08:00:27": "VirtualBox", "52:54:00": "QEMU/KVM", "bc:24:11": "Proxmox",
    "00:15:5d": "Hyper-V", "02:42:ac": "Docker", "00:1c:42": "Parallels", "f0:18:98": "Apple", "a4:83:e7": "Apple",
    "3c:22:fb": "Apple", "ac:de:48": "Apple", "00:11:32": "Synology", "00:08:9b": "QNAP", "b0:be:76": "TP-Link",
    "50:c7:bf": "TP-Link", "14:cc:20": "TP-Link", "00:1d:0f": "TP-Link", "d8:07:b6": "TP-Link", "c4:6e:1f": "TP-Link",
    "e8:48:b8": "TP-Link", "04:d9:f5": "ASUS", "2c:fd:a1": "ASUS", "00:e0:4c": "Realtek", "b4:2e:99": "Gigabyte",
    "00:1b:21": "Intel", "3c:fd:fe": "Intel", "a0:36:9f": "Intel", "f4:f2:6d": "TP-Link", "ec:08:6b": "TP-Link",
    "64:66:b3": "TP-Link", "00:17:88": "Philips Hue", "18:b4:30": "Nest", "f4:f5:d8": "Google", "44:07:0b": "Google",
    "fc:ec:da": "Ubiquiti", "24:5a:4c": "Ubiquiti", "78:8a:20": "Ubiquiti", "4c:5e:0c": "MikroTik", "48:8f:5a": "MikroTik",
    "e4:8d:8c": "MikroTik", "00:0c:42": "MikroTik", "d4:ca:6d": "MikroTik", "30:b5:c2": "TP-Link", "9c:5c:8e": "ASUS",
    "00:90:a9": "Western Digital", "00:14:ee": "Western Digital", "3c:7c:3f": "ASUS", "70:85:c2": "ASRock",
    "a8:a1:59": "ASRock", "00:d8:61": "Micro-Star", "d8:bb:c1": "Micro-Star", "00:26:ab": "Seiko Epson",
    "64:eb:8c": "Seiko Epson", "00:80:77": "Brother", "30:05:5c": "Brother", "3c:2a:f4": "Brother", "00:1e:0b": "HP",
    "9c:8e:99": "HP", "a0:d3:c1": "HP", "f4:39:09": "HP", "e8:9a:8f": "Quanta", "84:2b:2b": "Dell", "f8:bc:12": "Dell",
    "d0:94:66": "Dell", "18:66:da": "Dell", "54:bf:64": "Dell", "28:16:ad": "Intel", "40:a6:b7": "Intel",
    "34:13:e8": "Intel", "b8:ae:ed": "Elitegroup", "00:23:24": "G-PRO", "c8:3a:35": "Tenda", "74:da:88": "TP-Link",
    "28:ee:52": "TP-Link", "cc:2d:e0": "Routerboard", "b8:69:f4": "Routerboard", "dc:2c:6e": "Routerboard",
}

SECRETS = {}  # job_id -> {"password": ..., "sudo": ...}; только в памяти
_lock = threading.Lock()


def cfg():
    return web.APP.cfg


# ------------------------------------------------------------------ сканирование
def probe_port(ip, port, timeout):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        rc = s.connect_ex((ip, port))
        if rc == 0:
            return "open"
        if rc == errno.ECONNREFUSED:
            return "closed"
        return None
    except OSError:
        return None
    finally:
        s.close()


def ssh_banner(ip, port=22, timeout=2.0):
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.settimeout(timeout)
            data = s.recv(256)
            line = data.split(b"\n")[0].decode("utf-8", "replace").strip()
            return line[:120] if line.startswith("SSH-") else ""
    except OSError:
        return ""


def rdns(ip):
    try:
        return socket.gethostbyaddr(ip)[0]
    except (OSError, UnicodeError):
        return ""


def arp_table():
    out = {}
    try:
        with open("/proc/net/arp") as f:
            next(f)
            for line in f:
                p = line.split()
                if len(p) >= 4 and p[2] != "0x0" and p[3] != "00:00:00:00:00:00":
                    out[p[0]] = p[3].lower()
    except OSError:
        pass
    return out


FAMILIES = ("linux", "windows", "macos", "router", "nas", "hypervisor", "printer", "phone", "iot", "other")


def guess_os(ports, banner, vendor):
    """Предположение о системе по открытым портам, приветствию SSH и производителю сетевой карты.
    Возвращает (семейство, название). Приветствие SSH надёжнее портов: RDP (3389) бывает и на Linux (xrdp)."""
    pset = set(ports)
    b = (banner or "").lower()
    v = (vendor or "").lower()
    if b:
        if "windows" in b:
            return "windows", "Windows"
        if "dropbear" in b:
            return "router" if 53 in pset or 8291 in pset else "linux", "Linux (встраиваемая, dropbear)"
        if "rosssh" in b or "mikrotik" in v or "routerboard" in v or 8291 in pset:
            return "router", "MikroTik RouterOS"
        for key, name in (("ubuntu", "Ubuntu"), ("debian", "Debian"), ("raspbian", "Raspberry Pi OS"),
                          ("freebsd", "FreeBSD"), ("openbsd", "OpenBSD"), ("netbsd", "NetBSD")):
            if key in b:
                return ("linux" if key in ("ubuntu", "debian", "raspbian") else "other"), name
        if 8006 in pset:
            return "hypervisor", "Proxmox VE"
        if 548 in pset or "apple" in v:
            return "macos", "macOS"
        if "synology" in v:
            return "nas", "Synology DSM"
        return "linux", "Linux" + (" (Raspberry Pi)" if "raspberry" in v else "")
    if 8291 in pset or "mikrotik" in v or "routerboard" in v:
        return "router", "MikroTik RouterOS"
    if 62078 in pset:
        return "phone", "iOS / iPadOS"
    if 548 in pset or ("apple" in v and 88 in pset):
        return "macos", "macOS"
    if {135, 445}.issubset(pset) or 5985 in pset or (3389 in pset and 22 not in pset):
        return "windows", "Windows"
    if 8006 in pset:
        return "hypervisor", "Proxmox VE"
    if "synology" in v or "qnap" in v:
        return "nas", vendor
    if (631 in pset or 9100 in pset) and not pset & {22, 80, 8080, 9090}:
        return "printer", "принтер"
    if 53 in pset and (80 in pset or 443 in pset):
        return "router", "роутер"
    if 9090 in pset:
        return "linux", "Linux (Cockpit)"
    if "raspberry" in v:
        return "linux", "Linux (Raspberry Pi)"
    return "", ""


def parse_net(cidr):
    try:
        net = ipaddress.ip_network(cidr.strip(), strict=False)
    except ValueError:
        raise HTTPError(400, "неверная сеть: укажите вида 192.168.10.0/24")
    if net.version != 4:
        raise HTTPError(400, "поддерживаются только сети IPv4")
    if not (net.is_private or net.is_link_local):
        raise HTTPError(400, "сканировать можно только локальные (частные) сети")
    if net.num_addresses > MAX_HOSTS + 2:
        raise HTTPError(400, "сеть слишком большая: не больше /22 (%d адресов)" % MAX_HOSTS)
    return net


def do_scan(con, j):
    p = json.loads(j["params"])
    net = ipaddress.ip_network(p["cidr"], strict=False)
    ports = FULL_PORTS if p.get("ports") == "full" else QUICK_PORTS
    hosts = [str(h) for h in (net.hosts() if net.num_addresses > 2 else net)]
    timeout = 0.8
    found = {}
    done = [0]
    t0 = time.time()

    def scan_host(ip):
        res = {}
        alive = False
        for port in ports:
            st = probe_port(ip, port, timeout)
            if st:
                alive = True
                if st == "open":
                    res[port] = True
        return ip, alive, sorted(res)

    with concurrent.futures.ThreadPoolExecutor(max_workers=96) as ex:
        for ip, alive, open_ports in ex.map(scan_host, hosts):
            done[0] += 1
            if alive:
                found[ip] = {"ports": open_ports}
            if done[0] % 16 == 0:
                st = con.execute("SELECT status FROM jobs WHERE id=?", (j["id"],)).fetchone()
                if st and st["status"] == "cancelled":
                    return
                con.execute("UPDATE jobs SET progress=?, message=? WHERE id=?",
                            (done[0] / len(hosts), "проверено %d из %d, найдено %d" % (done[0], len(hosts), len(found)),
                             j["id"]))
    arp = arp_table()
    for ip, mac in arp.items():
        try:
            if ipaddress.ip_address(ip) in net and ip not in found:
                found[ip] = {"ports": []}
        except ValueError:
            pass
    con.execute("UPDATE jobs SET message=? WHERE id=?", ("найдено %d, определяю имена…" % len(found), j["id"]))

    def enrich(ip):
        info = found[ip]
        info["hostname"] = rdns(ip)
        info["banner"] = ssh_banner(ip) if 22 in info["ports"] else ""
        return ip, info

    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as ex:
        list(ex.map(enrich, list(found)))
    now = time.time()
    with db.tx(con):
        for ip, info in found.items():
            mac = arp.get(ip, "")
            vendor = OUI.get(mac[:8], "") if mac else ""
            fam, og = guess_os(info["ports"], info.get("banner", ""), vendor)
            ports_js = json.dumps(info["ports"])
            old = con.execute("SELECT ip FROM discovered WHERE ip=?", (ip,)).fetchone()
            if old:
                con.execute("UPDATE discovered SET mac=COALESCE(NULLIF(?,''), mac), vendor=COALESCE(NULLIF(?,''), vendor), "
                            "hostname=COALESCE(NULLIF(?,''), hostname), ports=?, banner=?, os_guess=?, os_family=?, last_seen=?, "
                            "alive=1 WHERE ip=?", (mac, vendor, info.get("hostname", ""), ports_js, info.get("banner", ""),
                                                   og, fam, now, ip))
            else:
                con.execute("INSERT INTO discovered(ip, mac, vendor, hostname, ports, banner, os_guess, os_family, "
                            "first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (ip, mac, vendor, info.get("hostname", ""), ports_js, info.get("banner", ""), og, fam, now, now))
        # кто был в этой сети раньше, но сейчас не ответил
        for r in con.execute("SELECT ip FROM discovered").fetchall():
            try:
                if ipaddress.ip_address(r["ip"]) in net and r["ip"] not in found:
                    con.execute("UPDATE discovered SET alive=0 WHERE ip=?", (r["ip"],))
            except ValueError:
                pass
    con.execute("UPDATE jobs SET status='done', progress=NULL, finished=?, message=? WHERE id=?",
                (time.time(), "%s: найдено устройств %d за %d с" % (net, len(found), time.time() - t0), j["id"]))


# ------------------------------------------------------------------ SSH
def ssh_dir():
    d = os.path.join(cfg().state_dir, ".ssh")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def panel_key():
    k = os.path.join(ssh_dir(), "id_ed25519")
    return k if os.path.exists(k) else None


def panel_pubkey():
    k = panel_key()
    if not k or not os.path.exists(k + ".pub"):
        return ""
    with open(k + ".pub") as f:
        return f.read().strip()


def askpass_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "askpass.sh")


def ssh_command(ip, port, user, auth_mode):
    kh = os.path.join(ssh_dir(), "known_hosts")
    cmd = ["ssh", "-p", str(int(port)), "-o", "StrictHostKeyChecking=accept-new", "-o", "UserKnownHostsFile=" + kh,
           "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=8",
           "-o", "NumberOfPasswordPrompts=1", "-o", "LogLevel=ERROR"]
    if auth_mode == "key":
        cmd += ["-i", panel_key() or "/nonexistent", "-o", "BatchMode=yes", "-o", "PreferredAuthentications=publickey",
                "-o", "IdentitiesOnly=yes"]
    else:
        cmd += ["-o", "PreferredAuthentications=password,keyboard-interactive", "-o", "PubkeyAuthentication=no"]
    cmd += ["%s@%s" % (user, ip)]
    return cmd


def remote_script(user, token, have_sudo_pw):
    url = cfg().repo_url + "/setup.sh"
    inner = "curl -fsS %s | bash -s -- --token %s" % (shlex.quote(url), shlex.quote(token))
    if user == "root":
        return inner
    # пароль sudo приходит первой строкой stdin и в командную строку не попадает
    return "sudo %s -p '' bash -c %s" % ("-S" if have_sudo_pw else "-n", shlex.quote(inner))


def do_install(con, j):
    p = json.loads(j["params"])
    sec = SECRETS.pop(j["id"], {})
    if p.get("auth") != "key" and not sec.get("password"):
        con.execute("UPDATE jobs SET status='error', finished=?, message=? WHERE id=?",
                    (time.time(), "пароль не сохранился (панель перезапускалась) — запустите установку ещё раз", j["id"]))
        return
    tok = auth.enroll_token()
    now = time.time()
    con.execute("INSERT INTO tokens(hash, note, created_by, created, expires) VALUES(?,?,?,?,?)",
                (auth.sha256(auth.norm_enroll(tok)), "SSH %s" % p["ip"], j["created_by"], now, now + 3600))
    sudo_pw = sec.get("sudo") or (sec.get("password") if p.get("auth") != "key" else "")
    cmd = ssh_command(p["ip"], p.get("port", 22), p["user"], p.get("auth"))
    cmd.append(remote_script(p["user"], tok, bool(sudo_pw)))
    env = dict(os.environ)
    env.update({"LC_ALL": "C", "DISPLAY": ":0", "SSH_ASKPASS": askpass_path(), "SSH_ASKPASS_REQUIRE": "force",
                "HR_SSH_PASS": sec.get("password", "")})
    con.execute("UPDATE jobs SET step='run', message=? WHERE id=?",
                ("подключение к %s@%s…" % (p["user"], p["ip"]), j["id"]))
    try:
        pr = subprocess.run(cmd, input=((sudo_pw + "\n") if sudo_pw and p["user"] != "root" else "").encode(),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, timeout=1800,
                            start_new_session=True)
        out = pr.stdout.decode("utf-8", "replace")
        rc = pr.returncode
    except subprocess.TimeoutExpired:
        out, rc = "превышено время ожидания (30 мин)", 124
    except FileNotFoundError:
        out, rc = "на сервере нет ssh-клиента (dnf install openssh-clients)", 127
    finally:
        sec.clear()
    out = re.sub(r"\x1b\[[0-9;]*m", "", out)[-8000:]
    if rc == 0:
        msg = "агент установлен — устройство появится в разделе «Устройства»"
        st = "done"
    else:
        hint = ""
        low = out.lower()
        if "permission denied" in low:
            hint = "неверный логин/пароль или ключ панели не добавлен на хост"
        elif "a password is required" in low or "sudo:" in low and "password" in low:
            hint = "sudo требует пароль — укажите пароль sudo"
        elif "must have a tty" in low:
            hint = "в sudoers включён requiretty — отключите его для этого пользователя или используйте root"
        elif "not in the sudoers" in low:
            hint = "у пользователя нет прав sudo"
        elif "connection refused" in low or "timed out" in low or "no route" in low:
            hint = "SSH недоступен (порт закрыт или хост выключен)"
        elif "host key verification failed" in low or "remote host identification has changed" in low:
            hint = "ключ хоста изменился — удалите его из /var/lib/homerepo/.ssh/known_hosts"
        elif "curl" in low and ("couldn't connect" in low or "resolve" in low):
            hint = "хост не может достучаться до репозитория %s" % cfg().repo_url
        msg = "ошибка (код %d)%s" % (rc, ": " + hint if hint else "")
        st = "error"
    con.execute("UPDATE jobs SET status=?, finished=?, message=?, log=? WHERE id=?",
                (st, time.time(), msg, out, j["id"]))
    db.audit(con, j["created_by"], "", "ssh_install", "%s@%s: %s" % (p["user"], p["ip"], msg))


# ------------------------------------------------------------------ API
def local_networks():
    nets = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))
        ip = s.getsockname()[0]
        s.close()
        nets.append(str(ipaddress.ip_network(ip + "/24", strict=False)))
    except OSError:
        pass
    m = re.match(r"^[a-z]+://([0-9.]+)", cfg().repo_url)
    if m:
        n = str(ipaddress.ip_network(m.group(1) + "/24", strict=False))
        if n not in nets:
            nets.insert(0, n)
    return nets


@route("GET", "/api/scanner")
def api_scanner(req):
    con = web.C()
    disc = db.rows(con.execute("SELECT * FROM discovered ORDER BY last_seen DESC"))
    hosts = db.rows(con.execute("SELECT id, name, ip, hw, os_name, os_version FROM hosts"))
    by_ip, by_mac = {}, {}
    for h in hosts:
        if h["ip"]:
            by_ip[h["ip"]] = h
        try:
            for i in json.loads(h["hw"] or "{}").get("interfaces", []):
                if i.get("mac"):
                    by_mac[i["mac"].lower()] = h
                ip = (i.get("addr") or "").split("/")[0]
                if ip:
                    by_ip.setdefault(ip, h)
        except ValueError:
            pass
    for d in disc:
        d["ports"] = json.loads(d["ports"] or "[]")
        d["port_names"] = [PORT_NAMES.get(p, str(p)) for p in d["ports"]]
        h = by_ip.get(d["ip"]) or (by_mac.get(d["mac"]) if d["mac"] else None)
        d["host_id"] = h["id"] if h else None
        d["host_name"] = h["name"] if h else None
        # что показывать: указано вручную > сообщил агент > автоопределение
        if d.get("manual_family"):
            d["family"], d["osname"], d["os_src"] = d["manual_family"], d.get("manual_name") or "", "manual"
        elif h and h.get("os_name"):
            try:
                pn = json.loads(h["hw"] or "{}").get("pretty_name")
            except ValueError:
                pn = ""
            d["family"], d["osname"], d["os_src"] = "linux", pn or ("%s %s" % (h["os_name"], h["os_version"] or "")).strip(), "agent"
        else:
            if d.get("os_family") is None:  # старые записи без типа — пересчитываем по сохранённым данным
                d["os_family"], d["os_guess"] = guess_os(d["ports"], d.get("banner") or "", d.get("vendor") or "")
            d["family"], d["osname"], d["os_src"] = d.get("os_family") or "", d.get("os_guess") or "", "auto"
        d["sort"] = tuple(int(x) for x in d["ip"].split("."))
    disc.sort(key=lambda x: x.pop("sort"))
    jobs = db.rows(con.execute("SELECT id, op, title, status, step, progress, message, log, created_by, created, "
                               "finished FROM jobs WHERE op IN ('scan','ssh_install') ORDER BY id DESC LIMIT 30"))
    return jresp({"networks": local_networks(), "discovered": disc, "jobs": jobs, "pubkey": panel_pubkey(),
                  "quick_ports": QUICK_PORTS, "full_ports": FULL_PORTS, "repo_url": cfg().repo_url})


@route("POST", "/api/scanner/scan", perm="admin")
def api_scan(req):
    con = web.C()
    d = req.json()
    net = parse_net(str(d.get("cidr", "")))
    if con.execute("SELECT 1 FROM jobs WHERE op='scan' AND status IN ('queued','running')").fetchone():
        raise HTTPError(409, "сканирование уже идёт")
    mode = "full" if d.get("ports") == "full" else "quick"
    jid = db.add_job(con, "scan", {"cidr": str(net), "ports": mode},
                     "сканирование %s (%s)" % (net, "расширенное" if mode == "full" else "быстрое"), req.uname,
                     worker="web")
    web.audit(req, "scan", "%s %s" % (net, mode))
    return jresp({"job": jid})


USER_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_.@-]{0,63}$")


@route("POST", "/api/scanner/install", perm="admin")
def api_install(req):
    con = web.C()
    d = req.json()
    ips = d.get("ips") or []
    user = str(d.get("user", "")).strip()
    mode = "key" if d.get("auth") == "key" else "password"
    port = int(d.get("port") or 22)
    if not ips or not isinstance(ips, list):
        raise HTTPError(400, "не выбраны устройства")
    if not USER_RE.match(user):
        raise HTTPError(400, "неверное имя пользователя SSH")
    if not 0 < port < 65536:
        raise HTTPError(400, "неверный порт")
    if mode == "password" and not d.get("password"):
        raise HTTPError(400, "укажите пароль")
    if mode == "key" and not panel_key():
        raise HTTPError(400, "ключ панели ещё не создан")
    out = []
    for ip in ips[:64]:
        try:
            ipaddress.ip_address(str(ip))
        except ValueError:
            raise HTTPError(400, "неверный адрес %s" % ip)
        jid = db.add_job(con, "ssh_install", {"ip": str(ip), "user": user, "port": port, "auth": mode},
                         "агент на %s@%s" % (user, ip), req.uname, worker="web")
        SECRETS[jid] = {"password": str(d.get("password") or ""), "sudo": str(d.get("sudo_password") or "")}
        out.append(jid)
    web.audit(req, "ssh_install_start", "%s@%s (%s)" % (user, ", ".join(map(str, ips[:64])), mode))
    return jresp({"jobs": out})


@route("POST", "/api/scanner/key", perm="admin")
def api_key_gen(req):
    if panel_key():
        return jresp({"pubkey": panel_pubkey()})
    k = os.path.join(ssh_dir(), "id_ed25519")
    try:
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "homerepo-panel", "-f", k],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    except (OSError, subprocess.CalledProcessError) as e:
        raise HTTPError(500, "не удалось создать ключ: %s" % e)
    web.audit(req, "ssh_key_create", "")
    return jresp({"pubkey": panel_pubkey()})


@route("PUT", r"/api/scanner/([0-9.]+)/os", perm="operator")
def api_disc_os(req, ip):
    con = web.C()
    d = req.json()
    if d.get("reset"):
        con.execute("UPDATE discovered SET manual_family=NULL, manual_name=NULL WHERE ip=?", (ip,))
        web.audit(req, "device_os", "%s: автоопределение" % ip)
        return jresp({"ok": True})
    fam = str(d.get("family") or "")
    if fam not in FAMILIES:
        raise HTTPError(400, "неизвестный тип системы")
    name = re.sub(r"[\x00-\x1f]", "", str(d.get("name") or "")).strip()[:80]
    if not con.execute("SELECT 1 FROM discovered WHERE ip=?", (ip,)).fetchone():
        raise HTTPError(404, "устройство не найдено")
    con.execute("UPDATE discovered SET manual_family=?, manual_name=? WHERE ip=?", (fam, name, ip))
    web.audit(req, "device_os", "%s: %s %s" % (ip, fam, name))
    return jresp({"ok": True})


@route("DELETE", r"/api/scanner/([0-9.]+)", perm="admin")
def api_disc_del(req, ip):
    web.C().execute("DELETE FROM discovered WHERE ip=?", (ip,))
    return jresp({"ok": True})


@route("DELETE", "/api/scanner", perm="admin")
def api_disc_clear(req):
    web.C().execute("DELETE FROM discovered")
    web.audit(req, "scan_clear", "")
    return jresp({"ok": True})
