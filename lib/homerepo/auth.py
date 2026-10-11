"""Пароли (scrypt из стандартной библиотеки), токены, правила паролей."""

import base64
import hashlib
import hmac
import secrets

ROLES = ("viewer", "operator", "admin")
ROLE_LEVEL = {r: i for i, r in enumerate(ROLES)}

# пароли старой панели и очевидные варианты не принимаются
BANNED = {
    "admin", "admin123", "manager123", "viewer123", "password", "password1", "123456", "12345678", "1234567890",
    "qwerty", "qwerty123", "qwertyuiop", "homerepo", "homerepo123", "changeme", "letmein", "p@ssw0rd",
}

_N, _R, _P = 2 ** 14, 8, 1


def hash_password(pw):
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt$%d$%d$%d$%s$%s" % (_N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(pw, stored):
    try:
        alg, n, r, p, salt, dk = stored.split("$")
        if alg != "scrypt":
            return False
        got = hashlib.scrypt(pw.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(base64.b64decode(dk)))
        return hmac.compare_digest(got, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


def password_problem(pw, name=""):
    if len(pw) < 10:
        return "пароль короче 10 символов"
    low = pw.lower()
    if low in BANNED or low.rstrip("!0123456789") in BANNED:
        return "слишком простой пароль"
    if name and name.lower() in low:
        return "пароль не должен содержать имя пользователя"
    if len(set(pw)) < 5:
        return "в пароле слишком мало разных символов"
    return None


def temp_password():
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(14))


def sha256(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def new_token(nbytes=32):
    return secrets.token_urlsafe(nbytes)


def enroll_token():
    alphabet = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(16))
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def norm_enroll(tok):
    return (tok or "").upper().replace("-", "").replace(" ", "")
