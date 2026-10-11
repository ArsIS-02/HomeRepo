# -*- coding: utf-8 -*-
"""Плагин вывода Ansible для HomeRepo: пишет события плейбука построчно в JSON (файл из $HR_EVENTS).

Исполнитель читает файл по мере записи и показывает в панели сетку «хосты × задачи» в реальном времени.
Работает с ansible-core 2.12+ и не требует сторонних пакетов. Результаты задач с no_log Ansible
передаёт сюда уже скрытыми — пароли в события не попадают.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = """
    name: homerepo_events
    type: notification
    short_description: события выполнения для панели HomeRepo
    description:
      - Пишет события плейбука (задачи, результаты по хостам, итог) в JSON Lines.
    requirements:
      - переменная окружения HR_EVENTS с путём к файлу
"""

import json
import os
import time

from ansible.plugins.callback import CallbackBase

LIMIT = 6000


def _cut(v, n=LIMIT):
    if v is None:
        return None
    if not isinstance(v, str):
        try:
            v = json.dumps(v, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            v = str(v)
    return v if len(v) <= n else v[:n] + "\n… (обрезано)"


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "notification"
    CALLBACK_NAME = "homerepo_events"
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self, *args, **kwargs):
        super(CallbackModule, self).__init__(*args, **kwargs)
        path = os.environ.get("HR_EVENTS")
        self._f = open(path, "a", encoding="utf-8") if path else None
        self._order = 0

    def _emit(self, kind, **kw):
        if not self._f:
            return
        kw["kind"] = kind
        kw["ts"] = time.time()
        self._f.write(json.dumps(kw, ensure_ascii=False, default=str) + "\n")
        self._f.flush()

    # ---------------------------------------------------------------- ход плейбука
    def v2_playbook_on_start(self, playbook):
        self._emit("playbook_start", name=os.path.basename(getattr(playbook, "_file_name", "") or ""))

    def v2_playbook_on_play_start(self, play):
        self._emit("play_start", name=play.get_name().strip(), hosts=str(getattr(play, "hosts", "")))

    def _task_start(self, task, handler=False):
        self._order += 1
        self._emit("task_start", task=str(task._uuid), name=task.get_name().strip(), action=task.action,
                   order=self._order, handler=handler)

    def v2_playbook_on_task_start(self, task, is_conditional):
        self._task_start(task)

    def v2_playbook_on_handler_task_start(self, task):
        self._task_start(task, handler=True)

    def v2_playbook_on_no_hosts_matched(self):
        self._emit("no_hosts", msg="ни один хост не подошёл под шаблон hosts")

    def v2_playbook_on_no_hosts_remaining(self):
        self._emit("no_hosts_remaining", msg="на всех хостах произошли ошибки — выполнение остановлено")

    # ---------------------------------------------------------------- результаты
    def _result(self, result, status):
        r = result._result if isinstance(result._result, dict) else {}
        data = {}
        if r.get("_ansible_no_log") or r.get("censored"):
            data["msg"] = "вывод скрыт (no_log)"
        else:
            for k in ("msg", "stdout", "stderr", "rc", "cmd", "reason"):
                if r.get(k) not in (None, "", []):
                    data[k] = r[k] if isinstance(r[k], (int, float, bool)) else _cut(r[k])
            if r.get("results") and isinstance(r["results"], list):
                items = []
                for it in r["results"][:50]:
                    if isinstance(it, dict):
                        st = "failed" if it.get("failed") else "changed" if it.get("changed") else \
                            "skipped" if it.get("skipped") else "ok"
                        items.append({"item": _cut(it.get("item"), 200), "status": st,
                                      "msg": _cut(it.get("msg"), 500)})
                data["items"] = items
            diff = r.get("diff")
            if diff:
                try:
                    data["diff"] = _cut(self._get_diff(diff))
                except Exception:  # noqa: BLE001
                    pass
        delegated = r.get("_ansible_delegated_vars", {}).get("ansible_host") if isinstance(r.get("_ansible_delegated_vars"), dict) else None
        self._emit("result", host=result._host.get_name(), task=str(result._task._uuid),
                   name=result._task.get_name().strip(), status=status, data=data, delegated=delegated,
                   ignored=bool(getattr(result, "_ignore_errors", False) or r.get("_ansible_ignore_errors")))

    def v2_runner_on_ok(self, result):
        self._result(result, "changed" if result._result.get("changed") else "ok")

    def v2_runner_on_failed(self, result, ignore_errors=False):
        self._result(result, "ignored" if ignore_errors else "failed")

    def v2_runner_on_skipped(self, result):
        self._result(result, "skipped")

    def v2_runner_on_unreachable(self, result):
        self._result(result, "unreachable")

    # ---------------------------------------------------------------- итог
    def v2_playbook_on_stats(self, stats):
        recap = {}
        for h in sorted(stats.processed.keys()):
            s = stats.summarize(h)
            recap[h] = {k: s.get(k, 0) for k in ("ok", "changed", "failures", "unreachable", "skipped", "rescued", "ignored")}
        self._emit("stats", recap=recap)
        if self._f:
            self._f.close()
            self._f = None
