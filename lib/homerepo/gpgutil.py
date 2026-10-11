"""Обёртки над gpg для службы подписи."""

import os
import subprocess


def _env(home):
    env = dict(os.environ)
    env["GNUPGHOME"] = home
    env["LC_ALL"] = "C"
    return env


def _run(home, args, inp=None, timeout=120):
    return subprocess.run(
        ["gpg", "--homedir", home, "--batch", "--no-tty"] + args,
        input=inp, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_env(home), timeout=timeout,
    )


def key_info(home, key_id=""):
    """Сведения о секретном ключе: отпечаток, все key id (основной и подключи), возможность подписи."""
    p = _run(home, ["--with-colons", "--fixed-list-mode", "--list-secret-keys"] + ([key_id] if key_id else []))
    if p.returncode != 0:
        return None
    info = None
    last = None
    for line in p.stdout.decode("utf-8", "replace").splitlines():
        f = line.split(":")
        t = f[0]
        if t == "sec":
            if info is not None:
                break  # берём первый подходящий ключ
            info = {
                "keyid": f[4].lower(), "algo": f[3], "bits": f[2], "created": int(f[5] or 0),
                "expires": int(f[6] or 0), "caps": f[11], "subkeys": [], "uid": "", "fpr": "",
            }
            last = info
        elif t == "ssb" and info is not None:
            sk = {"keyid": f[4].lower(), "created": int(f[5] or 0), "expires": int(f[6] or 0), "caps": f[11], "fpr": ""}
            info["subkeys"].append(sk)
            last = sk
        elif t == "fpr" and last is not None and not last.get("fpr"):
            last["fpr"] = f[9]
        elif t == "uid" and info is not None and not info["uid"]:
            info["uid"] = f[9]
    if not info:
        return None
    ids = [info["keyid"]] + [s["keyid"] for s in info["subkeys"]]
    info["keyids"] = [i[-16:] for i in ids]
    caps = info["caps"] + "".join(s["caps"] for s in info["subkeys"])
    info["can_sign"] = "s" in caps.lower()
    return info


def export_public(home, key_id):
    p = _run(home, ["--armor", "--export", key_id])
    if p.returncode != 0 or not p.stdout.strip():
        raise RuntimeError("не удалось экспортировать открытый ключ: %s" % p.stderr.decode("utf-8", "replace")[-300:])
    return p.stdout


def pass_args(passphrase_file):
    if passphrase_file:
        return ["--pinentry-mode", "loopback", "--passphrase-file", passphrase_file]
    return ["--pinentry-mode", "error"]


def detach_sign(home, key_id, path, passphrase_file=""):
    out = path + ".asc"
    tmp = out + ".tmp"
    p = _run(home, pass_args(passphrase_file) + ["--yes", "--armor", "--local-user", key_id,
                                                  "--output", tmp, "--detach-sign", path])
    if p.returncode != 0:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise RuntimeError("gpg: %s" % p.stderr.decode("utf-8", "replace")[-400:])
    os.replace(tmp, out)
    return out
