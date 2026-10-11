"""Центр ошибок: неудачные задания службы подписи и назначения на устройствах, проблемы служб.
Ошибку можно повторить (если это имеет смысл) или скрыть после просмотра."""

import json
import time

from . import db, reports
from . import web
from .web import HTTPError, jresp, route

KEEP_DAYS = 30

OP_RU = {"add": "добавление пакета", "fetch": "скачивание по ссылке", "resign": "переподпись", "delete": "удаление",
         "move": "перемещение", "metadata": "метаданные", "rescan": "сканирование репозитория",
         "cleanup": "очистка", "key": "проверка ключа", "scan": "сканирование сети", "ssh_install": "установка агента по SSH"}
ACT_RU = reports.ACT_RU

HINTS = [
    ("ключ подписи недоступен", "Проверьте службу подписи: systemctl status homerepo-signer; homerepo status."),
    ("секретный ключ", "Ключ не найден у службы подписи — см. раздел «Подпись» → «Ключ подписи»."),
    ("контрольные суммы", "Файл повреждён или загружен не полностью — загрузите его заново."),
    ("не rpm", "Загружен не RPM-пакет — проверьте файл или ссылку."),
    ("не удалось скачать", "Проверьте ссылку: открывается ли она с сервера репозитория, нет ли авторизации."),
    ("архитектура", "Репозиторий не обслуживает эту архитектуру — добавьте её в arches в /etc/homerepo/homerepo.conf."),
    ("мало места", "Освободите место на диске сервера (df -h) и повторите."),
    ("установлен вручную из файла", "Загрузите исходный .rpm в разделе «Подпись»."),
    ("не удалось получить", "Источник пакета его больше не отдаёт — загрузите .rpm вручную в разделе «Подпись»."),
    ("агент не сообщил результат", "Проверьте агента на устройстве: journalctl -u homerepo-agent -n 50."),
    ("no match for argument", "Такого пакета нет в подключённых репозиториях устройства — проверьте, подключён ли HomeRepo."),
    ("nothing provides", "Не хватает зависимости — добавьте нужный пакет в HomeRepo или подключите репозиторий ОС."),
    ("curl error", "Устройство не может скачать метаданные или пакет — проверьте сеть и адрес репозитория."),
    ("cannot download", "Устройство не может скачать пакет — проверьте сеть и адрес репозитория."),
    ("gpg check failed", "Пакет подписан не ключом HomeRepo — переподпишите его в разделе «Пакеты»."),
    ("public key", "На устройстве нет ключа HomeRepo — выполните setup.sh ещё раз или rpm --import ключа."),
    ("versionlock", "Нужен плагин dnf versionlock (python3-dnf-plugin-versionlock)."),
    ("permission denied", "Неверный логин/пароль SSH или ключ панели не добавлен на хост."),
    ("ssh недоступен", "Проверьте, что на устройстве запущен sshd и открыт порт 22."),
    ("sudo", "Пользователю нужны права sudo; при необходимости укажите пароль sudo."),
    ("прервано перезапуском", "Панель перезапускалась во время задания — просто повторите."),
    ("createrepo", "Не удалось пересобрать метаданные — см. journalctl -u homerepo-signer."),
]


def hint(text):
    low = (text or "").lower()
    for key, h in HINTS:
        if key in low:
            return h
    return ""


RETRY_OPS = {"resign", "delete", "move", "metadata", "rescan", "cleanup", "key", "scan"}


def job_retryable(j, params):
    if j["op"] in RETRY_OPS:
        return True
    orig = params.get("original") or params.get("url") or ""
    return j["op"] in ("add", "fetch") and orig.startswith(("http://", "https://"))


def collect(con, include_ack=False):
    since = time.time() - KEEP_DAYS * 86400
    ackq = "" if include_ack else " AND j.ack=0"
    jobs = []
    for j in db.rows(con.execute("SELECT j.* FROM jobs j WHERE j.status='error' AND coalesce(j.finished, j.created)>?%s "
                                 "ORDER BY j.id DESC LIMIT 300" % ackq, (since,))):
        try:
            params = json.loads(j.get("params") or "{}")
        except ValueError:
            params = {}
        jobs.append({"id": j["id"], "kind": OP_RU.get(j["op"], j["op"]), "op": j["op"], "title": j["title"],
                     "message": j["message"] or "", "log": j.get("log") or "", "who": j["created_by"],
                     "ts": j["finished"] or j["created"], "ack": j["ack"], "retry": job_retryable(j, params),
                     "hint": hint(j["message"] or "")})
    ackq = "" if include_ack else " AND t.ack=0"
    tasks = []
    for t in db.rows(con.execute("SELECT t.*, h.name AS host FROM tasks t JOIN hosts h ON h.id=t.host_id "
                                 "WHERE t.status='error' AND coalesce(t.finished, t.created)>?%s ORDER BY t.id DESC LIMIT 300"
                                 % ackq, (since,))):
        tasks.append({"id": t["id"], "host": t["host"], "host_id": t["host_id"], "action": ACT_RU.get(t["action"], t["action"]),
                      "name": t["name"], "evr": t["evr"] or "", "output": t["output"] or "", "who": t["created_by"],
                      "ts": t["finished"] or t["created"], "ack": t["ack"], "hint": hint(t["output"] or "")})
    system = []
    s = web.signer_state(con)
    if not s.get("alive"):
        system.append({"title": "Служба подписи не отвечает",
                       "message": "последний сигнал: %s" % (reports.dt(s.get("ts")) if s.get("ts") else "никогда"),
                       "hint": "systemctl status homerepo-signer; journalctl -u homerepo-signer -n 50"})
    k = db.get_setting(con, "key", {}) or {}
    if k.get("error"):
        system.append({"title": "Ключ подписи недоступен", "message": k["error"],
                       "hint": "Проверьте каталог ключа и signer.key_id в /etc/homerepo/homerepo.conf."})
    return {"system": system, "jobs": jobs, "tasks": tasks,
            "count": len(system) + sum(1 for j in jobs if not j["ack"]) + sum(1 for t in tasks if not t["ack"])}


@route("GET", "/api/errors")
def api_errors(req):
    con = web.C()
    if req.query.get("count"):
        since = time.time() - KEEP_DAYS * 86400
        n = con.execute("SELECT (SELECT count(*) FROM jobs WHERE status='error' AND ack=0 AND coalesce(finished,created)>?)"
                        " + (SELECT count(*) FROM tasks WHERE status='error' AND ack=0 AND coalesce(finished,created)>?)",
                        (since, since)).fetchone()[0]
        s = web.signer_state(con)
        k = db.get_setting(con, "key", {}) or {}
        return jresp({"count": n + (0 if s.get("alive") else 1) + (1 if k.get("error") else 0)})
    return jresp(collect(con, bool(req.query.get("all"))))


@route("POST", "/api/errors/ack", perm="operator")
def api_errors_ack(req):
    con = web.C()
    d = req.json()
    if d.get("all"):
        a = con.execute("UPDATE jobs SET ack=1 WHERE status='error' AND ack=0").rowcount
        b = con.execute("UPDATE tasks SET ack=1 WHERE status='error' AND ack=0").rowcount
        web.audit(req, "errors_ack", "все: %d" % (a + b))
        return jresp({"ok": True, "hidden": a + b})
    jobs = [int(x) for x in d.get("jobs") or []]
    tasks = [int(x) for x in d.get("tasks") or []]
    for i in jobs:
        con.execute("UPDATE jobs SET ack=1 WHERE id=? AND status='error'", (i,))
    for i in tasks:
        con.execute("UPDATE tasks SET ack=1 WHERE id=? AND status='error'", (i,))
    web.audit(req, "errors_ack", {"jobs": jobs, "tasks": tasks})
    return jresp({"ok": True})


@route("POST", r"/api/jobs/(\d+)/retry", perm="operator")
def api_job_retry(req, jid):
    con = web.C()
    j = con.execute("SELECT * FROM jobs WHERE id=?", (int(jid),)).fetchone()
    if not j or j["status"] != "error":
        raise HTTPError(400, "повторить можно только задание с ошибкой")
    params = json.loads(j["params"] or "{}")
    if not job_retryable(dict(j), params):
        if j["op"] == "ssh_install":
            raise HTTPError(400, "пароль SSH не сохраняется — запустите установку заново в разделе «Сканер»")
        raise HTTPError(400, "исходный файл после ошибки удалён — загрузите пакет заново")
    if j["op"] in ("add", "fetch"):
        url = params.get("original") or params.get("url")
        new = db.add_job(con, "fetch", {"url": url, "branch": params.get("branch"), "sign": params.get("sign", True),
                                        "keep": params.get("keep", 0)}, j["title"], req.uname, worker="web")
    else:
        new = db.add_job(con, j["op"], params, j["title"], req.uname, worker=j["worker"])
    con.execute("UPDATE jobs SET ack=1 WHERE id=?", (j["id"],))
    web.audit(req, "job_retry", "#%s → #%d %s" % (jid, new, j["title"]))
    return jresp({"job": new})


@route("GET", r"/api/report/errors\.html")
def report_errors(req):
    con = web.C()
    allx = bool(req.query.get("all"))
    d = collect(con, allx)
    e, dt = reports.e, reports.dt
    out = ['<header><div><h1>Отчёт об ошибках</h1><div class="muted">%s за %d дней</div></div>%s</header>'
           % ("все ошибки, включая скрытые," if allx else "непросмотренные ошибки", KEEP_DAYS,
              reports.buttons("/api/report/errors.html?download=1%s" % ("&all=1" if allx else "")))]
    out.append('<div class="grid"><div class="kpi"><b>%d</b><span>проблем служб</span></div>'
               '<div class="kpi"><b>%d</b><span>заданий с ошибкой</span></div>'
               '<div class="kpi"><b>%d</b><span>назначений с ошибкой</span></div>'
               '<div class="kpi"><b>%d</b><span>всего</span></div></div>'
               % (len(d["system"]), len(d["jobs"]), len(d["tasks"]), len(d["system"]) + len(d["jobs"]) + len(d["tasks"])))
    if d["system"]:
        out.append("<h2>Службы</h2><table><tr><th>Проблема</th><th>Подробности</th><th>Что сделать</th></tr>%s</table>" % "".join(
            "<tr><td><b>%s</b></td><td>%s</td><td class='small'>%s</td></tr>" % (e(x["title"]), e(x["message"]), e(x["hint"]))
            for x in d["system"]))
    out.append("<h2>Задания службы подписи и панели · %d</h2>" % len(d["jobs"]))
    out.append("<table><tr><th>Когда</th><th>Что</th><th>Кто</th><th>Ошибка</th><th>Что сделать</th></tr>%s</table>" % ("".join(
        "<tr><td>%s</td><td>%s<div class='muted small'>%s</div></td><td>%s</td><td class='small'>%s%s</td><td class='small'>%s</td></tr>"
        % (dt(x["ts"]), e(x["title"]), e(x["kind"]), e(x["who"]), e(x["message"]),
           " <span class='tag'>скрыто</span>" if x["ack"] else "", e(x["hint"])) for x in d["jobs"])
        or "<tr><td colspan=5 class='muted'>нет</td></tr>"))
    out.append("<h2>Назначения на устройствах · %d</h2>" % len(d["tasks"]))
    out.append("<table><tr><th>Когда</th><th>Устройство</th><th>Действие</th><th>Ошибка</th><th>Что сделать</th></tr>%s</table>" % ("".join(
        "<tr><td>%s</td><td>%s</td><td>%s %s %s</td><td class='small'>%s%s</td><td class='small'>%s</td></tr>"
        % (dt(x["ts"]), e(x["host"]), e(x["action"]), e(x["name"]), e(x["evr"]),
           e((x["output"] or "")[-600:]), " <span class='tag'>скрыто</span>" if x["ack"] else "", e(x["hint"]))
        for x in d["tasks"]) or "<tr><td colspan=5 class='muted'>нет</td></tr>"))
    fn = "homerepo-errors-%s.html" % time.strftime("%Y%m%d-%H%M")
    return reports._resp(reports.page("HomeRepo: ошибки", "".join(out), fn), fn, req)
