"""Модульные тесты, не требующие rpm/dnf (запускаются в CI)."""

import unittest

from homerepo import auth, rpmutil
from homerepo.scanner import guess_os


class VerCmp(unittest.TestCase):
    # ожидаемые значения сверены с rpm.labelCompare (rpm 4.16)
    CASES = [
        ("1.0", "1.0", 0), ("1.0", "1.0.1", -1), ("1.0a", "1.0", 1), ("1.0~rc1", "1.0", -1),
        ("1.0^git1", "1.0", 1), ("1.0^", "1.0~", 1), ("2.620", "2.61", 1), ("10", "9", 1),
        ("01", "1", 0), ("1.a", "1.1", -1), ("25.10.1.1215", "25.10.1.1173", 1), ("el10", "el9", 1),
        ("4.red80", "3.red80", 1), ("1.0+x", "1.0+y", -1), ("alpha", "beta1", -1),
    ]

    def test_cases(self):
        for a, b, want in self.CASES:
            self.assertEqual(rpmutil.rpmvercmp(a, b), want, (a, b))
            self.assertEqual(rpmutil.rpmvercmp(b, a), -want, (b, a))

    def test_evr(self):
        self.assertEqual(rpmutil.evr_cmp(1, "1.0", "1", 0, "9.9", "9"), 1)
        self.assertEqual(rpmutil.parse_evr("2:1.2-3"), (2, "1.2", "3"))
        self.assertEqual(rpmutil.fmt_evr(0, "1.2", "3"), "1.2-3")
        self.assertEqual(rpmutil.keyid_from_sigstr("RSA/SHA256, …, Key ID 0123456789ABCDEF"), "0123456789abcdef")


class Passwords(unittest.TestCase):
    def test_hash(self):
        h = auth.hash_password("Длинный-пароль-1")
        self.assertTrue(auth.verify_password("Длинный-пароль-1", h))
        self.assertFalse(auth.verify_password("другой", h))

    def test_policy(self):
        self.assertIsNotNone(auth.password_problem("short"))
        self.assertIsNotNone(auth.password_problem("qwerty1234"))
        self.assertIsNotNone(auth.password_problem("ivan-superpass", "ivan"))
        self.assertIsNone(auth.password_problem("Tr0ub4dor&3-horse", "admin"))


class OsGuess(unittest.TestCase):
    def test_guess(self):
        self.assertEqual(guess_os([22, 3389, 9090], "SSH-2.0-OpenSSH_9.9", "Raspberry Pi")[0], "linux")
        self.assertEqual(guess_os([135, 445, 3389], "", "")[0], "windows")
        self.assertEqual(guess_os([22], "SSH-2.0-OpenSSH_for_Windows_8.1", "")[0], "windows")
        self.assertEqual(guess_os([53, 80, 8291], "", "")[1], "MikroTik RouterOS")
        self.assertEqual(guess_os([22], "SSH-2.0-OpenSSH_8.9p1 Ubuntu-3", "")[1], "Ubuntu")


if __name__ == "__main__":
    unittest.main()
