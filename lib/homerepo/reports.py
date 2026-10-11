"""Отчёты по устройствам: самостоятельные HTML-страницы (открыть, сохранить, напечатать в PDF)."""

import html
import re
import time

from . import VERSION, db, inventory, rpmutil
from . import web
from .web import Resp, route

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#eef1f3;color:#14191c;font:13px/1.45 'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.page{max-width:1100px;margin:0 auto;padding:28px 32px 48px;background:#fff;min-height:100vh}
header{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;border-bottom:2px solid #2f8f94;padding-bottom:14px;margin-bottom:18px}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:13px;letter-spacing:.1em;text-transform:uppercase;color:#0b7480;margin:24px 0 10px;border-bottom:1px solid #d3d9dd;padding-bottom:6px}
.muted{color:#5d6970}.small{font-size:11.5px}
.bar{display:flex;gap:8px}
.btn{font:inherit;font-size:12px;letter-spacing:.05em;text-transform:uppercase;padding:8px 12px;border:1px solid #2c5a88;background:#2c5a88;color:#fff;cursor:pointer;text-decoration:none}
.btn.l{background:#fff;color:#2c5a88}
table{width:100%;border-collapse:collapse;margin:4px 0 8px}
th{text-align:left;font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:#5d6970;font-weight:600;padding:6px 8px;border-bottom:1px solid #c9d1d6;background:#f6f8f9}
td{padding:5px 8px;border-bottom:1px solid #e7ebed;vertical-align:top}
dl{display:grid;grid-template-columns:170px 1fr;gap:4px 16px;margin:0}
dt{color:#5d6970}dd{margin:0}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.kpi{border:1px solid #d3d9dd;padding:10px 12px}
.kpi b{display:block;font-size:20px;color:#0b7480}
.kpi span{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:#5d6970}
.tag{display:inline-block;font-size:10.5px;padding:1px 6px;border:1px solid #c9d1d6}
.ok{color:#1d7a3e;border-color:#9fd3b0}.warn{color:#9a4a10;border-color:#e6b98f}.bad{color:#b3261e;border-color:#eeb1ab}.info{color:#2057a8;border-color:#b3c8ec}
.num{text-align:right;white-space:nowrap}
details summary{cursor:pointer;color:#0b7480}
footer{margin-top:28px;border-top:1px solid #d3d9dd;padding-top:10px;font-size:11px;color:#5d6970}
@media print{
  body{background:#fff}.page{padding:0;max-width:none}.noprint{display:none!important}
  h2{break-after:avoid}tr{break-inside:avoid}details{display:block}details>*{display:block}
  @page{size:A4;margin:14mm 12mm}
}
"""

ST_RU = {"online": "на связи", "late": "давно не было", "offline": "нет связи"}
CLS_RU = {"homerepo": "из HomeRepo", "external": "извне", "os": "из ОС"}
ACT_RU = {"install": "установить / обновить", "update": "обновить", "remove": "удалить", "hold": "держать версию",
          "unhold": "снять фиксацию", "collect": "забрать в HomeRepo"}
TST_RU = {"pending": "ждёт", "sent": "выполняется", "processing": "в службе подписи", "done": "выполнено", "error": "ошибка", "skipped": "пропущено",
          "cancelled": "отменено"}


def e(x):
    return html.escape("" if x is None else str(x))


def dt(ts):
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts)) if ts else "—"


def size(n):
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "—"
    for unit, k in (("ГБ", 1024 ** 3), ("МБ", 1024 ** 2), ("КБ", 1024)):
        if n >= k:
            return ("%.1f %s" % (n / k, unit)).replace(".", ",")
    return "%d Б" % n


def pct(x):
    return "—" if x is None else "%d%%" % round(x)


def uptime(sec):
    if not sec:
        return "—"
    sec = int(sec)
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    return "%d дн %d ч" % (d, h) if d else "%d ч %d мин" % (h, m)


def page(title, body, download_name):
    return """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s</title><style>%s</style></head><body><div class="page">%s
<footer>HomeRepo %s · отчёт сформирован %s · %s</footer></div><script src="/report.js"></script></body></html>""" % (
        e(title), CSS, body, VERSION, dt(time.time()), e(download_name))


def buttons(dl_url):
    return ('<div class="bar noprint"><button class="btn" id="print">Печать / PDF</button>'
            '<a class="btn l" href="%s">Скачать HTML</a></div>' % e(dl_url))


def stats(hist, i):
    v = [r[i] for r in hist if r[i] is not None]
    if not v:
        return None
    return min(v), sum(v) / len(v), max(v)


def host_html(con, hid, full=False):
    h = web.host_detail(con, hid, 24)
    hw, m = h.get("hw") or {}, h.get("metrics") or {}
    name = h.get("fqdn") or h["name"]
    by = {"homerepo": [], "external": [], "os": []}
    for p in h["packages"]:
        by.setdefault(p["cls"], []).append(p)
    out = ['<header><div><h1>Отчёт об устройстве %s</h1><div class="muted">%s · %s · последний отчёт агента: %s · агент %s</div></div>%s</header>'
           % (e(name), e(hw.get("pretty_name") or "%s %s" % (h.get("os_name") or "", h.get("os_version") or "")),
              e(ST_RU.get(h["status"], h["status"])), dt(h.get("last_report")), e(h.get("agent_version") or "?"),
              buttons("/api/report/host/%d.html?download=1%s" % (h["id"], "&full=1" if full else "")))]
    out.append('<div class="grid">'
               '<div class="kpi"><b>%d</b><span>пакетов всего</span></div>'
               '<div class="kpi"><b>%d</b><span>из HomeRepo</span></div>'
               '<div class="kpi"><b>%d</b><span>извне</span></div>'
               '<div class="kpi"><b>%d</b><span>обновлений доступно</span></div></div>'
               % (len(h["packages"]), len(by["homerepo"]), len(by["external"]), len(h["updates"])))
    ifs = "<br>".join("%s %s <span class='muted'>%s</span>" % (e(i.get("name")), e(i.get("addr")), e(i.get("mac")))
                      for i in hw.get("interfaces") or []) or e(h.get("ip"))
    out.append("<h2>Система</h2><dl>"
               "<dt>Имя</dt><dd>%s</dd><dt>Система</dt><dd>%s</dd><dt>Ядро</dt><dd>%s · %s</dd>"
               "<dt>Платформа</dt><dd>%s%s</dd><dt>Процессор</dt><dd>%s%s</dd><dt>Память</dt><dd>%s%s</dd>"
               "<dt>Работает</dt><dd>%s%s</dd><dt>SELinux</dt><dd>%s</dd><dt>Сеть</dt><dd>%s</dd>"
               "<dt>Подключён к панели</dt><dd>%s</dd></dl>"
               % (e(name), e(hw.get("pretty_name") or h.get("os_name")), e(h.get("kernel")), e(h.get("arch")),
                  e(hw.get("model") or "—"), (" · " + e(hw["virt"])) if hw.get("virt") and hw.get("virt") != "none" else "",
                  e(hw.get("cpu_model") or "—"), (" × %d" % hw["cores"]) if hw.get("cores") else "",
                  size(hw.get("mem_total")), (" · swap " + size(hw["swap_total"])) if hw.get("swap_total") else "",
                  uptime(m.get("uptime")), (" (с %s)" % dt(hw["boot"])) if hw.get("boot") else "",
                  e(hw.get("selinux") or "—"), ifs, dt(h.get("enrolled"))))
    hist = h.get("history") or []
    rows = []
    for label, cur, i, fmt in (("CPU", m.get("cpu"), 1, pct), ("Память", m.get("mem_pct"), 2, pct),
                               ("Swap", m.get("swap_pct"), 3, pct), ("Нагрузка (LA 1 мин)", (m.get("load") or [None])[0], 4,
                                                                      lambda x: "—" if x is None else "%.2f" % x),
                               ("Диск /", m.get("disk_pct"), 5, pct), ("Температура", m.get("temp"), 6,
                                                                      lambda x: "—" if x is None else "%d°" % round(x))):
        st = stats(hist, i)
        rows.append("<tr><td>%s</td><td class='num'>%s</td><td class='num'>%s</td><td class='num'>%s</td><td class='num'>%s</td></tr>"
                    % (label, fmt(cur), fmt(st[0]) if st else "—", fmt(st[1]) if st else "—", fmt(st[2]) if st else "—"))
    out.append("<h2>Нагрузка</h2><div class='muted small'>метрики на %s; мин / сред / макс — за последние 24 ч (%d замеров)</div>"
               "<table><tr><th>Показатель</th><th class='num'>сейчас</th><th class='num'>мин</th><th class='num'>сред</th>"
               "<th class='num'>макс</th></tr>%s</table>" % (dt(h.get("metrics_ts")), len(hist), "".join(rows)))
    dk = m.get("disks") or []
    if dk:
        out.append("<h2>Разделы</h2><table><tr><th>Раздел</th><th>Устройство</th><th>ФС</th><th class='num'>Размер</th>"
                   "<th class='num'>Занято</th><th class='num'>%</th></tr>" + "".join(
                       "<tr><td>%s</td><td>%s</td><td>%s</td><td class='num'>%s</td><td class='num'>%s</td><td class='num'>%s</td></tr>"
                       % (e(d.get("mount")), e(d.get("dev")), e(d.get("fs")), size(d.get("size")), size(d.get("used")),
                          pct(d.get("pct"))) for d in dk) + "</table>")
    if h["updates"]:
        out.append("<h2>Доступные обновления из HomeRepo · %d</h2><table><tr><th>Пакет</th><th>Установлено</th><th>В HomeRepo</th>"
                   "<th>Ветка</th><th>Сейчас из</th></tr>%s</table>" % (len(h["updates"]), "".join(
                       "<tr><td>%s</td><td>%s</td><td><b>%s</b></td><td>%s</td><td>%s</td></tr>"
                       % (e(u["name"]), e(u["installed"]), e(u["available"]), e(u["branch"]), e(u["from_repo"] or u["cls"]))
                       for u in h["updates"])))

    def pk_table(lst, with_repo):
        if not lst:
            return "<div class='muted'>нет</div>"
        head = "<tr><th>Пакет</th><th>Версия</th><th>Арх.</th>%s<th>Источник</th><th>Подпись</th><th>Установлен</th></tr>" % (
            "<th>В HomeRepo</th>" if with_repo else "")
        body = []
        for p in lst:
            repo = ""
            if with_repo:
                c = p.get("repo_cmp")
                tag = "" if c is None else ("<span class='tag info'>есть новее</span>" if c > 0 else
                                            "<span class='tag ok'>актуален</span>" if c == 0 else "")
                repo = "<td>%s %s</td>" % (e(p.get("repo_evr") or "—"), tag)
            body.append("<tr><td>%s</td><td>%s</td><td>%s</td>%s<td>%s</td><td class='small'>%s</td><td>%s</td></tr>"
                        % (e(p["name"]), e(p["evr"]), e(p["arch"]), repo,
                           e("вручную из .rpm" if p.get("from_repo") == "@commandline" else p.get("from_repo") or "—"),
                           e(p.get("sigkey") or "нет"), dt(p.get("installtime"))[:10]))
        return "<table>%s%s</table>" % (head, "".join(body))

    out.append("<h2>Пакеты из HomeRepo · %d</h2>%s" % (len(by["homerepo"]), pk_table(by["homerepo"], True)))
    out.append("<h2>Установлено извне · %d</h2><div class='muted small'>сторонние репозитории и пакеты, поставленные вручную</div>%s"
               % (len(by["external"]), pk_table(by["external"], True)))
    if full:
        out.append("<h2>Пакеты из репозиториев ОС · %d</h2>%s" % (len(by["os"]), pk_table(by["os"], False)))
    else:
        out.append("<h2>Пакеты из репозиториев ОС · %d</h2><div class='muted noprint'>полный список — <a href='?full=1'>"
                   "отчёт с пакетами ОС</a></div>" % len(by["os"]))
    f = h.get("facts") or {}
    allrepos = f.get("repos") or []
    repos = [r for r in allrepos if r.get("enabled")]
    if allrepos:
        hr, osr = set(f.get("homerepo_ids") or []), set(f.get("os_repos") or [])
        out.append("<h2>Подключённые репозитории · %d</h2><table><tr><th>ID</th><th>Тип</th><th>Адрес</th></tr>%s</table>"
                   "<div class='muted small'>отключённых репозиториев: %d</div>"
                   % (len(repos), "".join("<tr><td>%s</td><td>%s</td><td class='small'>%s</td></tr>"
                                          % (e(r.get("id")), "homerepo" if r.get("id") in hr else "ОС" if r.get("id") in osr
                                             else "сторонний", e(r.get("baseurl"))) for r in repos),
                      len(allrepos) - len(repos)))
    if h["tasks"]:
        out.append("<h2>Назначения · последние %d</h2><table><tr><th>Создано</th><th>Действие</th><th>Пакет</th><th>Кем</th>"
                   "<th>Статус</th><th>Результат</th></tr>%s</table>" % (len(h["tasks"]), "".join(
                       "<tr><td>%s</td><td>%s</td><td>%s %s</td><td>%s</td><td>%s</td><td class='small'>%s</td></tr>"
                       % (dt(t["created"]), e(ACT_RU.get(t["action"], t["action"])), e(t["name"]), e(t.get("evr") or ""),
                          e(t.get("created_by")), e(TST_RU.get(t["status"], t["status"])),
                          e((t.get("output") or "").strip().split("\n")[0][:140])) for t in h["tasks"])))
    fn = "homerepo-%s-%s.html" % (h["name"], time.strftime("%Y%m%d-%H%M"))
    return page("Отчёт: %s" % name, "".join(out), fn), fn


def summary_html(con):
    cfg = web.APP.cfg
    hosts = inventory.host_summary(con, cfg)
    pk = con.execute("SELECT count(*), coalesce(sum(size),0), sum(sigclass='ours') FROM packages").fetchone()
    rows = []
    for h in hosts:
        m = h.get("metrics") or {}
        fresh = h.get("metrics_ts") and time.time() - h["metrics_ts"] < 900
        rows.append("<tr><td><b>%s</b><div class='muted small'>%s · %s</div></td><td>%s %s<div class='muted small'>%s</div></td>"
                    "<td>%s<div class='muted small'>%s</div></td><td class='num'>%s</td><td class='num'>%s</td><td class='num'>%s</td>"
                    "<td class='num'>%d</td><td class='num'>%d</td><td class='num'>%d</td><td class='num'>%s</td><td class='num'>%s</td></tr>"
                    % (e(h["name"]), e(h.get("fqdn")), e(h.get("ip")), e(h.get("os_name") or "?"), e(h.get("os_version") or ""),
                       e((h.get("hw") or {}).get("model") or h.get("arch")), e(ST_RU.get(h["status"], h["status"])),
                       dt(h.get("last_poll")), pct(m.get("cpu")) if fresh else "—", pct(m.get("mem_pct")) if fresh else "—",
                       pct(m.get("disk_pct")) if fresh else "—", h["n_homerepo"], h["n_external"], h["n_os"],
                       "—" if h["n_updates"] is None else h["n_updates"],
                       ("%d ждёт" % h["n_pending"] if h["n_pending"] else "") + (" %d ошибк." % h["n_errors"] if h["n_errors"] else "") or "—"))
    online = sum(1 for h in hosts if h["status"] == "online")
    body = ('<header><div><h1>Сводный отчёт по устройствам</h1><div class="muted">репозиторий %s</div></div>%s</header>'
            '<div class="grid"><div class="kpi"><b>%d</b><span>устройств</span></div><div class="kpi"><b>%d</b><span>на связи</span></div>'
            '<div class="kpi"><b>%d</b><span>обновлений доступно</span></div><div class="kpi"><b>%d</b><span>пакетов в HomeRepo · %s</span></div></div>'
            '<h2>Устройства</h2><table><tr><th>Устройство</th><th>Система</th><th>Статус</th><th class="num">CPU</th><th class="num">ОЗУ</th>'
            '<th class="num">Диск</th><th class="num">HomeRepo</th><th class="num">извне</th><th class="num">ОС</th>'
            '<th class="num">обновл.</th><th class="num">назнач.</th></tr>%s</table>'
            % (e(cfg.repo_url), buttons("/api/report/hosts.html?download=1"), len(hosts), online,
               sum(h["n_updates"] or 0 for h in hosts), pk[0], size(pk[1]), "".join(rows) or "<tr><td colspan=11>устройств нет</td></tr>"))
    fn = "homerepo-hosts-%s.html" % time.strftime("%Y%m%d-%H%M")
    return page("HomeRepo: сводный отчёт", body, fn), fn


def _resp(doc, fn, req):
    hdr = [("Cache-Control", "no-store")]
    if req.query.get("download"):
        hdr.append(("Content-Disposition", 'attachment; filename="%s"' % fn))
        # в сохранённом файле кнопки не нужны: печать — через меню браузера
        doc = re.sub(r'<div class="bar noprint">.*?</div>', "", doc, count=1, flags=re.S)
        doc = doc.replace('<script src="/report.js"></script>', "")
    return Resp(200, doc.encode("utf-8"), "text/html; charset=utf-8", hdr)


@route("GET", r"/api/report/host/(\d+)\.html")
def report_host(req, hid):
    doc, fn = host_html(web.C(), hid, full=bool(req.query.get("full")))
    web.audit(req, "report", "устройство #%s" % hid)
    return _resp(doc, fn, req)


@route("GET", r"/api/report/hosts\.html")
def report_hosts(req):
    doc, fn = summary_html(web.C())
    return _resp(doc, fn, req)
