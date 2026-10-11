"""Служба подписи homerepo-signer.

Единственный процесс, у которого есть доступ к секретному ключу и право записи в дерево
репозитория. Панель только ставит задания в таблицу jobs; служба их выполняет:
проверка RPM -> подпись -> публикация -> метаданные (createrepo_c) -> подпись repomd.xml.
"""

import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time

from . import VERSION, config, db, gpgutil, rpmutil

log = logging.getLogger("homerepo.signer")

TMP_PREFIX = ".hr-tmp-"
STEPS_ADD = ["check", "sign", "publish", "metadata"]


class JobError(Exception):
    pass


class Signer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.con = db.init(cfg.db_path)
        self.key = None
        self.our_keys = set()
        self.stop = False
        self.last_scan = 0
        self.waiting_md = []  # задания, ждущие пересборки метаданных

    # ------------------------------------------------------------ служебное
    def heartbeat(self, state="idle"):
        db.set_setting(self.con, "signer", {"ts": time.time(), "state": state, "version": VERSION, "pid": os.getpid()})

    def dirty(self):
        return set(db.get_setting(self.con, "dirty_dirs", []) or [])

    def mark_dirty(self, dirs):
        cur = self.dirty() | set(dirs)
        db.set_setting(self.con, "dirty_dirs", sorted(cur))

    def clear_dirty(self, dirs):
        cur = self.dirty() - set(dirs)
        db.set_setting(self.con, "dirty_dirs", sorted(cur))

    def set_step(self, job_id, step, message=None, progress=None):
        self.con.execute("UPDATE jobs SET step=?, message=COALESCE(?, message), progress=? WHERE id=?",
                         (step, message, progress, job_id))

    def finish(self, job_id, status, message=""):
        self.con.execute("UPDATE jobs SET status=?, message=?, finished=?, progress=NULL WHERE id=?",
                         (status, message, time.time(), job_id))
        # пакет, забранный с хоста: итог публикации — в назначение
        r = self.con.execute("SELECT params FROM jobs WHERE id=?", (job_id,)).fetchone()
        try:
            task = json.loads(r["params"] or "{}").get("task") if r else None
        except ValueError:
            task = None
        if task:
            tr = self.con.execute("SELECT output FROM tasks WHERE id=?", (task,)).fetchone()
            prev = (tr["output"] or "") if tr else ""
            note = prev.split("пакет получен", 1)[0] if "точной версии" in prev else ""
            if status == "done":
                out = "опубликован в HomeRepo: %s" % (message or "")
            else:
                out = "служба подписи не приняла пакет: %s" % (message or "")
            self.con.execute("UPDATE tasks SET status=?, output=?, finished=? WHERE id=? AND status='processing'",
                             ("done" if status == "done" else "error", note + out, time.time(), task))

    # ------------------------------------------------------------ ключ
    def refresh_key(self):
        c = self.cfg
        info = None
        err = ""
        if not c.key_id:
            err = "в /etc/homerepo/homerepo.conf не задан signer.key_id"
        elif not os.path.isdir(c.gnupg_home):
            err = "нет каталога ключей %s" % c.gnupg_home
        else:
            info = gpgutil.key_info(c.gnupg_home, c.key_id)
            if not info:
                err = "секретный ключ %s не найден в %s" % (c.key_id, c.gnupg_home)
            elif not info["can_sign"]:
                err = "ключ не может подписывать"
        if info:
            self.our_keys = set(info["keyids"])
            self.key = info
            try:
                pub = gpgutil.export_public(c.gnupg_home, c.key_id)
                dst = os.path.join(c.repo_root, "RPM-GPG-KEY-HOMEREPO")
                old = None
                if os.path.exists(dst):
                    with open(dst, "rb") as f:
                        old = f.read()
                if old != pub:
                    self._write_file(dst, pub)
                    log.info("опубликован открытый ключ %s", dst)
            except Exception as e:  # noqa
                err = str(e)
        else:
            self.key = None
        state = dict(info or {}, error=err, gnupg_home=c.gnupg_home, checked=time.time())
        db.set_setting(self.con, "key", state)
        if err:
            log.error("ключ: %s", err)
        return not err

    def _write_file(self, dst, data):
        tmp = os.path.join(os.path.dirname(dst), TMP_PREFIX + os.path.basename(dst))
        with open(tmp, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, dst)

    # ------------------------------------------------------------ подпись
    def sign_file(self, path):
        if not self.key:
            if not self.refresh_key():
                raise JobError("ключ подписи недоступен — см. раздел «Подпись»")
        import rpm

        c = self.cfg
        os.environ["GNUPGHOME"] = c.gnupg_home
        rpm.addMacro("_gpg_path", c.gnupg_home)
        rpm.addMacro("_gpg_name", c.key_id)
        if c.passphrase_file:
            rpm.addMacro("_gpg_sign_cmd_extra_args", "--pinentry-mode loopback --passphrase-file %s" % c.passphrase_file)
        try:
            rpm.delSign(path)
        except Exception as e:  # noqa
            log.warning("delsign %s: %s", path, e)
        ok = rpm.addSign(path, c.key_id)
        h = rpmutil.read_header(path)
        if not ok or h["sigkey"] not in self.our_keys:
            raise JobError("подпись не применилась (gpg вернул ошибку, подробности в journalctl -u homerepo-signer)")
        return h

    # ------------------------------------------------------------ публикация
    def ensure_tree(self):
        c = self.cfg
        for a in c.arches:
            for b in c.branches:
                os.makedirs(c.branch_dir(a, b), exist_ok=True)

    def check_space(self, d, need):
        free = shutil.disk_usage(d).free
        if free < need + 200 * 1024 * 1024:
            raise JobError("мало места на диске: свободно %d МБ, нужно %d МБ" % (free >> 20, (need >> 20) + 200))

    def publish(self, src_tmp, branch, filename, arches):
        """src_tmp — подписанный временный файл в каталоге первой архитектуры. Атомарная замена
        и жёсткие ссылки в каталоги остальных архитектур (noarch)."""
        c = self.cfg
        dirs = []
        first = None
        for i, a in enumerate(arches):
            d = c.branch_dir(a, branch)
            final = os.path.join(d, filename)
            if i == 0:
                os.chmod(src_tmp, 0o644)
                os.replace(src_tmp, final)
                first = final
            else:
                tmp = os.path.join(d, TMP_PREFIX + filename + ".part")
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                try:
                    os.link(first, tmp)
                except OSError:
                    shutil.copyfile(first, tmp)
                    os.chmod(tmp, 0o644)
                os.replace(tmp, final)
            dirs.append(d)
        return dirs

    def upsert_package(self, branch, filename, arches, h=None):
        c = self.cfg
        p = os.path.join(c.branch_dir(arches[0], branch), filename)
        st = os.stat(p)
        if h is None:
            h = rpmutil.read_header(p)
        sigclass = rpmutil.classify_sig(h["sigkey"], self.our_keys)
        now = time.time()
        self.con.execute(
            """INSERT INTO packages(branch, filename, name, epoch, version, release, arch, arches, size, mtime,
                   buildtime, summary, sigkey, sigclass, added)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(branch, filename) DO UPDATE SET name=excluded.name, epoch=excluded.epoch,
                   version=excluded.version, release=excluded.release, arch=excluded.arch, arches=excluded.arches,
                   size=excluded.size, mtime=excluded.mtime, buildtime=excluded.buildtime, summary=excluded.summary,
                   sigkey=excluded.sigkey, sigclass=excluded.sigclass""",
            (branch, filename, h["name"], h["epoch"], h["version"], h["release"], h["arch"], " ".join(arches),
             st.st_size, st.st_mtime, h["buildtime"], h["summary"], h["sigkey"], sigclass, now),
        )

    def remove_files(self, row):
        c = self.cfg
        dirs = []
        for a in row["arches"].split():
            d = c.branch_dir(a, row["branch"])
            try:
                os.unlink(os.path.join(d, row["filename"]))
            except FileNotFoundError:
                pass
            dirs.append(d)
        self.con.execute("DELETE FROM packages WHERE id=?", (row["id"],))
        return dirs

    # ------------------------------------------------------------ задания
    def job_add(self, job, p):
        c = self.cfg
        src = p["path"]
        branch = p.get("branch") or c.default_branch
        if branch not in c.branches:
            raise JobError("неизвестная ветка %s" % branch)
        inc = os.path.realpath(c.incoming_dir) + os.sep
        if not os.path.realpath(src).startswith(inc) or not os.path.isfile(src):
            raise JobError("файл задания не найден во входящих")
        try:
            self.set_step(job["id"], "check", "проверка RPM")
            try:
                h = rpmutil.read_header(src)
            except ValueError as e:
                raise JobError("это не RPM-пакет или файл повреждён (%s)" % e)
            if h["source"]:
                raise JobError("пакеты с исходниками (.src.rpm) не публикуются")
            ok, out = rpmutil.check_digests(src)
            if not ok:
                raise JobError("контрольные суммы не сходятся — файл повреждён или загружен не полностью: %s" % out)
            arches = c.arch_dirs(h["arch"])
            if not arches:
                raise JobError("архитектура %s не обслуживается репозиторием (%s)" % (h["arch"], ", ".join(c.arches)))
            filename = rpmutil.canonical_filename(h)
            self.ensure_tree()
            d0 = c.branch_dir(arches[0], branch)
            size = os.path.getsize(src)
            self.check_space(d0, size)
            tmp = os.path.join(d0, "%s%d.part" % (TMP_PREFIX, job["id"]))
            shutil.copyfile(src, tmp)
            try:
                if p.get("sign", True):
                    self.set_step(job["id"], "sign", "подпись ключом %s" % (c.key_id[-8:] or "?"))
                    h = self.sign_file(tmp)
                self.set_step(job["id"], "publish", "публикация в %s" % ", ".join("%s/%s" % (a, branch) for a in arches))
                # тот же NEVRA в этой ветке под другим именем файла — заменяем
                dup = self.con.execute(
                    "SELECT * FROM packages WHERE branch=? AND name=? AND epoch=? AND version=? AND release=? AND arch=? "
                    "AND filename<>?", (branch, h["name"], h["epoch"], h["version"], h["release"], h["arch"], filename)
                ).fetchall()
                replaced = os.path.exists(os.path.join(d0, filename))
                dirs = self.publish(tmp, branch, filename, arches)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            for r in dup:
                dirs += self.remove_files(r)
            self.upsert_package(branch, filename, arches, h)
            removed = []
            keep = int(p.get("keep") or 0)
            if keep > 0:
                removed = self.prune_versions(branch, h["name"], h["arch"], keep)
                for r in removed:
                    dirs += self.remove_files(r)
            self.mark_dirty(dirs)
            msg = "%s → %s" % (filename, branch)
            if replaced or dup:
                msg += " (заменён)"
            if removed:
                msg += "; удалены старые версии: " + ", ".join(r["filename"] for r in removed)
            return msg
        finally:
            try:
                os.unlink(src)
            except OSError:
                pass

    def prune_versions(self, branch, name, arch, keep):
        rs = [dict(r) for r in self.con.execute(
            "SELECT * FROM packages WHERE branch=? AND name=? AND arch=?", (branch, name, arch))]
        import functools

        rs.sort(key=functools.cmp_to_key(
            lambda a, b: rpmutil.evr_cmp(a["epoch"], a["version"], a["release"], b["epoch"], b["version"], b["release"])),
            reverse=True)
        return rs[keep + 1:]

    def _rows(self, ids):
        if not ids:
            return []
        q = "SELECT * FROM packages WHERE id IN (%s)" % ",".join("?" * len(ids))
        return [dict(r) for r in self.con.execute(q, [int(i) for i in ids])]

    def job_resign(self, job, p):
        c = self.cfg
        rows = self._rows(p.get("ids") or [])
        if p.get("all_foreign"):
            rows = [dict(r) for r in self.con.execute("SELECT * FROM packages WHERE sigclass<>'ours' ORDER BY name")]
        if not rows:
            return "нечего подписывать"
        done = []
        dirs = []
        for n, r in enumerate(rows):
            self.set_step(job["id"], "sign", "%s (%d из %d)" % (r["filename"], n + 1, len(rows)), (n / len(rows)))
            arches = r["arches"].split()
            d0 = c.branch_dir(arches[0], r["branch"])
            src = os.path.join(d0, r["filename"])
            if not os.path.exists(src):
                continue
            self.check_space(d0, os.path.getsize(src))
            tmp = os.path.join(d0, "%s%d.part" % (TMP_PREFIX, job["id"]))
            shutil.copyfile(src, tmp)
            try:
                h = self.sign_file(tmp)
                self.set_step(job["id"], "publish")
                dirs += self.publish(tmp, r["branch"], r["filename"], arches)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            self.upsert_package(r["branch"], r["filename"], arches, h)
            done.append(r["filename"])
        self.mark_dirty(dirs)
        return "переподписано: %d" % len(done)

    def job_delete(self, job, p):
        rows = self._rows(p.get("ids") or [])
        dirs = []
        for r in rows:
            dirs += self.remove_files(r)
        self.mark_dirty(dirs)
        return "удалено: %s" % ", ".join(r["filename"] for r in rows) if rows else "нечего удалять"

    def job_move(self, job, p):
        c = self.cfg
        to = p.get("to")
        if to not in c.branches:
            raise JobError("неизвестная ветка %s" % to)
        self.ensure_tree()
        rows = self._rows(p.get("ids") or [])
        dirs = []
        moved = []
        for r in rows:
            if r["branch"] == to:
                continue
            arches = r["arches"].split()
            first = os.path.join(c.branch_dir(arches[0], r["branch"]), r["filename"])
            for a in arches:
                d = c.branch_dir(a, to)
                tmp = os.path.join(d, TMP_PREFIX + r["filename"] + ".part")
                try:
                    os.link(first, tmp)
                except OSError:
                    shutil.copyfile(first, tmp)
                os.replace(tmp, os.path.join(d, r["filename"]))
                dirs.append(d)
            old = self.con.execute("SELECT id FROM packages WHERE branch=? AND filename=?", (to, r["filename"])).fetchone()
            if old:
                self.con.execute("DELETE FROM packages WHERE id=?", (old["id"],))
            dirs += self.remove_files(r)
            self.upsert_package(to, r["filename"], arches)
            moved.append(r["filename"])
        self.mark_dirty(dirs)
        return "перемещено в %s: %d" % (to, len(moved))

    def job_metadata(self, job, p):
        c = self.cfg
        self.mark_dirty([c.branch_dir(a, b) for a in c.arches for b in c.branches])
        return None  # завершится после пересборки

    def job_rescan(self, job, p):
        n = self.scan(force=True)
        return "просканировано пакетов: %d" % n

    def job_cleanup(self, job, p):
        c = self.cfg
        root = os.path.realpath(c.repo_root) + os.sep
        removed = []
        for r in self.con.execute("SELECT * FROM junk").fetchall():
            path = os.path.realpath(r["path"])
            if not path.startswith(root):
                continue
            try:
                if r["kind"] == "dir":
                    shutil.rmtree(path)
                elif not path.endswith(".rpm"):
                    os.unlink(path)
                else:
                    continue
                removed.append(os.path.relpath(path, c.repo_root))
            except FileNotFoundError:
                pass
        self.con.execute("DELETE FROM junk")
        self.scan(force=True)
        return "удалено: %d" % len(removed) + (" (%s)" % ", ".join(removed[:20]) if removed else "")

    def job_key(self, job, p):
        ok = self.refresh_key()
        if ok:
            self.scan(force=True)
            return "ключ проверен: %s" % self.key["fpr"]
        raise JobError(db.get_setting(self.con, "key", {}).get("error") or "ключ недоступен")

    HANDLERS = {
        "add": job_add, "resign": job_resign, "delete": job_delete, "move": job_move,
        "metadata": job_metadata, "rescan": job_rescan, "cleanup": job_cleanup, "key": job_key,
    }

    def claim(self):
        with db.tx(self.con):
            r = self.con.execute(
                "SELECT * FROM jobs WHERE status='queued' AND worker='signer' ORDER BY id LIMIT 1").fetchone()
            if not r:
                return None
            self.con.execute("UPDATE jobs SET status='running', started=? WHERE id=?", (time.time(), r["id"]))
            return dict(r)

    def process(self, job):
        p = json.loads(job["params"] or "{}")
        h = self.HANDLERS.get(job["op"])
        log.info("задание #%d %s %s", job["id"], job["op"], job.get("title") or "")
        self.heartbeat("job #%d" % job["id"])
        try:
            if not h:
                raise JobError("неизвестная операция %s" % job["op"])
            msg = h(self, job, p)
        except JobError as e:
            self.finish(job["id"], "error", str(e))
            log.warning("задание #%d: %s", job["id"], e)
            self.cleanup_incoming(p)
            return
        except Exception as e:  # noqa
            log.exception("задание #%d упало", job["id"])
            self.finish(job["id"], "error", "внутренняя ошибка: %s" % e)
            self.cleanup_incoming(p)
            return
        if self.dirty():
            self.set_step(job["id"], "metadata", msg)
            self.waiting_md.append((job["id"], msg))
        else:
            self.finish(job["id"], "done", msg or "готово")

    def cleanup_incoming(self, p):
        path = p.get("path")
        if path and os.path.realpath(path).startswith(os.path.realpath(self.cfg.incoming_dir) + os.sep):
            try:
                os.unlink(path)
            except OSError:
                pass

    # ------------------------------------------------------------ метаданные
    def fingerprint(self, d):
        h = hashlib.sha1()
        try:
            names = sorted(n for n in os.listdir(d) if n.endswith(".rpm") and not n.startswith("."))
        except FileNotFoundError:
            return ""
        for n in names:
            st = os.stat(os.path.join(d, n))
            h.update(("%s|%d|%d\n" % (n, st.st_size, int(st.st_mtime))).encode())
        return h.hexdigest()

    def rebuild_metadata(self):
        c = self.cfg
        dirs = sorted(self.dirty())
        if not dirs:
            return
        state = db.get_setting(self.con, "repo_state", {}) or {}
        errors = []
        for d in dirs:
            self.heartbeat("metadata %s" % d)
            stale = os.path.join(d, ".repodata")
            if os.path.isdir(stale):
                shutil.rmtree(stale, ignore_errors=True)
            os.makedirs(d, exist_ok=True)
            cmd = [c.createrepo, "--update", "-q", "--workers", str(c.workers), d]
            t0 = time.time()
            try:
                pr = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                                    timeout=3600)
                if pr.returncode != 0:
                    raise RuntimeError(pr.stdout[-600:])
                md = os.path.join(d, "repodata", "repomd.xml")
                if c.sign_metadata and self.key:
                    gpgutil.detach_sign(c.gnupg_home, c.key_id, md, c.passphrase_file)
                rel = os.path.relpath(d, c.repo_root)
                state[rel] = {"built": time.time(), "seconds": round(time.time() - t0, 1), "fp": self.fingerprint(d),
                              "packages": len([n for n in os.listdir(d) if n.endswith(".rpm") and not n.startswith(".")]),
                              "signed": bool(c.sign_metadata and self.key)}
                self.clear_dirty([d])
            except Exception as e:  # noqa
                log.error("createrepo %s: %s", d, e)
                errors.append("%s: %s" % (os.path.relpath(d, c.repo_root), str(e)[-300:]))
                self.clear_dirty([d])
        db.set_setting(self.con, "repo_state", state)
        for jid, msg in self.waiting_md:
            if errors:
                self.finish(jid, "error", (msg or "") + "; ошибка метаданных: " + "; ".join(errors))
            else:
                self.finish(jid, "done", msg or "метаданные пересобраны")
        self.waiting_md = []

    # ------------------------------------------------------------ сканирование
    def scan(self, force=False):
        """Сверяет таблицу packages с файлами на диске; находит мусор; noarch раскладывает
        во все архитектуры; отмечает каталоги, где метаданные устарели."""
        c = self.cfg
        self.ensure_tree()
        found = {}  # (branch, filename) -> [arches]
        junk = []
        now = time.time()
        for a in c.arches:
            for b in c.branches:
                d = c.branch_dir(a, b)
                for n in sorted(os.listdir(d)):
                    p = os.path.join(d, n)
                    if n in ("repodata",):
                        continue
                    if n.startswith(TMP_PREFIX) or n == ".repodata":
                        st = os.lstat(p)
                        if now - st.st_mtime > 3600:
                            junk.append((p, "dir" if os.path.isdir(p) else "file", st.st_size, "временный файл"))
                        continue
                    if os.path.isdir(p):
                        continue
                    if not n.endswith(".rpm"):
                        junk.append((p, "file", os.path.getsize(p), "не RPM (остаток прерванной загрузки?)"))
                        continue
                    found.setdefault((b, n), []).append(a)
        # устаревшие метаданные старой системы
        for p in [os.path.join(c.repo_root, "repodata")] + [os.path.join(c.repo_root, a, "repodata") for a in c.arches]:
            if os.path.isdir(p):
                junk.append((p, "dir", 0, "старые общие метаданные (клиенты их не используют)"))
        existing = {(r["branch"], r["filename"]): dict(r) for r in self.con.execute("SELECT * FROM packages")}
        dirty = []
        for (b, n), arches in found.items():
            p0 = os.path.join(c.branch_dir(arches[0], b), n)
            st = os.stat(p0)
            row = existing.get((b, n))
            try:
                if row and not force and row["size"] == st.st_size and abs(row["mtime"] - st.st_mtime) < 1 \
                        and row["arches"] == " ".join(arches):
                    h = None
                else:
                    h = rpmutil.read_header(p0)
            except ValueError as e:
                junk.append((p0, "file", st.st_size, "повреждённый RPM: %s" % str(e)[:100]))
                continue
            arch = h["arch"] if h else row["arch"]
            want = c.arch_dirs(arch)
            if arch == "noarch":
                missing = [a for a in want if a not in arches]
                for a in missing:
                    d = c.branch_dir(a, b)
                    dst = os.path.join(d, n)
                    if os.path.exists(dst):
                        continue
                    tmp = os.path.join(d, TMP_PREFIX + n + ".part")
                    try:
                        os.link(p0, tmp)
                    except OSError:
                        shutil.copyfile(p0, tmp)
                        os.chmod(tmp, 0o644)
                    os.replace(tmp, dst)
                    arches.append(a)
                    dirty.append(d)
                    log.info("noarch %s/%s добавлен в %s", b, n, a)
            arches = [a for a in c.arches if a in arches]
            if h is not None or (row and row["arches"] != " ".join(arches)):
                self.upsert_package(b, n, arches, h)
            elif row and rpmutil.classify_sig(row["sigkey"], self.our_keys) != row["sigclass"]:
                self.con.execute("UPDATE packages SET sigclass=? WHERE id=?",
                                 (rpmutil.classify_sig(row["sigkey"], self.our_keys), row["id"]))
        for k, row in existing.items():
            if k not in found:
                self.con.execute("DELETE FROM packages WHERE id=?", (row["id"],))
        with db.tx(self.con):
            self.con.execute("DELETE FROM junk")
            for p, kind, size, reason in junk:
                self.con.execute("INSERT OR REPLACE INTO junk(path, kind, size, reason) VALUES(?,?,?,?)",
                                 (p, kind, size, reason))
        # где метаданные не соответствуют файлам
        state = db.get_setting(self.con, "repo_state", {}) or {}
        for a in c.arches:
            for b in c.branches:
                d = c.branch_dir(a, b)
                rel = os.path.relpath(d, c.repo_root)
                md = os.path.join(d, "repodata", "repomd.xml")
                fp = self.fingerprint(d)
                if not os.path.exists(md) or state.get(rel, {}).get("fp") != fp:
                    dirty.append(d)
                elif c.sign_metadata and self.key and not os.path.exists(md + ".asc"):
                    dirty.append(d)
        if dirty:
            self.mark_dirty(dirty)
        self.last_scan = time.time()
        db.set_setting(self.con, "last_scan", self.last_scan)
        return len(found)

    # ------------------------------------------------------------ цикл
    def run(self):
        signal.signal(signal.SIGTERM, self._sigterm)
        os.umask(0o002)
        self.con.execute("UPDATE jobs SET status='queued', step=NULL WHERE status='running' AND worker='signer'")
        self.refresh_key()
        try:
            self.scan()
        except Exception:  # noqa
            log.exception("первичное сканирование")
        log.info("homerepo-signer %s запущен, ключ %s", VERSION, (self.key or {}).get("fpr", "—"))
        last_key = time.time()
        while not self.stop:
            self.heartbeat("idle")
            job = self.claim()
            if job:
                self.process(job)
                continue
            if self.dirty():
                self.heartbeat("metadata")
                try:
                    self.rebuild_metadata()
                except Exception:  # noqa
                    log.exception("метаданные")
                continue
            if self.waiting_md:
                for jid, msg in self.waiting_md:
                    self.finish(jid, "done", msg or "готово")
                self.waiting_md = []
            now = time.time()
            if now - self.last_scan > self.cfg.rescan_minutes * 60:
                try:
                    self.scan()
                except Exception:  # noqa
                    log.exception("сканирование")
            if now - last_key > 600:
                self.refresh_key()
                last_key = now
            time.sleep(1.0)
        log.info("остановлен")

    def _sigterm(self, *a):
        self.stop = True


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    os.umask(0o002)
    cfg = config.get()
    Signer(cfg).run()


if __name__ == "__main__":
    main()
