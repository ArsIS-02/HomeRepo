"""Сквозной тест сервера: вход, загрузка, подпись, перемещение, удаление, по ссылке.

python3 e2e_backend.py <временный пароль admin> <каталог с тестовыми RPM> [URL панели]
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from apiclient import Client  # noqa

PW = "Tr0ub4dor&3-horse"


def main():
    tmp, out = sys.argv[1], sys.argv[2]
    base = sys.argv[3] if len(sys.argv) > 3 else "http://127.0.0.1:8090"
    c = Client(base)
    r = c.login("admin", tmp)
    assert r["must_change"]
    c.post("/api/password", {"old": tmp, "new": PW})
    ok = 0

    def check(cond, msg):
        nonlocal ok
        if not cond:
            raise AssertionError(msg)
        ok += 1
        print("  ✓", msg)

    j1 = c.upload(os.path.join(out, "hr-hello-1.0.0-1.x86_64.rpm"), "updates", True, chunk=700)
    j2 = c.upload(os.path.join(out, "hr-common-2.0.0-1.noarch.rpm"), "extras", False)
    j3 = c.upload(os.path.join(out, "hr-hello-1.1.0-1.aarch64.rpm"), "updates", True)
    for j in (j1, j2, j3):
        check(c.wait_job(j)["status"] == "done", "загрузка #%d" % j)
    pk = {p["filename"]: p for p in c.get("/api/packages")["packages"]}
    check(pk["hr-hello-1.0.0-1.x86_64.rpm"]["sigclass"] == "ours", "загруженный пакет подписан ключом homerepo")
    check(pk["hr-common-2.0.0-1.noarch.rpm"]["sigclass"] == "foreign", "без подписи — сохранена чужая подпись")
    check(pk["hr-common-2.0.0-1.noarch.rpm"]["arches"] == "x86_64 aarch64", "noarch опубликован в обе архитектуры")

    jf = c.post("/api/fetch", {"url": base.replace("8090", "8099") + "/hr-app-3.0.0-1.x86_64.rpm", "branch": "extras",
                               "sign": False})["job"]
    check(c.wait_job(jf)["status"] == "done", "скачивание по ссылке")
    jb = c.post("/api/fetch", {"url": base.replace("8090", "8099") + "/", "branch": "extras"})["job"]
    check(c.wait_job(jb)["status"] == "error", "ссылка не на RPM отклонена")

    jr = c.post("/api/packages/action", {"op": "resign_foreign"})["job"]
    check(c.wait_job(jr)["status"] == "done", "переподпись чужих")
    s = c.get("/api/summary")
    check(s["sig"]["foreign"] == 0 and s["sig"]["none"] == 0, "все пакеты подписаны ключом homerepo")

    j = c.upload(os.path.join(out, "hr-hello-1.1.0-1.x86_64.rpm"), "updates", True, keep=0)
    c.wait_job(j)
    pk = {p["filename"]: p for p in c.get("/api/packages")["packages"]}
    check(not pk["hr-hello-1.0.0-1.x86_64.rpm"]["latest"] and pk["hr-hello-1.1.0-1.x86_64.rpm"]["latest"],
          "старая версия помечена как не последняя")
    app = pk["hr-app-3.0.0-1.x86_64.rpm"]
    j = c.post("/api/packages/action", {"op": "move", "ids": [app["id"]], "to": "updates"})["job"]
    check(c.wait_job(j)["status"] == "done", "перемещение в другую ветку")
    pk = {(p["branch"], p["filename"]): p for p in c.get("/api/packages")["packages"]}
    check(("updates", "hr-app-3.0.0-1.x86_64.rpm") in pk, "пакет теперь в updates")
    st = c.get("/api/key")
    check(all(v.get("signed") for v in st["repo_state"].values()), "repomd.xml всех веток подписаны")
    print("пройдено проверок: %d" % ok)


if __name__ == "__main__":
    main()
