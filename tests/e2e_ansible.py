"""Сквозной тест раздела «Плейбуки»: конструктор, запуск через исполнитель, события, остановка, секреты, ошибки.

python3 e2e_ansible.py "<команда запуска агента>"

На тестовой машине хост с адресом 127.0.0.1 выполняется локально (ansible_connection=local), поэтому
SSH для теста не нужен. Плейбуки теста не используют sudo (become: false).
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from apiclient import Client  # noqa


def main():
    agent_cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    c = Client("http://127.0.0.1:8090")
    c.login("admin", "Tr0ub4dor&3-horse")
    ok = 0

    def check(cond, msg):
        nonlocal ok
        if not cond:
            raise AssertionError(msg)
        ok += 1
        print("  ✓", msg)

    def wait_run(rid, timeout=120):
        t0 = time.time()
        while time.time() - t0 < timeout:
            r = c.get("/api/runs/%d" % rid)["run"]
            if r["status"] in ("done", "failed", "cancelled"):
                return r
            time.sleep(0.5)
        raise AssertionError("запуск #%d не завершился" % rid)

    def events(rid):
        return c.get("/api/runs/%d" % rid)["events"]

    def logtext(rid):
        return "".join(x["text"] for x in c.get("/api/runs/%d" % rid)["log"])

    for _ in range(30):
        st = c.get("/api/ansible/status")
        if st["runner"]["alive"]:
            break
        time.sleep(0.5)
    check(st["runner"]["alive"] and st["runner"]["ansible"], "исполнитель на связи, Ansible %s" % st["runner"]["ansible"])
    check(st["runner"]["pubkey"].startswith("ssh-ed25519 "), "у исполнителя есть SSH-ключ")
    cat = c.get("/api/ansible/catalog")
    check(len(cat["blocks"]) >= 20 and "hr-hello" in cat["repo_packages"], "каталог блоков и пакеты HomeRepo для конструктора")
    rec = c.get("/api/ansible/recipes")["recipes"]
    check(any(r["id"] == "baseline" for r in rec), "рецепты на месте")
    r = c.post("/api/playbooks", {"recipe": "baseline"})
    check("community.general.timezone" in r["yaml"] and "homerepo-updates" in r["yaml"], "плейбук из рецепта собран в YAML")

    hid = [t for t in c.get("/api/ansible/targets")["targets"] if t["local"]][0]["id"]
    dest = "/tmp/hr-pb-test-%d.txt" % int(time.time())
    model = {"become": False, "gather_facts": False, "steps": [
        {"type": "file_content", "params": {"dest": dest, "content": "собрано для {{ inventory_hostname }}"}},
        {"type": "command", "params": {"cmd": "cat " + dest + " 2>/dev/null || true", "readonly": True}, "title": "Прочитать файл"},
        {"type": "debug", "params": {"msg": "готово"}, "only_os": "fedora"},
    ]}
    rr = c.post("/api/playbooks/render", {"model": model})
    check(not rr["errors"] and "ansible.builtin.copy" in rr["yaml"], "предпросмотр YAML без ошибок")
    pid = c.post("/api/playbooks", {"name": "Тест исполнителя", "model": model})["id"]

    rid = c.post("/api/runs", {"playbook_id": pid, "hosts": [hid], "mode": "check"})["run"]
    run = wait_run(rid)
    check(run["status"] == "done" and run["recap"] and "изменилось бы на 1" in run["message"],
          "проверка (--check) показывает будущие изменения: %s" % run["message"])
    rid = c.post("/api/runs", {"playbook_id": pid, "hosts": [hid], "mode": "run"})["run"]
    run = wait_run(rid)
    ev = events(rid)
    res = {e["task_name"]: e["status"] for e in ev if e["kind"] == "result"}
    check(run["status"] == "done" and res.get("Файл с содержимым") == "changed", "выполнение: файл создан (changed)")
    check(res.get("Прочитать файл") == "ok" and res.get("Сообщение") == "skipped", "команда ok, шаг «только Fedora» пропущен")
    out = [e for e in ev if e["kind"] == "result" and e["task_name"] == "Прочитать файл"][0]["data"]["data"]
    check("собрано для" in out.get("stdout", ""), "вывод задачи виден в событиях")
    check("PLAY RECAP" in logtext(rid), "текстовый вывод Ansible сохранён")
    rid2 = c.post("/api/runs", {"playbook_id": pid, "hosts": [hid], "mode": "run"})["run"]
    run2 = wait_run(rid2)
    check(run2["recap"] and all(v["changed"] == 0 for v in run2["recap"].values()), "повторный запуск ничего не меняет (идемпотентность)")

    # ошибка -> центр ошибок
    bad = c.post("/api/playbooks", {"name": "Тест ошибки", "model": {"become": False, "gather_facts": False, "steps": [
        {"type": "command", "params": {"cmd": "echo плохо >&2; exit 3"}}]}})["id"]
    rid = c.post("/api/runs", {"playbook_id": bad, "hosts": [hid], "mode": "run"})["run"]
    run = wait_run(rid)
    check(run["status"] == "failed" and "ошибками" in run["message"], "упавший плейбук: %s" % run["message"])
    errs = c.get("/api/errors")
    check(any(x["id"] == rid for x in errs["runs"]), "упавший запуск — в центре ошибок")
    c.post("/api/errors/ack", {"runs": [rid]})
    check(not any(x["id"] == rid for x in c.get("/api/errors")["runs"]), "ошибку запуска можно скрыть")

    # остановка
    slow = c.post("/api/playbooks", {"name": "Тест остановки", "model": {"become": False, "gather_facts": False, "steps": [
        {"type": "command", "params": {"cmd": "sleep 60"}}]}})["id"]
    rid = c.post("/api/runs", {"playbook_id": slow, "hosts": [hid], "mode": "run"})["run"]
    for _ in range(40):
        if c.get("/api/runs/%d" % rid)["run"]["status"] == "running" and events(rid):
            break
        time.sleep(0.5)
    t0 = time.time()
    c.post("/api/runs/%d/cancel" % rid)
    run = wait_run(rid, 40)
    check(run["status"] == "cancelled" and time.time() - t0 < 20, "запуск остановлен по кнопке")

    # секретные переменные
    sec = c.post("/api/playbooks", {"name": "Тест секрета", "model": {
        "become": False, "gather_facts": False, "secrets": [{"name": "hr_token", "label": "Токен"}],
        "steps": [{"type": "debug", "params": {"msg": "длина секрета {{ hr_token | length }}"}}]}})["id"]
    pb = c.get("/api/playbooks/%d" % sec)
    check([s["name"] for s in pb["secrets"]] == ["hr_token"], "плейбук сообщает, какие секреты спросить")
    try:
        c.post("/api/runs", {"playbook_id": sec, "hosts": [hid], "mode": "run"})
        check(False, "запуск без секрета должен отклоняться")
    except AssertionError as e:
        check("секретные" in str(e), "запуск без секрета отклонён")
    rid = c.post("/api/runs", {"playbook_id": sec, "hosts": [hid], "mode": "run",
                               "secrets": {"hr_token": "S3cr3t-Value-42"}})["run"]
    run = wait_run(rid)
    lt = logtext(rid)
    check(run["status"] == "done" and "длина секрета 15" in lt, "секрет передан в плейбук")
    check("S3cr3t-Value-42" not in lt and "S3cr3t" not in str(events(rid)), "значение секрета не попало ни в вывод, ни в события")

    inv = c.get("/api/ansible/inventory")
    check("homerepo" in inv and inv["_meta"]["hostvars"], "инвентарь для Ansible из HomeRepo")
    hist = c.get("/api/runs")["runs"]
    check(len(hist) >= 6 and hist[0]["hosts"], "история запусков")
    import urllib.parse
    log = c.get("/api/audit?q=" + urllib.parse.quote("плейбук"))["audit"]
    check(any(a["action"] == "run_start" for a in log), "запуски записаны в журнал человеческим языком")

    # доступ Ansible через агента
    if agent_cmd:
        import subprocess
        c.post("/api/ansible/access", {"hosts": [hid], "op": "grant"})
        t = [t for t in c.get("/api/ansible/targets")["targets"] if t["id"] == hid][0]
        check(t["access"]["state"] == "pending", "доступ Ansible назначен агенту")
        subprocess.run(agent_cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t = [t for t in c.get("/api/ansible/targets")["targets"] if t["id"] == hid][0]
        check(t["access"]["state"] == "ok", "агент выдал доступ: %s" % t["access"].get("msg", "")[:120])
        c.post("/api/ansible/access", {"hosts": [hid], "op": "revoke"})
        subprocess.run(agent_cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t = [t for t in c.get("/api/ansible/targets")["targets"] if t["id"] == hid][0]
        check(t["access"]["state"] == "none", "доступ отозван")
        # обновления ОС: агент сообщает, назначение «обновить систему» их ставит
        subprocess.run(agent_cmd.replace(" run", " report"), shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        h = c.get("/api/hosts/%d" % hid)
        check(h["os_updates"] is not None and "count" in h["os_updates"],
              "агент сообщает обновления ОС: %s" % h["os_updates"]["count"])
        r = c.post("/api/tasks", {"action": "upgrade", "hosts": [hid], "schedule": "now"})
        check(r["created"] == 1, "назначение «обновить систему» создано")
        subprocess.run(agent_cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t = [t for t in c.get("/api/tasks")["tasks"] if t["action"] == "upgrade"][0]
        check(t["status"] in ("done", "error") and t["output"], "обновление системы выполнено агентом: %s" %
              (t["output"] or "").splitlines()[0][:100])
    print("пройдено проверок: %d" % ok)


if __name__ == "__main__":
    main()
