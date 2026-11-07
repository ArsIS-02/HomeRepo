"""Отправка журнала действий и событий сканера в Graylog по GELF (UDP или TCP). Только стандартная библиотека.

Включается секцией [graylog] в homerepo.conf:

    [graylog]
    gelf_host = 192.0.2.10     ; пусто — отправка выключена
    gelf_port = 12201
    gelf_proto = udp           ; или tcp
    source = homerepo

Сообщения уходят из фонового потока через очередь: недоступный Graylog не задерживает панель,
исполнитель и подписыватель, а журнал в базе остаётся полным в любом случае.
"""
import json
import logging
import os
import queue
import socket
import struct
import threading
import time
import zlib

log = logging.getLogger("homerepo.gelf")

CHUNK = 8000          # полезная нагрузка UDP-части, с запасом под MTU и заголовок 12 байт
MAX_CHUNKS = 128      # предел GELF
QUEUE_MAX = 1000


def udp_packets(payload, chunk=CHUNK):
    """Готовые UDP-датаграммы: одна, если влезает, иначе части с заголовком 0x1e 0x0f."""
    data = zlib.compress(payload)
    if len(data) <= chunk:
        return [data]
    parts = [data[i:i + chunk] for i in range(0, len(data), chunk)]
    if len(parts) > MAX_CHUNKS:
        raise ValueError("GELF: сообщение слишком большое (%d частей)" % len(parts))
    mid = os.urandom(8)
    return [b"\x1e\x0f" + mid + struct.pack("BB", i, len(parts)) + p for i, p in enumerate(parts)]


def message(short, fields, source, level=6, ts=None):
    """Словарь GELF 1.1: свои поля получают префикс «_», пустые значения не отправляются."""
    msg = {"version": "1.1", "host": source, "short_message": str(short)[:1000] or "-",
           "timestamp": round(ts if ts is not None else time.time(), 3), "level": int(level)}
    for k, v in fields.items():
        if v is None or v == "" or v == [] or v == {}:
            continue
        if isinstance(v, bool):
            v = "true" if v else "false"
        elif isinstance(v, (list, tuple, set)):
            v = ", ".join(str(x) for x in v)
        elif isinstance(v, dict):
            v = json.dumps(v, ensure_ascii=False, sort_keys=True)
        key = "_" + "".join(c if c.isalnum() or c == "_" else "_" for c in str(k))
        if key in ("_id", "_source"):
            key = "_f" + key
        msg[key] = v
    return msg


class Sender:
    def __init__(self, host, port=12201, proto="udp", source="homerepo", timeout=2.0):
        self.host, self.port = host, int(port)
        self.proto = (proto or "udp").lower()
        self.source = source or "homerepo"
        self.timeout = timeout
        self.status = {"sent": 0, "errors": 0, "last_ok": None, "last_error": ""}
        self._q = queue.Queue(QUEUE_MAX)
        self._t = None

    @classmethod
    def from_config(cls, cfg):
        if not getattr(cfg, "gelf_host", ""):
            return None
        return cls(cfg.gelf_host, cfg.gelf_port, cfg.gelf_proto, cfg.gelf_source)

    def send_now(self, msg):
        """Отправить сразу, в вызывающем потоке. Ошибки сети поднимаются наверх."""
        raw = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        if self.proto == "tcp":
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
                s.sendall(raw + b"\0")
        else:
            infos = socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_DGRAM)
            fam, _, _, _, addr = infos[0]
            with socket.socket(fam, socket.SOCK_DGRAM) as s:
                for p in udp_packets(raw):
                    s.sendto(p, addr)

    def submit(self, short, fields, level=6, ts=None):
        """Поставить сообщение в очередь фонового потока. Никогда не бросает исключений."""
        try:
            self._q.put_nowait(message(short, fields, self.source, level, ts))
        except queue.Full:
            self.status["errors"] += 1
            self.status["last_error"] = "очередь переполнена"
            return
        if self._t is None or not self._t.is_alive():
            self._t = threading.Thread(target=self._run, name="homerepo-gelf", daemon=True)
            self._t.start()

    def _run(self):
        while True:
            msg = self._q.get()
            try:
                self.send_now(msg)
                self.status["sent"] += 1
                self.status["last_ok"] = time.time()
            except (OSError, ValueError) as e:
                self.status["errors"] += 1
                self.status["last_error"] = str(e)
                log.warning("GELF %s:%s: %s", self.host, self.port, e)
            finally:
                self._q.task_done()

    def flush(self, timeout=5.0):
        """Дождаться отправки очереди (для тестов и остановки)."""
        end = time.time() + timeout
        while self._q.unfinished_tasks and time.time() < end:
            time.sleep(0.02)


# ---------- что уходит в Graylog ----------

_sender = None
_sender_ready = False
_lock = threading.Lock()


def sender():
    """Отправитель по настройкам из homerepo.conf; None, если отправка выключена или настроек нет."""
    global _sender, _sender_ready
    if not _sender_ready:
        with _lock:
            if not _sender_ready:
                try:
                    from . import config
                    _sender = Sender.from_config(config.get())
                    if _sender:
                        import atexit
                        atexit.register(_sender.flush, 3.0)   # CLI завершается быстро: дождаться очереди
                except Exception as e:  # noqa: BLE001 — без настроек просто не отправляем
                    log.warning("GELF: настройки не прочитаны: %s", e)
                    _sender = None
                _sender_ready = True
    return _sender


def reset(new=None):
    """Для тестов: подставить своего отправителя (или сбросить, чтобы перечитать настройки)."""
    global _sender, _sender_ready
    _sender, _sender_ready = new, new is not None


# syslog-уровни: 4 — warning, 5 — notice, 6 — info
WARN_ACTIONS = {
    "login_failed", "password_changed", "user_create", "user_delete", "user_role", "user_reset_password",
    "token_create", "token_delete", "package_resign", "resign_foreign", "package_delete", "repo_key",
    "host_enroll", "host_delete", "ansible_access", "ssh_install", "playbook_delete",
}
INFO_ACTIONS = {"login", "logout", "report", "errors_ack"}


def level_for(action):
    if action in WARN_ACTIONS:
        return 4
    return 6 if action in INFO_ACTIONS else 5


def audit_fields(user, ip, action, detail):
    f = {"hr_source": "homerepo", "homelab_app": "homerepo", "hr_event": "audit", "hr_action": action,
         "hr_user": user or "", "hr_client_ip": ip or "", "hr_agent": (user or "").startswith("agent:")}
    try:
        from .auditfmt import LABELS
        f["hr_action_name"] = LABELS.get(action, "")
    except Exception:  # noqa: BLE001
        pass
    d = detail
    if isinstance(d, str):
        try:
            d = json.loads(d) if d[:1] in "{[" else d
        except ValueError:
            pass
    if isinstance(d, dict):
        for k, v in list(d.items())[:20]:
            f["hr_detail_" + str(k)] = v
        f["hr_detail"] = json.dumps(d, ensure_ascii=False)[:2000]
    elif d not in (None, ""):
        f["hr_detail"] = (d if isinstance(d, str) else json.dumps(d, ensure_ascii=False))[:2000]
    return f


def audit(user, ip, action, detail=""):
    """Запись журнала действий → Graylog. Никогда не бросает исключений."""
    s = sender()
    if not s:
        return
    try:
        try:
            from .auditfmt import describe
            text = describe(action, detail, user)
        except Exception:  # noqa: BLE001
            text = action
        s.submit("HomeRepo: %s — %s" % (text, user or "?"), audit_fields(user, ip, action, detail), level_for(action))
    except Exception as e:  # noqa: BLE001
        log.warning("GELF: запись журнала не отправлена: %s", e)


def device_new(dev):
    """Сканер нашёл в сети устройство, которого раньше не видел."""
    s = sender()
    if not s:
        return
    ports = dev.get("ports") or []
    s.submit("HomeRepo: новое устройство в сети %s%s" % (dev.get("ip", ""), " (%s)" % dev["hostname"] if dev.get("hostname") else ""),
             {"hr_source": "homerepo", "homelab_app": "homerepo", "hr_event": "device_new", "hr_ip": dev.get("ip", ""),
              "hr_mac": dev.get("mac", ""), "hr_vendor": dev.get("vendor", ""), "hr_hostname": dev.get("hostname", ""),
              "hr_ports": [str(p) for p in ports], "hr_os_family": dev.get("os_family", ""), "hr_os_guess": dev.get("os_guess", ""),
              "hr_network": dev.get("network", "")}, 4)
