"""Мини-клиент API панели для сквозных тестов."""

import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.request


class Client:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        self.csrf = ""

    def req(self, method, path, data=None, body=None, ctype="application/json", ok=(200,)):
        h = {"X-CSRF": self.csrf}
        if data is not None:
            body = json.dumps(data).encode()
        if body is not None:
            h["Content-Type"] = ctype
        r = urllib.request.Request(self.base + path, data=body, headers=h, method=method)
        try:
            with self.op.open(r, timeout=120) as resp:
                raw = resp.read()
                code = resp.status
        except urllib.error.HTTPError as e:
            raw = e.read()
            code = e.code
        try:
            js = json.loads(raw.decode() or "{}")
        except ValueError:
            js = {"raw": raw.decode("utf-8", "replace")}
        if code not in ok:
            raise AssertionError("%s %s -> %s %s" % (method, path, code, js))
        return js

    def get(self, p, **kw):
        return self.req("GET", p, **kw)

    def post(self, p, d=None, **kw):
        return self.req("POST", p, data=d if d is not None else {}, **kw)

    def login(self, user, pw):
        r = self.post("/api/login", {"user": user, "password": pw})
        self.csrf = self.get("/api/me")["csrf"]
        return r

    def upload(self, path, branch="updates", sign=True, keep=0, chunk=None):
        size = os.path.getsize(path)
        st = self.post("/api/uploads", {"filename": os.path.basename(path), "size": size})
        ch = chunk or st["chunk"]
        off = 0
        with open(path, "rb") as f:
            while off < size:
                b = f.read(ch)
                r = self.req("PUT", "/api/uploads/%s?offset=%d" % (st["id"], off), body=b,
                             ctype="application/octet-stream")
                off = r["received"]
        return self.post("/api/uploads/%s/finish" % st["id"], {"branch": branch, "sign": sign, "keep": keep})["job"]

    def wait_job(self, jid, timeout=120):
        t0 = time.time()
        while time.time() - t0 < timeout:
            for j in self.get("/api/jobs?limit=100")["jobs"]:
                if j["id"] == jid and j["status"] in ("done", "error", "cancelled"):
                    return j
            time.sleep(0.5)
        raise AssertionError("задание #%d не завершилось" % jid)
