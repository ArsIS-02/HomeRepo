"""Конструктор плейбуков: каталог блоков, сборка YAML из модели, готовые рецепты.

Модель плейбука (JSON, хранится в БД):
    {"name": ..., "description": ..., "become": true, "gather_facts": true, "serial": 0,
     "vars": [{"name": ..., "value": ...}], "secrets": [{"name": ..., "label": ...}],
     "steps": [{"type": <блок>, "params": {...}, "title": ..., "only_os": "", "no_become": false,
                "ignore_errors": false, "notify": "<служба>"}]}

Каждый блок превращается в одну или несколько задач Ansible. Секретные переменные запрашиваются
при запуске и в модель не попадают; задачи, которые их используют, получают no_log.
"""

import re

from . import yamlgen

OS_OPTIONS = [
    ("", "любая система"),
    ("redos", "только РЕД ОС"),
    ("rhel", "только Rocky, Alma, RHEL"),
    ("fedora", "только Fedora"),
    ("not_redos", "все, кроме РЕД ОС"),
]
REDOS = "ansible_distribution | lower in ['red', 'redos', 'red os']"
OS_WHEN = {
    "redos": REDOS,
    "rhel": "ansible_os_family == 'RedHat' and not (%s) and ansible_distribution != 'Fedora'" % REDOS,
    "fedora": "ansible_distribution == 'Fedora'",
    "not_redos": "not (%s)" % REDOS,
}
TIMEZONES = ["Europe/Kaliningrad", "Europe/Moscow", "Europe/Samara", "Asia/Yekaterinburg", "Asia/Omsk",
             "Asia/Novosibirsk", "Asia/Krasnoyarsk", "Asia/Irkutsk", "Asia/Yakutsk", "Asia/Vladivostok",
             "Asia/Magadan", "Asia/Kamchatka", "UTC"]

GROUPS = [
    ("homerepo", "HomeRepo и пакеты"),
    ("files", "Службы и файлы"),
    ("system", "Система и пользователи"),
    ("security", "Сеть и безопасность"),
    ("logs", "Логи и домен"),
    ("other", "Прочее"),
]


def F(key, label, kind="text", **kw):
    d = {"key": key, "label": label, "kind": kind}
    d.update(kw)
    return d


def _lines(v):
    if isinstance(v, list):
        items = v
    else:
        items = re.split(r"[\n,]", str(v or ""))
    return [x.strip() for x in items if str(x).strip()]


def _mode(v):
    v = str(v or "").strip()
    return v if re.match(r"^0?[0-7]{3,4}$", v) else (v or None)


def _clean(d):
    return {k: v for k, v in d.items() if v not in (None, "", [])}


class BlockError(ValueError):
    pass


# ---------------------------------------------------------------------- блоки
# build(params, ctx) -> список задач (dict); ctx: {"repo_url": ..., "branches": [...]}

def b_package(p, ctx):
    names = _lines(p.get("names"))
    if not names:
        raise BlockError("укажите хотя бы один пакет")
    return [{"ansible.builtin.dnf": {"name": names, "state": p.get("state") or "present"}}]


def b_homerepo_package(p, ctx):
    names = _lines(p.get("names"))
    if not names:
        raise BlockError("выберите хотя бы один пакет HomeRepo")
    return [{"ansible.builtin.dnf": {"name": names, "state": p.get("state") or "latest", "enablerepo": "homerepo-*"}}]


def b_homerepo_repo(p, ctx):
    url = ctx.get("repo_url") or "https://repo.example.lan/homerepo"
    branches = [b for b in _lines(p.get("branches")) if b] or ctx.get("branches") or ["os", "updates", "extras"]
    tasks = [{"name": "Ключ подписи HomeRepo", "ansible.builtin.rpm_key": {"key": url + "/RPM-GPG-KEY-HOMEREPO",
                                                                           "state": "present"}}]
    for b in branches:
        tasks.append({"name": "Репозиторий homerepo-%s" % b, "ansible.builtin.yum_repository": {
            "name": "homerepo-" + b, "description": "HomeRepo %s ($basearch)" % b,
            "baseurl": "%s/$basearch/%s" % (url, b), "enabled": True, "gpgcheck": True,
            "gpgkey": url + "/RPM-GPG-KEY-HOMEREPO", "metadata_expire": "300", "skip_if_unavailable": True}})
    return tasks


def b_upgrade(p, ctx):
    args = {"name": "*", "state": "latest", "update_only": True}
    if p.get("security"):
        args["security"] = True
    tasks = [{"ansible.builtin.dnf": args}]
    if p.get("reboot"):
        tasks.append({"name": "Нужна ли перезагрузка", "ansible.builtin.command": "needs-restarting -r",
                      "register": "hr_needs_reboot", "changed_when": False, "failed_when": False})
        tasks.append({"name": "Перезагрузка после обновления", "ansible.builtin.reboot": {"reboot_timeout": 900},
                      "when": "hr_needs_reboot.rc == 1"})
    return tasks


def b_service(p, ctx):
    name = (p.get("name") or "").strip()
    if not name:
        raise BlockError("укажите службу")
    a = {"name": name}
    if p.get("state"):
        a["state"] = p["state"]
    if p.get("enabled") in ("yes", "no"):
        a["enabled"] = p["enabled"] == "yes"
    if p.get("daemon_reload"):
        a["daemon_reload"] = True
    return [{"ansible.builtin.systemd": a}]


def b_file_content(p, ctx):
    dest = (p.get("dest") or "").strip()
    if not dest.startswith("/"):
        raise BlockError("путь к файлу должен быть абсолютным")
    content = p.get("content") or ""
    if content and not content.endswith("\n"):
        content += "\n"
    return [{"ansible.builtin.copy": _clean({"dest": dest, "content": content, "owner": p.get("owner"),
                                            "group": p.get("group"), "mode": _mode(p.get("mode")),
                                            "backup": True if p.get("backup") else None})}]


def b_lineinfile(p, ctx):
    path = (p.get("path") or "").strip()
    if not path.startswith("/"):
        raise BlockError("путь к файлу должен быть абсолютным")
    st = p.get("state") or "present"
    a = {"path": path, "regexp": p.get("regexp") or None, "state": st}
    if st == "present":
        if not p.get("line"):
            raise BlockError("укажите строку")
        a["line"] = p["line"]
        a["create"] = bool(p.get("create"))
    if p.get("backup"):
        a["backup"] = True
    return [{"ansible.builtin.lineinfile": _clean(a)}]


def b_directory(p, ctx):
    path = (p.get("path") or "").strip()
    if not path.startswith("/"):
        raise BlockError("путь должен быть абсолютным")
    st = p.get("state") or "directory"
    return [{"ansible.builtin.file": _clean({"path": path, "state": st, "owner": p.get("owner") if st != "absent" else None,
                                            "group": p.get("group") if st != "absent" else None,
                                            "mode": _mode(p.get("mode")) if st != "absent" else None})}]


def b_get_url(p, ctx):
    url, dest = (p.get("url") or "").strip(), (p.get("dest") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise BlockError("нужна ссылка http:// или https://")
    if not dest.startswith("/"):
        raise BlockError("путь назначения должен быть абсолютным")
    return [{"ansible.builtin.get_url": _clean({"url": url, "dest": dest, "mode": _mode(p.get("mode")),
                                               "checksum": p.get("checksum") or None,
                                               "validate_certs": False if p.get("insecure") else None})}]


def b_timezone(p, ctx):
    tz = (p.get("tz") or "").strip()
    if not tz:
        raise BlockError("выберите часовой пояс")
    return [{"community.general.timezone": {"name": tz}}]


def b_hostname(p, ctx):
    return [{"ansible.builtin.hostname": {"name": (p.get("name") or "{{ inventory_hostname }}").strip()}}]


def b_user(p, ctx):
    name = (p.get("name") or "").strip()
    if not re.match(r"^[a-z_][a-z0-9_.-]{0,31}$", name):
        raise BlockError("имя пользователя: латиница в нижнем регистре, цифры, . _ -")
    if p.get("state") == "absent":
        return [{"ansible.builtin.user": {"name": name, "state": "absent", "remove": bool(p.get("remove_home"))}},
                {"name": "Убрать sudo для %s" % name, "ansible.builtin.file": {"path": "/etc/sudoers.d/" + name,
                                                                              "state": "absent"}}]
    tasks = [{"ansible.builtin.user": _clean({"name": name, "groups": _lines(p.get("groups")) or None,
                                             "append": True if _lines(p.get("groups")) else None,
                                             "shell": p.get("shell") or None, "comment": p.get("comment") or None})}]
    keys = [k.strip() for k in (p.get("ssh_keys") or "").splitlines() if k.strip()]
    if keys:
        tasks.append({"name": "SSH-ключи для %s" % name, "ansible.posix.authorized_key": {
            "user": name, "key": "\n".join(keys) + "\n", "exclusive": bool(p.get("exclusive_keys"))}})
    if p.get("sudo"):
        tasks.append({"name": "sudo для %s" % name, "ansible.builtin.copy": {
            "dest": "/etc/sudoers.d/" + name, "mode": "0440",
            "content": "%s ALL=(ALL) %sALL\n" % (name, "NOPASSWD: " if p.get("nopasswd") else ""),
            "validate": "visudo -cf %s"}})
    return tasks


def b_sshd(p, ctx):
    tasks = []
    opts = [("PermitRootLogin", p.get("root") or "prohibit-password"),
            ("PasswordAuthentication", "no" if p.get("no_password") else "yes")]
    if p.get("max_auth"):
        opts.append(("MaxAuthTries", str(int(p["max_auth"]))))
    for k, v in opts:
        tasks.append({"name": "sshd: %s %s" % (k, v), "ansible.builtin.lineinfile": {
            "path": "/etc/ssh/sshd_config", "regexp": r"^\s*#?\s*%s\s" % k, "line": "%s %s" % (k, v),
            "validate": "/usr/sbin/sshd -t -f %s"}, "notify": "Перезапустить sshd"})
    return tasks


def b_reboot(p, ctx):
    return [{"ansible.builtin.reboot": _clean({"reboot_timeout": int(p.get("timeout") or 600),
                                              "msg": p.get("msg") or "Перезагрузка по плейбуку HomeRepo"})}]


def b_command(p, ctx):
    cmd = (p.get("cmd") or "").strip()
    if not cmd:
        raise BlockError("укажите команду")
    t = {"ansible.builtin.shell": _clean({"cmd": cmd, "creates": p.get("creates") or None,
                                         "chdir": p.get("chdir") or None})}
    if p.get("readonly"):
        t["changed_when"] = False
    return [t]


def b_firewalld(p, ctx):
    services, ports = _lines(p.get("services")), _lines(p.get("ports"))
    if not services and not ports:
        raise BlockError("укажите службы или порты")
    st = p.get("state") or "enabled"
    zone = (p.get("zone") or "").strip() or None
    tasks = []
    if services:
        tasks.append({"name": "firewalld: службы", "ansible.posix.firewalld": _clean({
            "service": "{{ item }}", "state": st, "permanent": True, "immediate": True, "zone": zone}),
            "loop": services})
    if ports:
        for x in ports:
            if not re.match(r"^\d+(-\d+)?/(tcp|udp|sctp)$", x):
                raise BlockError("порт в виде 8080/tcp или 6000-6010/udp: %s" % x)
        tasks.append({"name": "firewalld: порты", "ansible.posix.firewalld": _clean({
            "port": "{{ item }}", "state": st, "permanent": True, "immediate": True, "zone": zone}),
            "loop": ports})
    return tasks


def b_seboolean(p, ctx):
    name = (p.get("name") or "").strip()
    if not name:
        raise BlockError("укажите переключатель SELinux")
    return [{"ansible.posix.seboolean": {"name": name, "state": bool(p.get("state", True)), "persistent": True}}]


def b_rsyslog(p, ctx):
    host = (p.get("host") or "").strip()
    if not host:
        raise BlockError("укажите сервер журналов")
    port = int(p.get("port") or 514)
    at = "@@" if (p.get("proto") or "tcp") == "tcp" else "@"
    sel = (p.get("selector") or "*.*").strip()
    return [
        {"name": "rsyslog установлен", "ansible.builtin.dnf": {"name": "rsyslog", "state": "present"}},
        {"name": "Пересылка журналов на %s" % host, "ansible.builtin.copy": {
            "dest": "/etc/rsyslog.d/90-homerepo-forward.conf", "mode": "0644",
            "content": "# создано плейбуком HomeRepo\n%s %s%s:%d\n" % (sel, at, host, port)},
         "notify": "Перезапустить rsyslog"},
    ]


def b_ipa_client(p, ctx):
    domain, server = (p.get("domain") or "").strip(), (p.get("server") or "").strip()
    if not domain:
        raise BlockError("укажите домен")
    secret = (p.get("password_var") or "ipa_password").strip()
    args = ["ipa-client-install", "--unattended", "--domain=" + domain]
    if server:
        args.append("--server=" + server)
    if p.get("realm"):
        args.append("--realm=" + p["realm"].strip())
    args.append("--principal=" + ((p.get("principal") or "admin").strip()))
    args.append('--password="{{ %s }}"' % secret)
    if p.get("mkhomedir", True):
        args.append("--mkhomedir")
    if p.get("no_ntp"):
        args.append("--no-ntp")
    return [
        {"name": "Пакет клиента FreeIPA", "ansible.builtin.dnf": {"name": "freeipa-client", "state": "present"}},
        {"name": "Ввод в домен %s" % domain, "ansible.builtin.shell": {"cmd": " ".join(args),
                                                                      "creates": "/etc/ipa/default.conf"},
         "no_log": True},
    ]


def b_debug(p, ctx):
    return [{"ansible.builtin.debug": {"msg": p.get("msg") or "{{ inventory_hostname }}"}}]


def b_raw(p, ctx):
    text = (p.get("yaml") or "").rstrip()
    if not text:
        raise BlockError("вставьте задачу в формате YAML")
    return [yamlgen.Raw(text)]


BLOCKS = [
    {"type": "package", "group": "homerepo", "title": "Пакеты", "desc": "Установить, обновить или удалить пакеты (dnf)",
     "fields": [F("names", "Пакеты", "lines", placeholder="htop\ntmux", required=True),
                F("state", "Состояние", "select", options=[["present", "установлен"], ["latest", "последняя версия"],
                                                            ["absent", "удалён"]], default="present")],
     "build": b_package},
    {"type": "homerepo_package", "group": "homerepo", "title": "Пакет из HomeRepo",
     "desc": "Поставить пакеты из вашего репозитория (подключит репозитории homerepo-* на время установки)",
     "fields": [F("names", "Пакеты HomeRepo", "repo_packages", required=True),
                F("state", "Состояние", "select", options=[["latest", "последняя версия"], ["present", "установлен"]],
                  default="latest")],
     "build": b_homerepo_package},
    {"type": "homerepo_repo", "group": "homerepo", "title": "Подключить HomeRepo",
     "desc": "Ключ подписи и .repo для веток репозитория — как setup.sh, но без агента",
     "fields": [F("branches", "Ветки", "branches", help="пусто — все ветки")], "build": b_homerepo_repo},
    {"type": "upgrade", "group": "homerepo", "title": "Обновить систему",
     "desc": "Все доступные обновления (dnf upgrade) или только обновления безопасности",
     "fields": [F("security", "Только обновления безопасности", "bool"),
                F("reboot", "Перезагрузить, если нужно (needs-restarting)", "bool")],
     "build": b_upgrade},
    {"type": "service", "group": "files", "title": "Служба", "desc": "Запустить, перезапустить, остановить, включить автозапуск",
     "fields": [F("name", "Служба", "text", placeholder="chronyd", required=True),
                F("state", "Действие", "select", options=[["started", "запущена"], ["restarted", "перезапустить"],
                                                           ["reloaded", "перечитать настройки"], ["stopped", "остановлена"],
                                                           ["", "не менять"]], default="started"),
                F("enabled", "Автозапуск", "select", options=[["yes", "включён"], ["no", "выключен"], ["", "не менять"]],
                  default="yes"),
                F("daemon_reload", "Перечитать юниты systemd", "bool")],
     "build": b_service},
    {"type": "file_content", "group": "files", "title": "Файл с содержимым",
     "desc": "Записать файл целиком. Можно использовать переменные: {{ inventory_hostname }}, {{ ansible_default_ipv4.address }}",
     "fields": [F("dest", "Путь", "text", placeholder="/etc/motd", required=True),
                F("content", "Содержимое", "code"),
                F("owner", "Владелец", "text", placeholder="root"), F("group", "Группа", "text", placeholder="root"),
                F("mode", "Права", "text", placeholder="0644"), F("backup", "Сохранить копию прежнего файла", "bool")],
     "build": b_file_content},
    {"type": "lineinfile", "group": "files", "title": "Строка в файле",
     "desc": "Добавить, заменить по шаблону или удалить строку в конфиге",
     "fields": [F("path", "Файл", "text", placeholder="/etc/sysctl.conf", required=True),
                F("regexp", "Шаблон (регулярное выражение)", "text", placeholder="^vm.swappiness"),
                F("line", "Строка", "text", placeholder="vm.swappiness = 10"),
                F("state", "Действие", "select", options=[["present", "строка есть"], ["absent", "удалить строки по шаблону"]],
                  default="present"),
                F("create", "Создать файл, если его нет", "bool"), F("backup", "Сохранить копию", "bool")],
     "build": b_lineinfile},
    {"type": "directory", "group": "files", "title": "Каталог", "desc": "Создать каталог с владельцем и правами или удалить",
     "fields": [F("path", "Путь", "text", placeholder="/srv/backup", required=True),
                F("state", "Действие", "select", options=[["directory", "создать"], ["absent", "удалить"]], default="directory"),
                F("owner", "Владелец", "text"), F("group", "Группа", "text"), F("mode", "Права", "text", placeholder="0755")],
     "build": b_directory},
    {"type": "get_url", "group": "files", "title": "Скачать файл", "desc": "Скачать файл по ссылке на хосты",
     "fields": [F("url", "Ссылка", "text", required=True), F("dest", "Куда", "text", placeholder="/opt/tool/tool.tar.gz", required=True),
                F("mode", "Права", "text"), F("checksum", "Контрольная сумма", "text", placeholder="sha256:…"),
                F("insecure", "Не проверять сертификат", "bool")],
     "build": b_get_url},
    {"type": "timezone", "group": "system", "title": "Часовой пояс", "desc": "Установить часовой пояс",
     "fields": [F("tz", "Часовой пояс", "select", options=[[t, t] for t in TIMEZONES], default="Europe/Moscow", editable=True)],
     "build": b_timezone},
    {"type": "hostname", "group": "system", "title": "Имя хоста", "desc": "Задать имя хоста (по умолчанию — имя из инвентаря)",
     "fields": [F("name", "Имя", "text", placeholder="{{ inventory_hostname }}")], "build": b_hostname},
    {"type": "user", "group": "system", "title": "Пользователь", "desc": "Создать пользователя, разложить SSH-ключи, дать sudo — или удалить",
     "fields": [F("name", "Имя", "text", required=True), F("comment", "Описание", "text"),
                F("groups", "Доп. группы", "text", placeholder="wheel"), F("shell", "Оболочка", "text", placeholder="/bin/bash"),
                F("ssh_keys", "SSH-ключи (по одному в строке)", "code"),
                F("exclusive_keys", "Оставить только эти ключи", "bool"),
                F("sudo", "Права sudo", "bool"), F("nopasswd", "sudo без пароля", "bool"),
                F("state", "Действие", "select", options=[["present", "есть"], ["absent", "удалить"]], default="present"),
                F("remove_home", "При удалении стереть домашний каталог", "bool")],
     "build": b_user},
    {"type": "sshd", "group": "security", "title": "Безопасный SSH", "desc": "Запрет входа root по паролю, вход только по ключам",
     "fields": [F("root", "Вход root", "select", options=[["prohibit-password", "только по ключу"], ["no", "запрещён"],
                                                          ["yes", "разрешён"]], default="prohibit-password"),
                F("no_password", "Запретить вход по паролю для всех", "bool"),
                F("max_auth", "Попыток входа", "number", placeholder="4")],
     "build": b_sshd, "handlers": {"Перезапустить sshd": "sshd"}},
    {"type": "firewalld", "group": "security", "title": "Firewalld", "desc": "Открыть или закрыть службы и порты (постоянно и сразу)",
     "fields": [F("services", "Службы", "lines", placeholder="http\nhttps"), F("ports", "Порты", "lines", placeholder="9100/tcp"),
                F("state", "Действие", "select", options=[["enabled", "открыть"], ["disabled", "закрыть"]], default="enabled"),
                F("zone", "Зона", "text", placeholder="по умолчанию")],
     "build": b_firewalld},
    {"type": "seboolean", "group": "security", "title": "SELinux: переключатель", "desc": "Включить или выключить boolean SELinux",
     "fields": [F("name", "Переключатель", "text", placeholder="httpd_can_network_connect", required=True),
                F("state", "Включён", "bool", default=True)],
     "build": b_seboolean},
    {"type": "rsyslog", "group": "logs", "title": "Пересылка журналов", "desc": "rsyslog отправляет журналы на ваш сервер (Graylog и т. п.)",
     "fields": [F("host", "Сервер журналов", "text", placeholder="logs.example.lan", required=True),
                F("port", "Порт", "number", default=514), F("proto", "Протокол", "select", options=[["tcp", "TCP"], ["udp", "UDP"]],
                                                            default="tcp"),
                F("selector", "Что пересылать", "text", default="*.*")],
     "build": b_rsyslog, "handlers": {"Перезапустить rsyslog": "rsyslog"}},
    {"type": "ipa_client", "group": "logs", "title": "Ввод в домен FreeIPA", "desc": "Установить клиент и ввести хост в домен (пароль вводится при запуске)",
     "fields": [F("domain", "Домен", "text", placeholder="example.lan", required=True),
                F("server", "Сервер", "text", placeholder="ipa.example.lan"), F("realm", "Realm", "text", placeholder="EXAMPLE.LAN"),
                F("principal", "Учётная запись", "text", default="admin"),
                F("password_var", "Переменная с паролем", "secret", default="ipa_password",
                  help="пароль спрашивается при запуске и нигде не сохраняется"),
                F("mkhomedir", "Создавать домашние каталоги", "bool", default=True), F("no_ntp", "Не настраивать время", "bool")],
     "build": b_ipa_client},
    {"type": "reboot", "group": "other", "title": "Перезагрузка", "desc": "Перезагрузить и дождаться возвращения хоста",
     "fields": [F("timeout", "Ждать, сек", "number", default=600), F("msg", "Сообщение", "text")], "build": b_reboot},
    {"type": "command", "group": "other", "title": "Команда", "desc": "Выполнить команду оболочки. Ansible не знает, что она меняет — используйте с осторожностью",
     "fields": [F("cmd", "Команда", "code", required=True), F("creates", "Не выполнять, если есть файл", "text"),
                F("chdir", "Каталог", "text"), F("readonly", "Только чтение (не считать изменением)", "bool")],
     "build": b_command},
    {"type": "debug", "group": "other", "title": "Сообщение", "desc": "Вывести сообщение или значение переменной",
     "fields": [F("msg", "Текст", "text", placeholder="{{ ansible_distribution }} {{ ansible_distribution_version }}")],
     "build": b_debug},
    {"type": "raw", "group": "other", "title": "Задача YAML", "desc": "Задача Ansible, написанная вручную",
     "fields": [F("yaml", "YAML задачи", "code", required=True,
                  placeholder="ansible.builtin.cron:\n  name: backup\n  minute: \"0\"\n  hour: \"3\"\n  job: /usr/local/bin/backup.sh")],
     "build": b_raw},
]
BY_TYPE = {b["type"]: b for b in BLOCKS}


def catalog():
    out = []
    for b in BLOCKS:
        out.append({k: v for k, v in b.items() if k not in ("build",)})
    return {"blocks": out, "groups": [{"key": k, "title": t} for k, t in GROUPS],
            "os_options": [{"key": k, "title": t} for k, t in OS_OPTIONS]}


def _secret_names(model):
    names = {s.get("name") for s in model.get("secrets") or [] if s.get("name")}
    for st in model.get("steps") or []:
        for f in BY_TYPE.get(st.get("type"), {}).get("fields", []):
            if f["kind"] == "secret":
                names.add((st.get("params") or {}).get(f["key"]) or f.get("default"))
    return {n for n in names if n}


def secrets_needed(model):
    """Секретные переменные, которые надо спросить при запуске: [{"name", "label"}]."""
    out, seen = [], set()
    for s in model.get("secrets") or []:
        if s.get("name") and s["name"] not in seen:
            seen.add(s["name"])
            out.append({"name": s["name"], "label": s.get("label") or s["name"]})
    for st in model.get("steps") or []:
        for f in BY_TYPE.get(st.get("type"), {}).get("fields", []):
            if f["kind"] == "secret":
                n = (st.get("params") or {}).get(f["key"]) or f.get("default")
                if n and n not in seen:
                    seen.add(n)
                    out.append({"name": n, "label": "%s: %s" % (BY_TYPE[st["type"]]["title"], f.get("help") and "пароль" or n)})
    return out


def render(model, ctx=None):
    """Модель -> (yaml, ошибки по шагам). Ошибки не мешают показать остальной YAML."""
    ctx = ctx or {}
    errors = {}
    tasks, handlers = [], {}
    secret_vars = _secret_names(model)
    for i, st in enumerate(model.get("steps") or []):
        b = BY_TYPE.get(st.get("type"))
        if not b:
            errors[i] = "неизвестный блок"
            continue
        params = {f["key"]: f["default"] for f in b["fields"] if "default" in f}
        params.update({k: v for k, v in (st.get("params") or {}).items() if v is not None})
        try:
            built = b["build"](params, ctx)
        except BlockError as e:
            errors[i] = str(e)
            continue
        except (TypeError, ValueError) as e:
            errors[i] = "неверное значение: %s" % e
            continue
        title = (st.get("title") or "").strip() or b["title"]
        when = OS_WHEN.get(st.get("only_os") or "")
        multi = len(built) > 1
        for j, t in enumerate(built):
            if isinstance(t, yamlgen.Raw):
                tasks.append(t)
                continue
            task = {}
            if multi and t.get("name"):
                task["name"] = "%s — %s" % (title, t["name"])
            elif multi and j > 0:
                task["name"] = "%s (%d)" % (title, j + 1)
            else:
                task["name"] = title
            for k, v in t.items():
                if k != "name":
                    task[k] = v
            if when:
                prev = task.get("when")
                task["when"] = [when, prev] if prev else when
            if st.get("no_become"):
                task["become"] = False
            if st.get("ignore_errors"):
                task["ignore_errors"] = True
            if st.get("notify"):
                hn = "Перезапустить %s" % st["notify"].strip()
                task["notify"] = hn
                handlers[hn] = st["notify"].strip()
            if any(("{{ %s }}" % s) in yamlgen.dump(t) for s in secret_vars):
                task["no_log"] = True
            tasks.append(task)
        for hn, svc in (b.get("handlers") or {}).items():
            if any(isinstance(t, dict) and t.get("notify") == hn for t in built):
                handlers[hn] = svc
    play = {"name": (model.get("name") or "Плейбук").strip(), "hosts": "all"}
    play["become"] = bool(model.get("become", True))
    # условие «только для такой-то ОС» опирается на факты — без их сбора задача упадёт
    uses_os = any(st.get("only_os") for st in model.get("steps") or [])
    play["gather_facts"] = bool(model.get("gather_facts", True)) or uses_os
    if int(model.get("serial") or 0) > 0:
        play["serial"] = int(model["serial"])
    vs = {}
    for v in model.get("vars") or []:
        n = (v.get("name") or "").strip()
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", n):
            vs[n] = v.get("value", "")
    if vs:
        play["vars"] = vs
    play["tasks"] = tasks or [{"name": "Пока нет шагов", "ansible.builtin.debug": {"msg": "добавьте шаги в конструкторе"}}]
    if handlers:
        play["handlers"] = [{"name": hn, "ansible.builtin.systemd": {"name": svc, "state": "restarted"}}
                            for hn, svc in handlers.items()]
    header = "Плейбук HomeRepo: %s" % play["name"]
    if model.get("description"):
        header += "\n" + model["description"].strip().replace("\n", " ")
    if secret_vars:
        header += "\nСекретные переменные (вводятся при запуске): " + ", ".join(sorted(secret_vars))
    return yamlgen.dump([play], header), errors


# ---------------------------------------------------------------------- рецепты
def S(type_, title=None, **params):
    d = {"type": type_, "params": params}
    if title:
        d["title"] = title
    return d


RECIPES = [
    {"id": "baseline", "name": "Базовая настройка хоста",
     "description": "Часовой пояс, синхронизация времени, полезные пакеты, HomeRepo и безопасный SSH — для новой машины",
     "steps": [S("timezone", tz="Europe/Moscow"),
               S("package", "Полезные пакеты", names="chrony\nvim-enhanced\nhtop\ntmux\nbash-completion", state="present"),
               S("service", "Синхронизация времени", name="chronyd", state="started", enabled="yes"),
               S("homerepo_repo"),
               S("sshd", root="prohibit-password")]},
    {"id": "ssh", "name": "Безопасный SSH", "description": "Root — только по ключу, ограничение попыток, SSH открыт в firewalld",
     "steps": [S("sshd", root="prohibit-password", max_auth=4), S("firewalld", services="ssh", state="enabled")]},
    {"id": "homerepo", "name": "Подключить HomeRepo", "description": "Ключ подписи и репозитории homerepo-* на хостах без агента",
     "steps": [S("homerepo_repo")]},
    {"id": "upgrade", "name": "Обновить систему", "description": "Все доступные обновления пакетов",
     "steps": [S("upgrade")]},
    {"id": "upgrade_reboot", "name": "Обновить и перезагрузить по одному",
     "description": "Обновления, затем перезагрузка, если она нужна — по одному хосту, чтобы службы не пропали разом",
     "serial": 1, "steps": [S("upgrade", reboot=True)]},
    {"id": "security", "name": "Только обновления безопасности", "description": "dnf upgrade --security",
     "steps": [S("upgrade", security=True)]},
    {"id": "freeipa", "name": "Ввод в домен FreeIPA", "description": "Клиент FreeIPA и ввод хоста в домен; пароль спросят при запуске",
     "steps": [S("ipa_client", domain="example.lan", server="ipa.example.lan", principal="admin", password_var="ipa_password")]},
    {"id": "rsyslog", "name": "Пересылка журналов на сервер", "description": "rsyslog отправляет журналы на центральный сервер (например, Graylog)",
     "steps": [S("rsyslog", host="logs.example.lan", port=514, proto="tcp")]},
    {"id": "admin_user", "name": "Служебный пользователь", "description": "Пользователь с SSH-ключом и sudo на всех хостах",
     "steps": [S("user", name="admin2", groups="wheel", shell="/bin/bash", ssh_keys="ssh-ed25519 AAAA… admin@example.lan",
                 sudo=True, nopasswd=False, state="present")]},
    {"id": "node_exporter", "name": "Метрики для Prometheus", "description": "node_exporter из HomeRepo, автозапуск и открытый порт 9100",
     "steps": [S("homerepo_package", names="node_exporter", state="latest"),
               S("service", name="node_exporter", state="started", enabled="yes"),
               S("firewalld", ports="9100/tcp", state="enabled")]},
    {"id": "facts", "name": "Сведения о хостах", "description": "Ничего не меняет: выводит систему, ядро и адрес каждого хоста",
     "steps": [S("debug", "Система",
                 msg="{{ ansible_distribution }} {{ ansible_distribution_version }}, ядро {{ ansible_kernel }}, "
                     "{{ ansible_default_ipv4.address | default('нет адреса') }}")]},
]
RECIPE_BY_ID = {r["id"]: r for r in RECIPES}


def recipe_model(rid):
    r = RECIPE_BY_ID.get(rid)
    if not r:
        return None
    return {"name": r["name"], "description": r["description"], "become": True, "gather_facts": True,
            "serial": r.get("serial", 0), "vars": [], "secrets": [], "steps": [dict(s) for s in r["steps"]]}
