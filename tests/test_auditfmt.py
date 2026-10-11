"""Журнал действий человеческим языком (вымышленные имена и адреса)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from homerepo.auditfmt import describe  # noqa: E402

CTX = {"hosts": {2: "srv-files"}, "tokens": {8: "pi-garage"}, "discovered": {"192.168.10.1": "gw.example.lan"}}


class AuditText(unittest.TestCase):
    def d(self, action, detail):
        return describe(action, detail, "admin", CTX)

    def test_assign(self):
        t = self.d("assign", '{"action": "install", "created": ["srv-files: btop", "pi-garage: btop"], '
                             '"skipped": [], "schedule": "now"}')
        self.assertEqual(t, "Установить или обновить btop на srv-files и pi-garage — при следующем опросе агента")
        t = self.d("assign", '{"action": "hold", "created": ["a: x", "b: x", "a: y"], '
                             '"skipped": ["c: x — нет сборки для aarch64"], "schedule": "night"}')
        self.assertIn("Держать текущую версию x на a и b; y на a — ночью", t)
        self.assertIn("Пропущено: c: x — нет сборки", t)
        self.assertNotIn("{", t)

    def test_misc(self):
        self.assertEqual(self.d("errors_ack", '{"jobs": [], "tasks": [5]}'), "Скрыта ошибка назначения #5")
        self.assertEqual(self.d("token_create", '{"hours": 168, "multi": true, "note": "лаборатория"}'),
                         "Многоразовый токен подключения на 7 дн. · «лаборатория»")
        self.assertEqual(self.d("report", "устройство #2"), "Отчёт об устройстве srv-files")
        self.assertEqual(self.d("device_os", "192.168.10.1: router MikroTik RouterOS"),
                         "192.168.10.1 (gw.example.lan) — роутер, MikroTik RouterOS")
        self.assertEqual(self.d("device_os", "192.168.10.80: macos macOS"), "192.168.10.80 — macOS")
        self.assertEqual(self.d("host_enroll", "токен #8"), "Агент зарегистрирован по токену #8 · «pi-garage»")
        self.assertEqual(self.d("task_retry", "#5 srv-files collect tool"), "Назначение #5: забрать в HomeRepo tool на srv-files")
        self.assertIn("1,4 КБ", self.d("upload", '{"file": "a.rpm", "size": 1433, "branch": "updates", "sign": true}'))
        self.assertEqual(self.d("package_move", '{"files": ["a.rpm", "b.rpm"], "to": "updates"}'),
                         "Перемещение a.rpm и b.rpm → ветка updates")

    def test_unknown_and_broken(self):
        self.assertEqual(self.d("something_new", '{"a": [1, 2], "b": null}'), "a: 1, 2")
        self.assertEqual(self.d("assign", "{битый json"), "{битый json")
        self.assertEqual(self.d("login", ""), "")


if __name__ == "__main__":
    unittest.main()
