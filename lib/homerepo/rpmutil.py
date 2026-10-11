"""Работа с RPM: сравнение версий (без модуля rpm), чтение заголовков, подписи."""

import os
import re
import subprocess

_ALNUM = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
_DIG = set("0123456789")
_ALPHA = _ALNUM - _DIG

KEYID_RE = re.compile(r"Key ID ([0-9a-fA-F]{8,40})")

SIG_QF = (
    "%|DSAHEADER?{%{DSAHEADER:pgpsig}}:{%|RSAHEADER?{%{RSAHEADER:pgpsig}}:"
    "{%|SIGGPG?{%{SIGGPG:pgpsig}}:{%|SIGPGP?{%{SIGPGP:pgpsig}}:{(none)}|}|}|}|"
)


def rpmvercmp(a, b):
    """Точная копия алгоритма rpmvercmp() из rpm (включая ~ и ^)."""
    a = a or ""
    b = b or ""
    if a == b:
        return 0
    i = j = 0
    la, lb = len(a), len(b)
    while i < la or j < lb:
        while i < la and a[i] not in _ALNUM and a[i] not in "~^":
            i += 1
        while j < lb and b[j] not in _ALNUM and b[j] not in "~^":
            j += 1
        if (i < la and a[i] == "~") or (j < lb and b[j] == "~"):
            if i >= la or a[i] != "~":
                return 1
            if j >= lb or b[j] != "~":
                return -1
            i += 1
            j += 1
            continue
        if (i < la and a[i] == "^") or (j < lb and b[j] == "^"):
            if i >= la:
                return -1
            if j >= lb:
                return 1
            if a[i] != "^":
                return 1
            if b[j] != "^":
                return -1
            i += 1
            j += 1
            continue
        if not (i < la and j < lb):
            break
        si, sj = i, j
        if a[i] in _DIG:
            isnum = True
            while i < la and a[i] in _DIG:
                i += 1
            while j < lb and b[j] in _DIG:
                j += 1
        else:
            isnum = False
            while i < la and a[i] in _ALPHA:
                i += 1
            while j < lb and b[j] in _ALPHA:
                j += 1
        s1, s2 = a[si:i], b[sj:j]
        if not s1:
            return -1
        if not s2:
            return 1 if isnum else -1
        if isnum:
            s1 = s1.lstrip("0")
            s2 = s2.lstrip("0")
            if len(s1) != len(s2):
                return 1 if len(s1) > len(s2) else -1
        if s1 != s2:
            return 1 if s1 > s2 else -1
    if i >= la and j >= lb:
        return 0
    return -1 if i >= la else 1


def evr_cmp(e1, v1, r1, e2, v2, r2):
    e1, e2 = int(e1 or 0), int(e2 or 0)
    if e1 != e2:
        return 1 if e1 > e2 else -1
    c = rpmvercmp(v1, v2)
    if c:
        return c
    return rpmvercmp(r1 or "", r2 or "")


def parse_evr(s):
    """'1:2.3-4' -> (1, '2.3', '4'); '2.3' -> (0, '2.3', '')."""
    s = (s or "").strip()
    e = 0
    if ":" in s:
        es, s = s.split(":", 1)
        e = int(es) if es.isdigit() else 0
    if "-" in s:
        v, r = s.rsplit("-", 1)
    else:
        v, r = s, ""
    return e, v, r


def fmt_evr(e, v, r):
    s = "%s-%s" % (v, r) if r else v
    return "%s:%s" % (e, s) if int(e or 0) else s


def keyid_from_sigstr(s):
    m = KEYID_RE.search(s or "")
    return m.group(1).lower()[-16:] if m else ""


def _s(x):
    if isinstance(x, bytes):
        return x.decode("utf-8", "replace")
    return x if x is not None else ""


def read_header(path):
    """Читает заголовок RPM (без проверки подписи). Возвращает dict или бросает ValueError."""
    import rpm  # системный python3-rpm

    ts = rpm.TransactionSet()
    ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)
    fd = os.open(path, os.O_RDONLY)
    try:
        h = ts.hdrFromFdno(fd)
    except rpm.error as e:
        raise ValueError("не удалось прочитать заголовок RPM: %s" % e)
    finally:
        os.close(fd)
    src = bool(h[rpm.RPMTAG_SOURCEPACKAGE])
    return {
        "name": _s(h[rpm.RPMTAG_NAME]),
        "epoch": int(h[rpm.RPMTAG_EPOCH] or 0),
        "version": _s(h[rpm.RPMTAG_VERSION]),
        "release": _s(h[rpm.RPMTAG_RELEASE]),
        "arch": "src" if src else _s(h[rpm.RPMTAG_ARCH]),
        "summary": _s(h[rpm.RPMTAG_SUMMARY])[:300],
        "buildtime": int(h[rpm.RPMTAG_BUILDTIME] or 0),
        "sigkey": keyid_from_sigstr(_s(h.format(SIG_QF))),
        "source": src,
    }


def check_digests(path):
    """Проверка целостности (дайджесты заголовка и содержимого) без проверки подписи."""
    try:
        p = subprocess.run(
            ["rpm", "-K", "--nosignature", path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=1800,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    out = (p.stdout or "").strip()
    if p.returncode != 0 or "NOT OK" in out:
        return False, out[-500:]
    return True, out


def canonical_filename(h):
    return "%s-%s-%s.%s.rpm" % (h["name"], h["version"], h["release"], h["arch"])


def classify_sig(sigkey, our_keys):
    if not sigkey:
        return "none"
    return "ours" if sigkey.lower()[-16:] in our_keys else "foreign"
