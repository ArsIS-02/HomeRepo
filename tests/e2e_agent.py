"""Сквозной тест устройств: отчёт агента, назначения (обновление, удаление, установка), забор пакета «извне».

python3 e2e_agent.py <каталог с тестовыми RPM> <команда запуска агента>
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
from apiclient import Client  # noqa


def main():
    out, agent_cmd = sys.argv[1], sys.argv[2]
    c = Client("http://127.0.0.1:8090")
    c.login("admin", "Tr0ub4dor&3-horse")
    ok = 0

    def check(cond, msg):
        nonlocal ok
        if not cond:
            raise AssertionError(msg)
        ok += 1
        print("  ✓", msg)

    def agent():
        p = subprocess.run(agent_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        print("    agent:", p.stdout.decode().strip().replace("\n", "\n    agent: "))

    hid = c.get("/api/hosts")["hosts"][0]["id"]
    c.post("/api/hosts/%d/report" % hid)
    agent()
    h = c.get("/api/hosts/%d" % hid)
    ext = [p for p in h["packages"] if p["cls"] == "external"]
    check([p["name"] for p in ext] == ["hr-extpkg"], "пакет из стороннего репозитория определён как «извне»")
    check(ext[0]["from_repo"] == "ext", "источник «извне» — репозиторий ext")

    # новая версия в homerepo -> обновление
    j = c.upload(os.path.join(out, "hr-hello-1.2.0-1.x86_64.rpm"), "updates")
    check(c.wait_job(j)["status"] == "done", "загружен hr-hello 1.2")
    h = c.get("/api/hosts/%d" % hid)
    check([u["name"] for u in h["updates"]] == ["hr-hello"], "панель видит доступное обновление hr-hello")
    r = c.post("/api/tasks", {"action": "update", "items": [{"name": "hr-hello"}], "hosts": [hid], "schedule": "now"})
    check(r["created"] == 1, "назначение «обновить» создано")
    r = c.post("/api/tasks", {"action": "remove", "items": [{"name": "hr-app"}], "hosts": [hid], "schedule": "now"})
    r = c.post("/api/tasks", {"action": "install", "items": [{"name": "hr-hello", "evr": "9.9-1"}], "hosts": [hid]})
    check(r["created"] == 0 and r["skipped"], "несуществующая версия не назначается")
    r = c.post("/api/tasks", {"action": "install", "items": [{"name": "hr-hello"}], "hosts": [hid],
                              "schedule": "night"})
    agent()
    tasks = {(t["action"], t["name"]): t for t in c.get("/api/tasks")["tasks"]}
    check(tasks[("update", "hr-hello")]["status"] == "done", "обновление выполнено: " +
          (tasks[("update", "hr-hello")]["output"] or "").splitlines()[0])
    check(tasks[("remove", "hr-app")]["status"] == "done", "удаление выполнено")
    check(tasks[("install", "hr-hello")]["status"] == "pending", "ночное назначение ждёт своего окна")
    c.post("/api/tasks/%d/cancel" % tasks[("install", "hr-hello")]["id"])
    h = c.get("/api/hosts/%d" % hid)
    names = {p["name"]: p for p in h["packages"]}
    check(names["hr-hello"]["evr"] == "1.2.0-1", "на хосте теперь hr-hello 1.2.0-1")
    check("hr-app" not in names, "hr-app удалён с хоста")
    check(not h["updates"], "обновлений больше нет")

    r = c.post("/api/tasks", {"action": "install", "items": [{"name": "hr-app"}], "hosts": [hid]})
    agent()
    t = [t for t in c.get("/api/tasks")["tasks"] if t["name"] == "hr-app" and t["action"] == "install"][0]
    check(t["status"] == "done", "установка hr-app по назначению (с зависимостью)")

    # забрать пакет «извне» в homerepo
    c.post("/api/hosts/%d/collect" % hid, {"name": "hr-extpkg", "arch": "x86_64", "branch": "extras"})
    agent()
    import time
    for _ in range(60):
        t = [t for t in c.get("/api/tasks")["tasks"] if t["action"] == "collect"][0]
        if t["status"] in ("done", "error"):
            break
        time.sleep(0.5)
    print("    collect:", t["status"], (t["output"] or "")[:200])
    check(t["status"] == "done" and "опубликован" in (t["output"] or ""),
          "пакет с хоста прошёл службу подписи, назначение закрыто с итогом публикации")
    for _ in range(60):
        pk = [p for p in c.get("/api/packages")["packages"] if p["name"] == "hr-extpkg"]
        if pk:
            break
        time.sleep(0.5)
    check(pk and pk[0]["sigclass"] == "ours" and pk[0]["branch"] == "extras", "hr-extpkg теперь в homerepo/extras и подписан")

    # перевести на homerepo: та же версия, переустановка из нашего репозитория
    for _ in range(20):
        if c.get("/api/key")["dirty"] == []:
            break
        time.sleep(0.5)
    r = c.post("/api/tasks", {"action": "install", "items": [{"name": "hr-extpkg", "switch": True}], "hosts": [hid]})
    check(r["created"] == 1, "назначение «перевести» создано")
    agent()
    t = [t for t in c.get("/api/tasks")["tasks"] if t["name"] == "hr-extpkg" and t["action"] == "install"][0]
    print("    switch:", t["status"], (t["output"] or "")[:120].replace("\n", " "))
    h = c.get("/api/hosts/%d" % hid)
    p = [p for p in h["packages"] if p["name"] == "hr-extpkg"][0]
    check(p["cls"] == "homerepo" and p["from_repo"] == "homerepo-extras", "hr-extpkg теперь установлен из homerepo")
    print("пройдено проверок: %d" % ok)


if __name__ == "__main__":
    main()
