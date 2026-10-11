"""Конструктор плейбуков: каждый блок и рецепт собирается в корректный YAML."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from homerepo import playbooks as P  # noqa: E402
from homerepo import yamlgen  # noqa: E402

try:
    import yaml
except ImportError:  # PyYAML нужен только для дополнительной проверки разбора
    yaml = None

CTX = {"repo_url": "https://repo.example.lan/homerepo", "branches": ["os", "updates", "extras"]}
SAMPLES = {
    "package": {"names": "htop\ntmux"}, "homerepo_package": {"names": ["btop"]}, "homerepo_repo": {},
    "upgrade": {"security": True, "reboot": True}, "service": {"name": "chronyd"},
    "file_content": {"dest": "/etc/motd", "content": "Привет, {{ inventory_hostname }}\n  отступ\nключ: значение # не комментарий"},
    "lineinfile": {"path": "/etc/x", "regexp": "^a", "line": "a = 1"}, "directory": {"path": "/srv/b", "mode": "750"},
    "get_url": {"url": "https://x.example/y", "dest": "/tmp/y"}, "timezone": {"tz": "Asia/Yekaterinburg"}, "hostname": {},
    "user": {"name": "bob", "ssh_keys": "ssh-ed25519 AAA b@x", "sudo": True, "groups": "wheel"},
    "sshd": {"no_password": True}, "firewalld": {"services": "http", "ports": "8080/tcp"},
    "seboolean": {"name": "httpd_can_network_connect"}, "rsyslog": {"host": "logs.example.lan"},
    "ipa_client": {"domain": "example.lan", "server": "ipa.example.lan"}, "reboot": {},
    "command": {"cmd": "echo 'a: b' | tee /tmp/x", "readonly": True}, "debug": {},
    "raw": {"yaml": "ansible.builtin.cron:\n  name: backup\n  minute: \"0\"\n  job: /usr/local/bin/b.sh"},
}


class Playbooks(unittest.TestCase):
    def test_every_block_renders(self):
        self.assertEqual(set(SAMPLES), set(P.BY_TYPE))
        m = {"name": "Все блоки", "steps": [{"type": k, "params": v} for k, v in SAMPLES.items()]}
        text, errors = P.render(m, CTX)
        self.assertEqual(errors, {})
        self.assertIn("ansible.builtin.dnf", text)
        if yaml:
            doc = yaml.safe_load(text)
            self.assertEqual(doc[0]["hosts"], "all")
            self.assertGreater(len(doc[0]["tasks"]), 25)
            motd = [t for t in doc[0]["tasks"] if t.get("name") == "Файл с содержимым"][0]
            self.assertIn("ключ: значение # не комментарий", motd["ansible.builtin.copy"]["content"])

    def test_recipes(self):
        for r in P.RECIPES:
            text, errors = P.render(P.recipe_model(r["id"]), CTX)
            self.assertEqual(errors, {}, r["id"])
            if yaml:
                self.assertIsInstance(yaml.safe_load(text), list)

    def test_errors_and_secrets(self):
        m = {"steps": [{"type": "package", "params": {}}, {"type": "ipa_client", "params": {"domain": "example.lan"}}]}
        text, errors = P.render(m, CTX)
        self.assertIn(0, errors)
        self.assertIn("no_log: true", text)
        self.assertEqual([s["name"] for s in P.secrets_needed(m)], ["ipa_password"])

    def test_os_condition_forces_facts(self):
        m = {"gather_facts": False, "steps": [{"type": "debug", "params": {}, "only_os": "redos"}]}
        text, _ = P.render(m, CTX)
        self.assertIn("gather_facts: true", text)
        self.assertIn("ansible_distribution", text)

    def test_scalars(self):
        self.assertEqual(yamlgen.scalar("yes"), '"yes"')
        self.assertEqual(yamlgen.scalar("0644"), '"0644"')
        self.assertEqual(yamlgen.scalar("{{ x }}"), '"{{ x }}"')
        self.assertEqual(yamlgen.scalar("Пакеты"), "Пакеты")
        self.assertEqual(yamlgen.scalar("a: b"), '"a: b"')


if __name__ == "__main__":
    unittest.main()
