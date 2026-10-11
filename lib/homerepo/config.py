"""Чтение /etc/homerepo/homerepo.conf (INI)."""

import configparser
import os

DEFAULT_PATH = "/etc/homerepo/homerepo.conf"

DEFAULTS = {
    "repo": {
        "root": "/var/www/html/repos/homerepo",
        "url": "http://127.0.0.1/homerepo",
        "arches": "x86_64 aarch64",
        "branches": "os updates extras",
        "default_branch": "updates",
        "repo_gpgcheck": "no",
    },
    "panel": {
        "listen": "127.0.0.1",
        "port": "8090",
        "public_url": "https://127.0.0.1",
        "session_hours": "12",
        "upload_max_gb": "8",
    },
    "signer": {
        "gnupg_home": "/var/lib/homerepo-signer/gnupg",
        "key_id": "",
        "passphrase_file": "",
        "sign_metadata": "yes",
        "createrepo": "createrepo_c",
        "workers": "2",
        "rescan_minutes": "10",
    },
    "agent": {
        "api_url": "",
        "ca_file": "/etc/homerepo/tls/server.crt",
        "interval": "120",
        "report_every": "900",
    },
    "ansible": {
        "enabled": "yes",
        "remote_user": "homerepo-ansible",
        "runner_home": "/var/lib/homerepo-runner",
        "playbook_cmd": "ansible-playbook",
        "forks": "10",
        "timeout_minutes": "120",
    },
    "paths": {
        "state_dir": "/var/lib/homerepo",
        "web_dir": "",
        "agent_file": "",
    },
}

# Архитектуры, которые кладутся в дерево конкретной архитектуры репозитория
ARCH_MAP = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "i686": "x86_64",
    "i586": "x86_64",
    "i386": "x86_64",
    "aarch64": "aarch64",
    "arm64": "aarch64",
}


class Config:
    def __init__(self, path=None):
        self.path = path or os.environ.get("HOMEREPO_CONF") or DEFAULT_PATH
        cp = configparser.ConfigParser(interpolation=None)
        cp.read_dict(DEFAULTS)
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                cp.read_file(f)
        self.cp = cp
        here = os.path.dirname(os.path.abspath(__file__))
        base = os.path.dirname(os.path.dirname(here))  # .../lib/homerepo -> корень проекта

        self.repo_root = cp.get("repo", "root").rstrip("/")
        self.repo_url = cp.get("repo", "url").rstrip("/")
        self.arches = cp.get("repo", "arches").split()
        self.branches = cp.get("repo", "branches").split()
        self.default_branch = cp.get("repo", "default_branch")
        if self.default_branch not in self.branches:
            self.default_branch = self.branches[0]
        self.repo_gpgcheck = cp.getboolean("repo", "repo_gpgcheck")

        self.listen = cp.get("panel", "listen")
        self.port = cp.getint("panel", "port")
        self.public_url = cp.get("panel", "public_url").rstrip("/")
        self.session_hours = cp.getint("panel", "session_hours")
        self.upload_max = int(float(cp.get("panel", "upload_max_gb")) * 1024 ** 3)

        self.gnupg_home = cp.get("signer", "gnupg_home")
        self.key_id = cp.get("signer", "key_id").replace(" ", "")
        self.passphrase_file = cp.get("signer", "passphrase_file")
        self.sign_metadata = cp.getboolean("signer", "sign_metadata")
        self.createrepo = cp.get("signer", "createrepo")
        self.workers = cp.getint("signer", "workers")
        self.rescan_minutes = cp.getint("signer", "rescan_minutes")

        self.agent_api_url = (cp.get("agent", "api_url") or self.public_url).rstrip("/")
        self.agent_ca_file = cp.get("agent", "ca_file")
        self.agent_interval = cp.getint("agent", "interval")
        self.agent_report_every = cp.getint("agent", "report_every")

        self.ansible_enabled = cp.getboolean("ansible", "enabled")
        self.ansible_user = cp.get("ansible", "remote_user")
        self.runner_home = cp.get("ansible", "runner_home")
        self.playbook_cmd = cp.get("ansible", "playbook_cmd")
        self.ansible_forks = cp.getint("ansible", "forks")
        self.ansible_timeout = cp.getint("ansible", "timeout_minutes") * 60

        self.state_dir = cp.get("paths", "state_dir")
        self.web_dir = cp.get("paths", "web_dir") or os.path.join(base, "web")
        self.agent_file = cp.get("paths", "agent_file") or os.path.join(base, "agent", "homerepo-agent.py")

    @property
    def db_path(self):
        return os.path.join(self.state_dir, "homerepo.db")

    @property
    def secrets_dir(self):
        """Секретные переменные запуска: панель кладёт, исполнитель забирает и сразу удаляет."""
        return os.path.join(self.state_dir, "run-secrets")

    @property
    def incoming_dir(self):
        return os.path.join(self.state_dir, "incoming")

    def arch_dirs(self, arch):
        """Каталоги дерева, куда публикуется пакет с данной архитектурой."""
        if arch == "noarch":
            return list(self.arches)
        a = ARCH_MAP.get(arch)
        return [a] if a in self.arches else []

    def branch_dir(self, arch, branch):
        return os.path.join(self.repo_root, arch, branch)


_cfg = None


def get():
    global _cfg
    if _cfg is None:
        _cfg = Config()
    return _cfg
