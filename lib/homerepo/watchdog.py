"""Сторож связи с агентами: агент молчит дольше суток — предупреждение в Graylog (hr_event=agent_silent).

Фоновый поток службы панели (запускается из web.main) перепроверяет last_poll всех хостов раз в час;
когда хост вернулся — отправляет hr_event=agent_recovered. Каждый проход отправляет также пульс
hr_event=heartbeat со счётчиками устройств: по его отсутствию Graylog замечает, что молчит сама панель.
Состояние («по кому уже сообщили») лежит в settings, схема БД не меняется.
Пока хост молчит, тревога повторяется не чаще раза в сутки.

Ручной прогон для проверки: homerepo watchdog
"""
import logging
import time

from . import config, db, gelf

log = logging.getLogger("homerepo.watchdog")

SILENT_AFTER = 86400       # молчание дольше суток — потеря связи (как статус «offline» в инвентаре)
REMIND_EVERY = 86400       # повторное напоминание, пока хост не вернулся
CHECK_EVERY = 3600         # период фонового потока

_KEY = "watchdog.alerted"  # settings: {"<id хоста>": время последней тревоги}


def check(con, now=None):
    """Один проход по хостам. Возвращает (тревог отправлено, возвращений замечено)."""
    now = time.time() if now is None else now
    alerted = db.get_setting(con, _KEY, {}) or {}
    hosts = db.rows(con.execute("SELECT id, name, ip, enrolled, last_poll FROM hosts"))
    ids = {str(h["id"]) for h in hosts}
    sent, back = 0, 0
    for h in hosts:
        seen = h["last_poll"] or h["enrolled"] or now
        age = now - seen
        k = str(h["id"])
        if age > SILENT_AFTER:
            if now - alerted.get(k, 0) >= REMIND_EVERY:
                gelf.agent_silent(h["name"], h["ip"] or "", age, seen)
                alerted[k] = now
                sent += 1
        elif k in alerted:
            gelf.agent_recovered(h["name"], h["ip"] or "")
            del alerted[k]
            back += 1
    for k in list(alerted):      # хост удалён из панели — забыть и тревогу по нему
        if k not in ids:
            del alerted[k]
    db.set_setting(con, _KEY, alerted)
    silent = sum(1 for h in hosts
                 if now - (h["last_poll"] or h["enrolled"] or now) > SILENT_AFTER)
    gelf.heartbeat(len(hosts), silent)   # пульс панели: по его пропаже Graylog видит молчание панели
    return sent, back


def run_forever():
    """Фоновый цикл для службы панели. Без настроенного Graylog ([graylog] gelf_host) сразу завершается."""
    if gelf.sender() is None:
        log.info("watchdog: GELF не настроен — сторож выключен")
        return
    con = db.thread_con(config.get().db_path)
    log.info("watchdog: слежу за связью с агентами (тишина %d ч, проверка раз в %d мин)",
             SILENT_AFTER // 3600, CHECK_EVERY // 60)
    time.sleep(60)               # дать панели подняться; первый проход — через минуту после старта
    while True:
        try:
            sent, back = check(con)
            if sent or back:
                log.info("watchdog: тревог %d, возвращений %d", sent, back)
        except Exception as e:  # noqa: BLE001 — сторож не должен умирать из-за одного прохода
            log.warning("watchdog: проход не удался: %s", e)
        time.sleep(CHECK_EVERY)
