#!/bin/bash
# HomeRepo — откат к старой системе (PHP-панель на Apache) из резервной копии установщика.
#   sudo /opt/homerepo/deploy/rollback.sh /var/backups/homerepo-YYYYmmdd-HHMMSS
# Пакеты в репозитории не трогаются. Данные новой панели (/var/lib/homerepo) остаются на месте.
set -euo pipefail
B="${1:-$(cat /var/backups/homerepo-last 2>/dev/null || true)}"
[ "$(id -u)" = 0 ] || { echo "запустите от root" >&2; exit 1; }
[ -n "$B" ] && [ -d "$B" ] || { echo "укажите каталог резервной копии /var/backups/homerepo-…" >&2; exit 1; }
echo "==> откат из $B"
systemctl disable --now homerepo-runner homerepo-web homerepo-signer 2>/dev/null || true
if [ -f /etc/nginx/conf.d/homerepo.conf ]; then
  rm -f /etc/nginx/conf.d/homerepo.conf
  systemctl disable --now nginx 2>/dev/null || true
fi
if [ -d "$B/files" ]; then
  (cd "$B/files" && find . -type f | while read -r f; do install -D -p -m "$(stat -c %a "$f")" "$f" "/${f#./}"; echo "    восстановлен /${f#./}"; done)
fi
[ -d "$B/repos-web" ] && [ ! -e /var/www/html/repos-web ] && mv "$B/repos-web" /var/www/html/repos-web && echo "    возвращена /var/www/html/repos-web"
[ -d "$B/old-apache-gnupg" ] && [ ! -e /usr/share/httpd/.gnupg ] && mv "$B/old-apache-gnupg" /usr/share/httpd/.gnupg && echo "    возвращён ключ apache"
if [ -d "$B/bin" ]; then for f in "$B"/bin/*; do [ -e "/usr/local/bin/$(basename "$f")" ] || cp -a "$f" /usr/local/bin/; done; fi
REPO=$(sed -n 's/^root\s*=\s*//p' /etc/homerepo/homerepo.conf 2>/dev/null | head -1)
[ -n "$REPO" ] && [ -d "$REPO" ] && id apache >/dev/null 2>&1 && chown -R apache:apache "$REPO" && echo "    репозиторий снова принадлежит apache"
systemctl daemon-reload
while read -r s en act; do
  case "$en" in enabled) systemctl enable -q "$s" 2>/dev/null || true;; esac
  case "$act" in active) systemctl start "$s" 2>/dev/null || true;; esac
done < "$B/services.txt"
echo "==> готово: старая панель снова на Apache. Новая (homerepo-web/homerepo-signer) остановлена."
