"""Журнал действий человеческим языком.

В базе запись хранится как есть (действие + подробности, иногда JSON), а при показе превращается в фразу.
Так читаемыми становятся и старые записи, сделанные до появления этого модуля.
"""

import json
import re

LABELS = {
    "login": "вход", "login_failed": "неудачный вход", "logout": "выход", "password_changed": "смена пароля",
    "upload": "загрузка пакета", "fetch": "скачивание по ссылке",
    "package_resign": "переподпись", "resign_foreign": "переподпись чужих", "package_delete": "удаление пакетов",
    "package_move": "перемещение пакетов",
    "repo_metadata": "пересборка метаданных", "repo_rescan": "пересканирование репозитория",
    "repo_cleanup": "очистка мусора", "repo_key": "проверка ключа", "repo_add": "добавление пакетов",
    "repo_resign": "переподпись",
    "job_cancel": "отмена задания", "job_retry": "повтор задания",
    "assign": "назначение", "task_cancel": "отмена назначения", "task_retry": "повтор назначения",
    "collect": "забрать в HomeRepo", "collect_upload": "пакет от агента",
    "host_enroll": "подключение устройства", "host_delete": "удаление устройства",
    "token_create": "токен создан", "token_delete": "токен удалён",
    "report": "просмотр отчёта", "errors_ack": "ошибки скрыты",
    "device_os": "система устройства", "scan": "сканирование сети", "scan_clear": "очистка найденных",
    "ssh_install_start": "установка агента по SSH", "ssh_install": "результат установки по SSH",
    "ssh_key_create": "SSH-ключ панели",
    "user_create": "пользователь создан", "user_delete": "пользователь удалён", "user_role": "смена роли",
    "user_disable": "блокировка", "user_enable": "разблокировка", "user_reset_password": "сброс пароля",
}

TASK_ACT = {"install": "установить или обновить", "update": "обновить", "remove": "удалить",
            "hold": "держать текущую версию", "unhold": "снять фиксацию версии", "collect": "забрать в HomeRepo"}
SCHED = {"now": "при следующем опросе агента", "night": "ночью, 03:00–05:00", "at": "к заданному времени"}
ROLES = {"viewer": "наблюдатель", "operator": "оператор", "admin": "администратор"}
FAM = {"linux": "Linux", "windows": "Windows", "macos": "macOS", "router": "роутер", "nas": "NAS / хранилище",
       "hypervisor": "гипервизор", "printer": "принтер", "phone": "телефон / планшет", "iot": "умное устройство",
       "other": "другое"}
REPO_OPS = {"metadata": "запущена пересборка метаданных всех веток",
            "rescan": "запущено пересканирование каталога репозитория — поиск новых, изменённых и удалённых файлов",
            "cleanup": "запущено удаление мусорных файлов из каталога репозитория",
            "key": "запущена проверка ключа подписи"}


def _cap(s):
    return s[:1].upper() + s[1:] if s else s


def _and(items, limit=8):
    items = [str(x) for x in items if str(x)]
    if len(items) > limit:
        return ", ".join(items[:limit]) + " и ещё %d" % (len(items) - limit)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " и " + items[-1]


def _size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    for u in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or u == "ГБ":
            return ("%d %s" % (n, u)) if u == "Б" else ("%.1f %s" % (n, u)).replace(".", ",")
        n /= 1024


def _hours(h):
    try:
        h = int(h)
    except (TypeError, ValueError):
        return ""
    if h % 24 == 0 and h >= 48:
        return "%d дн." % (h // 24)
    return "%d ч" % h


def _json(detail):
    s = (detail or "").strip()
    if s[:1] in "{[":
        try:
            return json.loads(s)
        except ValueError:
            return None
    return None


def _assign(d):
    act = TASK_ACT.get(d.get("action"), d.get("action") or "?")
    by_pkg = {}
    for item in d.get("created") or []:
        host, _, pkg = str(item).partition(": ")
        by_pkg.setdefault(pkg or host, []).append(host if pkg else "?")
    if not by_pkg:
        text = "%s — ничего не назначено" % _cap(act)
    else:
        groups = {}
        for pkg, hosts in by_pkg.items():
            groups.setdefault(tuple(hosts), []).append(pkg)
        parts = ["%s на %s" % (_and(pkgs), _and(hosts)) for hosts, pkgs in groups.items()]
        text = "%s %s" % (_cap(act), "; ".join(parts))
    sched = SCHED.get(d.get("schedule"))
    if sched and by_pkg:
        text += " — " + sched
    sk = d.get("skipped") or []
    if sk:
        text += ". Пропущено: " + "; ".join(str(x) for x in sk[:6]) + (" и ещё %d" % (len(sk) - 6) if len(sk) > 6 else "")
    return text


def _files(d, verb):
    files = d.get("files") or []
    text = "%s %s" % (verb, _and(files, 5)) if files else verb
    if d.get("to"):
        text += " → ветка %s" % d["to"]
    return text


def describe(action, detail, user="", ctx=None):
    """Фраза для записи журнала. ctx: {"hosts": {id: имя}, "tokens": {id: заметка}, "discovered": {ip: имя}}."""
    ctx = ctx or {}
    detail = detail or ""
    d = _json(detail)
    try:
        if action == "assign" and isinstance(d, dict):
            return _assign(d)
        if action == "errors_ack":
            if isinstance(d, dict):
                parts = []
                n = len(d.get("tasks") or []) + len(d.get("jobs") or [])
                if d.get("tasks"):
                    parts.append(("назначения " if n == 1 else "назначений ") + ", ".join("#%s" % x for x in d["tasks"]))
                if d.get("jobs"):
                    parts.append(("задания службы подписи " if n == 1 else "заданий службы подписи ") + ", ".join("#%s" % x for x in d["jobs"]))
                return ("Скрыта ошибка " if n == 1 else "Скрыты ошибки ") + "; ".join(parts) if parts else "Ошибки скрыты"
            m = re.match(r"все: (\d+)", detail)
            if m:
                return "Скрыты все ошибки (%s)" % m.group(1)
        if action == "token_create" and isinstance(d, dict):
            text = "%s токен подключения на %s" % ("Многоразовый" if d.get("multi") else "Одноразовый", _hours(d.get("hours")))
            if d.get("note"):
                text += " · «%s»" % d["note"]
            return text
        if action == "token_delete":
            m = re.match(r"#(\d+)", detail)
            note = ctx.get("tokens", {}).get(int(m.group(1))) if m else None
            return "Токен %s%s" % (detail, " · «%s»" % note if note else "")
        if action in ("package_resign", "package_delete", "package_move") and isinstance(d, dict):
            verb = {"package_resign": "Переподпись ключом HomeRepo:", "package_delete": "Удаление из репозитория:",
                    "package_move": "Перемещение"}[action]
            return _files(d, verb)
        if action == "resign_foreign":
            m = re.match(r"(\d+)", detail)
            return "Переподпись ключом HomeRepo всех пакетов с чужой подписью или без подписи" + (" — %s шт." % m.group(1) if m else "")
        if action == "upload":
            if isinstance(d, dict):
                text = "%s%s → ветка %s" % (d.get("file", "?"), " (%s)" % _size(d["size"]) if d.get("size") else "",
                                            d.get("branch", "?"))
                return text + (", с подписью ключом HomeRepo" if d.get("sign", True) else ", без переподписи")
            return "%s — из командной строки, с подписью ключом HomeRepo" % detail
        if action == "fetch" and isinstance(d, dict):
            return "%s → ветка %s" % (d.get("url", "?"), d.get("branch", "?"))
        if action.startswith("repo_") and action[5:] in REPO_OPS:
            return _cap(REPO_OPS[action[5:]])
        if action == "collect":
            m = re.match(r"^(\S+) (\S+) → (\S+)$", detail)
            if m:
                return "%s с устройства %s → ветка %s (агент скачает пакет из его источника)" % (m.group(2), m.group(1), m.group(3))
        if action == "collect_upload":
            m = re.match(r"^(\S+) \(с (.+)\)$", detail)
            if m:
                return "Получен %s с устройства %s и передан службе подписи" % (m.group(1), m.group(2))
        if action in ("task_cancel", "task_retry"):
            m = re.match(r"^#(\d+) (\S+) (\w+) (.+)$", detail)
            if m:
                return "Назначение #%s: %s %s на %s" % (m.group(1), TASK_ACT.get(m.group(3), m.group(3)), m.group(4), m.group(2))
        if action == "job_cancel":
            m = re.match(r"^#(\d+) (.*)$", detail)
            if m:
                return "Задание #%s: %s" % (m.group(1), m.group(2))
        if action == "job_retry":
            m = re.match(r"^#(\d+) → #(\d+) (.*)$", detail)
            if m:
                return "Задание #%s запущено заново как #%s: %s" % m.groups()
        if action == "report":
            m = re.match(r"устройство #(\d+)", detail)
            if m:
                name = ctx.get("hosts", {}).get(int(m.group(1)))
                return "Отчёт об устройстве %s" % (name or "#" + m.group(1))
        if action == "device_os":
            m = re.match(r"^(\S+): (.*)$", detail)
            if m:
                ip, rest = m.groups()
                hn = ctx.get("discovered", {}).get(ip)
                who = "%s (%s)" % (ip, hn) if hn else ip
                if rest == "автоопределение":
                    return "%s — тип системы снова определяется автоматически" % who
                fam, _, name = rest.partition(" ")
                fl = FAM.get(fam, fam)
                if not name or name.lower() == fl.lower():
                    return "%s — %s" % (who, fl)
                if name.lower().startswith(fl.lower()):  # «macOS 15» вместо «macOS, macOS 15»
                    return "%s — %s" % (who, name)
                return "%s — %s, %s" % (who, fl, name)
        if action == "scan":
            m = re.match(r"^(\S+) (quick|full)$", detail)
            if m:
                return "Сеть %s, %s" % (m.group(1), "расширенная проверка портов" if m.group(2) == "full" else "быстрая проверка портов")
        if action == "ssh_install_start":
            m = re.match(r"^(\S+?)@(.+) \((password|key)\)$", detail)
            if m:
                return "Пользователь %s на %s, вход %s" % (m.group(1), m.group(2),
                                                           "по паролю (пароль не сохраняется)" if m.group(3) == "password" else "ключом панели")
        if action == "ssh_key_create":
            return "Создан SSH-ключ панели для установки агентов без пароля"
        if action == "scan_clear":
            return "Список найденных сканером устройств очищен"
        if action == "host_enroll":
            m = re.match(r"токен #(\d+)", detail)
            if m:
                note = ctx.get("tokens", {}).get(int(m.group(1)))
                return "Агент зарегистрирован по токену #%s%s" % (m.group(1), " · «%s»" % note if note else "")
        if action == "host_delete":
            return "Устройство %s удалено из панели" % detail
        if action == "user_create":
            m = re.match(r"^(\S+) \((\w+)\)$", detail)
            if m:
                return "%s, роль «%s»" % (m.group(1), ROLES.get(m.group(2), m.group(2)))
        if action == "user_role":
            m = re.match(r"^(\S+) → (\w+)$", detail)
            if m:
                return "%s: новая роль «%s»" % (m.group(1), ROLES.get(m.group(2), m.group(2)))
        if action == "login_failed":
            return "Неверное имя пользователя или пароль"
    except Exception:  # noqa: BLE001 — журнал не должен ломаться из-за одной странной записи
        pass
    if isinstance(d, dict):  # неизвестная запись с JSON — хотя бы без скобок и кавычек
        return "; ".join("%s: %s" % (k, ", ".join(map(str, v)) if isinstance(v, list) else v)
                         for k, v in d.items() if v not in (None, "", [], {}))
    return detail


def context(con):
    def q(sql):
        try:
            return con.execute(sql).fetchall()
        except Exception:  # noqa: BLE001
            return []
    return {"hosts": {r[0]: r[1] for r in q("SELECT id, name FROM hosts")},
            "tokens": {r[0]: r[1] for r in q("SELECT id, note FROM tokens") if r[1]},
            "discovered": {r[0]: r[1] for r in q("SELECT ip, hostname FROM discovered") if r[1]}}
