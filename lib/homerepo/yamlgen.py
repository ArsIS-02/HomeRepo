"""Небольшой генератор YAML для плейбуков (без PyYAML): порядок ключей сохраняется, вывод читаемый.

Поддерживает словари, списки, строки (простые, в кавычках, многострочные через «|»), числа, логические
значения, None и Raw — готовый кусок YAML, который вставляется как есть с нужным отступом.
"""

import json
import re

RESERVED = {"y", "n", "yes", "no", "true", "false", "on", "off", "null", "~", ""}
NUMLIKE = re.compile(r"^[-+]?(\d[\d_]*(\.\d*)?|\.\d+)([eE][-+]?\d+)?$|^0[xob][0-9a-fA-F_]+$|^[-+]?\.(inf|nan)$", re.I)
PLAIN_START_BAD = set("-?:,[]{}#&*!|>'\"%@` ")


class Raw:
    """Кусок YAML, вставляемый без изменений (например, задача, написанная вручную)."""

    def __init__(self, text):
        self.text = text


def scalar(v):
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v)
    if (s.lower() in RESERVED or NUMLIKE.match(s) or s[0] in PLAIN_START_BAD or s[-1] in " :"
            or ": " in s or " #" in s or "\t" in s or "{{" in s or "\\" in s or not s.isprintable()):
        return json.dumps(s, ensure_ascii=False)
    return s


def _block(s, indent):
    pad = " " * indent
    head = "|" if s.endswith("\n") else "|-"
    body = s[:-1] if s.endswith("\n") else s
    if body.startswith((" ", "\t")):
        head += "2"
    return head + "\n" + "\n".join((pad + ln) if ln else "" for ln in body.split("\n"))


def _emit(v, indent, out, key_prefix):
    pad = " " * indent
    if isinstance(v, dict):
        if not v:
            out.append(key_prefix + "{}")
            return
        first = True
        for k, val in v.items():
            lead = key_prefix if first else pad
            first = False
            ks = scalar(k)
            if isinstance(val, (dict, list)) and val:
                out.append("%s%s:" % (lead, ks))
                if isinstance(val, dict):
                    _emit(val, indent + 2, out, " " * (indent + 2))
                else:
                    _emit(val, indent + 2, out, " " * (indent + 2))
            elif isinstance(val, Raw):
                out.append("%s%s:" % (lead, ks))
                for ln in val.text.rstrip("\n").split("\n"):
                    out.append((" " * (indent + 2) + ln) if ln.strip() else "")
            elif isinstance(val, str) and "\n" in val:
                out.append("%s%s: %s" % (lead, ks, _block(val, indent + 2)))
            else:
                out.append("%s%s: %s" % (lead, ks, scalar(val) if not isinstance(val, (dict, list)) else
                                         ("{}" if isinstance(val, dict) else "[]")))
        return
    if isinstance(v, list):
        if not v:
            out.append(key_prefix + "[]")
            return
        for i, item in enumerate(v):
            lead = key_prefix if i == 0 else pad
            if isinstance(item, dict) and item:
                _emit(item, indent + 2, out, lead + "- ")
            elif isinstance(item, Raw):
                lines = item.text.rstrip("\n").split("\n")
                base = min((len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()), default=0)
                lines = [ln[base:] for ln in lines]
                if lines and lines[0].lstrip().startswith("- "):
                    out.append(lead + lines[0].lstrip())
                    rest = lines[1:]
                    shift = 0
                else:
                    out.append(lead + "- " + lines[0])
                    rest = lines[1:]
                    shift = 2
                for ln in rest:
                    out.append((pad + " " * shift + ln) if ln.strip() else "")
            elif isinstance(item, list):
                out.append(lead + "-")
                _emit(item, indent + 2, out, " " * (indent + 2))
            elif isinstance(item, str) and "\n" in item:
                out.append(lead + "- " + _block(item, indent + 2))
            else:
                out.append(lead + "- " + scalar(item))
        return
    out.append(key_prefix + scalar(v))


def dump(obj, header=None):
    out = []
    if header:
        out.extend("# " + ln if ln else "#" for ln in header.split("\n"))
    out.append("---")
    _emit(obj, 0, out, "")
    return "\n".join(out) + "\n"
