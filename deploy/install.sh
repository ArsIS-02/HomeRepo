#!/bin/bash
# HomeRepo — установка на сервер репозитория (РЕД ОС 8 / RHEL-подобные) и переезд со старой PHP-панели.
#
#   sudo ./deploy/install.sh --check        проверить систему и показать, что будет сделано (ничего не меняет)
#   sudo ./deploy/install.sh                установить / обновить
#
# Параметры:
#   --web nginx|httpd    веб-сервер (по умолчанию nginx; если nginx не ставится — httpd)
#   --repo-url URL       адрес репозитория для клиентов (по умолчанию берётся из /etc/yum.repos.d/homerepo.repo)
#   --repo-root DIR      каталог репозитория (по умолчанию /var/www/html/repos/homerepo)
#   --key-id ID          ключ подписи (по умолчанию — единственный секретный ключ старой панели)
#   --old-gnupg DIR      каталог ключей старой панели (по умолчанию /usr/share/httpd/.gnupg)
#   --import-key FILE    взять готовый секретный ключ из файла (gpg --armor --export-secret-keys …)
#   --key-name "Имя <почта>"  владелец нового ключа, если ключ создаётся (по умолчанию «HomeRepo signing key»)
#                        Если ключа нет нигде, установщик создаст новый (RSA 4096, без пароля, только у службы подписи).
#   --no-ansible         не ставить Ansible (раздел «Плейбуки» останется, но запускать будет нечем)
#   --yes                не спрашивать подтверждение
#
# Повторный запуск безопасен: обновляет код и службы, данные и пользователей не трогает.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
HR_VERSION="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$SRC/lib/homerepo/__init__.py")"
WEB="nginx"; WEB_FORCED=0
REPO_URL=""; REPO_ROOT="/var/www/html/repos/homerepo"; KEY_ID=""; OLD_GNUPG="/usr/share/httpd/.gnupg"
IMPORT_KEY=""; KEY_NAME=""; KEY_MODE=""
CHECK=0; YES=0; NO_ANSIBLE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --web) WEB="$2"; WEB_FORCED=1; shift 2 ;;
    --repo-url) REPO_URL="${2%/}"; shift 2 ;;
    --repo-root) REPO_ROOT="${2%/}"; shift 2 ;;
    --key-id) KEY_ID="$2"; shift 2 ;;
    --old-gnupg) OLD_GNUPG="$2"; shift 2 ;;
    --import-key) IMPORT_KEY="$2"; shift 2 ;;
    --key-name) KEY_NAME="$2"; shift 2 ;;
    --check) CHECK=1; shift ;;
    --no-ansible) NO_ANSIBLE=1; shift ;;
    --yes|-y) YES=1; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "неизвестный параметр: $1" >&2; exit 2 ;;
  esac
done

C_OK=$'\033[32m'; C_W=$'\033[33m'; C_E=$'\033[31m'; C_H=$'\033[36m'; C_0=$'\033[0m'
conf_get(){ [ -f "$CONF" ] && sed -n "s/^$1\s*=\s*//p" "$CONF" | head -1 || true; }
say(){ printf '%s==>%s %s\n' "$C_H" "$C_0" "$*"; }
ok(){ printf '    %s✓%s %s\n' "$C_OK" "$C_0" "$*"; }
warn(){ printf '    %s!%s %s\n' "$C_W" "$C_0" "$*"; }
die(){ printf '%sОШИБКА:%s %s\n' "$C_E" "$C_0" "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "запустите от root: sudo $0"

TS="$(date +%Y%m%d-%H%M%S)"
BACKUP="/var/backups/homerepo-$TS"
CONF=/etc/homerepo/homerepo.conf
STATE=/var/lib/homerepo
SIGNER_HOME=/var/lib/homerepo-signer
RUNNER_HOME=/var/lib/homerepo-runner
GNUPG_NEW="$SIGNER_HOME/gnupg"
TLS_DIR=/etc/homerepo/tls

# ================================================================== проверка системы
say "HomeRepo $HR_VERSION — проверка системы"
. /etc/os-release 2>/dev/null || true
ok "ОС: ${PRETTY_NAME:-?} ($(uname -m))"
command -v python3 >/dev/null || die "нет python3"
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,7) else 1)' || die "нужен Python 3.7+, найден $PYV"
ok "Python $PYV"

NEED_PKGS=()
python3 -c 'import rpm' 2>/dev/null || NEED_PKGS+=(python3-rpm)
command -v createrepo_c >/dev/null || NEED_PKGS+=(createrepo_c)
command -v gpg >/dev/null || NEED_PKGS+=(gnupg2)
command -v openssl >/dev/null || NEED_PKGS+=(openssl)
command -v ssh >/dev/null && command -v ssh-keygen >/dev/null || NEED_PKGS+=(openssh-clients)
python3 -c 'import hashlib; hashlib.scrypt(b"x", salt=b"y", n=16, r=8, p=1)' 2>/dev/null || die "в Python нет hashlib.scrypt (нужен OpenSSL 1.1+)"
[ ${#NEED_PKGS[@]} -eq 0 ] && ok "rpm, createrepo_c, gnupg2, openssl, ssh на месте" || warn "будут установлены: ${NEED_PKGS[*]}"

# Ansible для раздела «Плейбуки»
ANSIBLE_PKG=""
if command -v ansible-playbook >/dev/null; then
  ok "Ansible: $(ansible-playbook --version 2>/dev/null | head -1)"
elif [ $NO_ANSIBLE = 1 ]; then
  warn "Ansible не установлен и не будет ставиться (--no-ansible): плейбуки запускать будет нечем"
else
  for p in ansible ansible-core; do dnf -q list --available "$p" >/dev/null 2>&1 && { ANSIBLE_PKG=$p; break; }; done
  [ -n "$ANSIBLE_PKG" ] && warn "будет установлен $ANSIBLE_PKG (для раздела «Плейбуки»; не нужен — --no-ansible)" \
    || warn "пакета ansible в подключённых репозиториях нет — раздел «Плейбуки» заработает после dnf install ansible-core"
fi

# старая система
OLD_PANEL=""; [ -d /var/www/html/repos-web ] && OLD_PANEL=/var/www/html/repos-web
OLD_ANSIBLE=""; [ -f /etc/systemd/system/repo-panel.service ] && OLD_ANSIBLE=repo-panel
OLD_PLAYBOOKS=$(ls /opt/ansible-repo/playbooks/*.yml /opt/ansible-repo/playbooks/*.yaml 2>/dev/null | tr '\n' ' ' || true)
[ -n "$OLD_ANSIBLE" ] && warn "найдена старая веб-панель Ansible (служба repo-panel, порт 5000) — будет отключена: её делает раздел «Плейбуки»"
[ -n "$OLD_PLAYBOOKS" ] && warn "плейбуки из /opt/ansible-repo/playbooks будут импортированы в раздел «Плейбуки» (файлы остаются на месте)"
[ -n "$OLD_PANEL" ] && warn "найдена старая PHP-панель $OLD_PANEL — будет отключена и перенесена в резервную копию"
[ -d "$REPO_ROOT" ] && ok "репозиторий: $REPO_ROOT ($(find "$REPO_ROOT" -name '*.rpm' -type f 2>/dev/null | wc -l) RPM, $(du -sh "$REPO_ROOT" 2>/dev/null | cut -f1))" \
  || warn "каталога $REPO_ROOT нет — будет создан пустой репозиторий"

# адрес репозитория
URL_SRC="указан параметром --repo-url"
if [ -z "$REPO_URL" ] && [ -f /etc/yum.repos.d/homerepo.repo ]; then
  URL_SRC="как в текущих .repo-файлах хостов"
  REPO_URL=$(sed -n 's#^\s*baseurl\s*=\s*\(.*\)/homerepo/.*#\1/homerepo#p' /etc/yum.repos.d/homerepo.repo | head -1)
fi
if [ -z "$REPO_URL" ] && [ -f "$CONF" ]; then
  URL_SRC="из $CONF"
  REPO_URL=$(conf_get url)
fi
MYIP=$( (ip -4 route get 1.1.1.1 2>/dev/null || true) | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1)
[ -n "$REPO_URL" ] || { REPO_URL="http://${MYIP:-$(uname -n)}/homerepo"; URL_SRC="по адресу сервера; другой — параметром --repo-url"; }
REPO_HOST=$(echo "$REPO_URL" | sed -E 's#^[a-z]+://([^/:]+).*#\1#')
PUBLIC_URL="https://$REPO_HOST"
ok "адрес для клиентов: $REPO_URL ($URL_SRC)"
ok "панель будет доступна: $PUBLIC_URL/"

# ключ подписи (каталог старой панели читаем от имени его владельца)
OLD_GPG="gpg"
if [ -d "$OLD_GNUPG" ]; then
  OWN=$(stat -c %U "$OLD_GNUPG")
  [ "$OWN" != root ] && id "$OWN" >/dev/null 2>&1 && OLD_GPG="runuser -u $OWN -- gpg"
fi
HAVE_NEW_KEY=0
if [ -d "$GNUPG_NEW" ] && runuser -u homerepo-signer -- gpg --homedir "$GNUPG_NEW" --batch --list-secret-keys 2>/dev/null | grep -q '^sec'; then
  HAVE_NEW_KEY=1; KEY_MODE=existing
  [ -n "$KEY_ID" ] || KEY_ID=$(conf_get key_id)
  ok "ключ уже у службы подписи: $GNUPG_NEW ($KEY_ID)"
elif [ -n "$IMPORT_KEY" ]; then
  [ -r "$IMPORT_KEY" ] || die "нет файла $IMPORT_KEY"
  KEY_MODE=import
  KEY_ID=$(gpg --batch --with-colons --import-options show-only --import "$IMPORT_KEY" 2>/dev/null | awk -F: '/^fpr/{print $10; exit}')
  gpg --batch --with-colons --import-options show-only --import "$IMPORT_KEY" 2>/dev/null | grep -q '^sec' \
    || die "в $IMPORT_KEY нет секретного ключа (нужен вывод gpg --armor --export-secret-keys)"
  ok "ключ подписи будет импортирован из $IMPORT_KEY: $KEY_ID"
elif [ -n "$KEY_ID" ] || { [ -d "$OLD_GNUPG" ] && $OLD_GPG --homedir "$OLD_GNUPG" --batch --list-secret-keys 2>/dev/null | grep -q '^sec'; }; then
  KEY_MODE=migrate
  if [ -z "$KEY_ID" ]; then
    KEY_ID=$($OLD_GPG --homedir "$OLD_GNUPG" --batch --with-colons --list-secret-keys 2>/dev/null | awk -F: '/^fpr/{print $10; exit}')
    NKEYS=$($OLD_GPG --homedir "$OLD_GNUPG" --batch --with-colons --list-secret-keys 2>/dev/null | grep -c '^sec' || true)
    [ "${NKEYS:-0}" -le 1 ] || die "в $OLD_GNUPG несколько секретных ключей — укажите нужный: --key-id"
  fi
  [ -d "$OLD_GNUPG" ] || die "ключ $KEY_ID указан, но каталога $OLD_GNUPG нет (--old-gnupg или --import-key)"
  ok "ключ подписи: $KEY_ID — будет перенесён из $OLD_GNUPG в $GNUPG_NEW (доступ только у службы homerepo-signer)"
else
  KEY_MODE=generate
  [ -n "$KEY_NAME" ] || KEY_NAME="HomeRepo signing key <homerepo@$(hostname -f 2>/dev/null || uname -n)>"
  ok "ключа подписи нет — будет создан новый: RSA 4096, «$KEY_NAME» (копия попадёт в резервную копию)"
fi

# порт панели
PORT=$(conf_get port)
if [ -z "$PORT" ]; then
  for p in 8090 8091 8092 8093 8094 8095; do
    (ss -ltnH "sport = :$p" 2>/dev/null || true) | grep -q . || { PORT=$p; break; }
  done
fi
[ -n "$PORT" ] || die "не нашёлся свободный порт 8090–8095"
ok "панель слушает 127.0.0.1:$PORT (снаружи — только через веб-сервер)"

# веб-сервер
KNOWN_HTTPD='^(repos-web|homerepo|ssl|welcome|autoindex|userdir|php|README|manual|fcgid|lua|nss|perl|deflate|lookup_identity|auth_gssapi|auth_mellon)(\.conf)?$'
OTHER_HTTPD=""
if [ -d /etc/httpd/conf.d ]; then
  for f in /etc/httpd/conf.d/*.conf; do
    [ -f "$f" ] || continue
    b=$(basename "$f" .conf)
    echo "$b" | grep -Eq "$KNOWN_HTTPD" || OTHER_HTTPD="$OTHER_HTTPD $b"
  done
fi
if [ "$WEB" = nginx ] && [ -n "$OTHER_HTTPD" ] && [ $WEB_FORCED = 0 ] && systemctl is-active -q httpd 2>/dev/null; then
  warn "Apache обслуживает и другие сайты:$OTHER_HTTPD — оставляю Apache (--web nginx чтобы заменить)"
  WEB=httpd
fi
case "$WEB" in
  nginx) ok "веб-сервер: nginx вместо Apache (Apache и php-fpm будут остановлены)";;
  httpd) ok "веб-сервер: Apache httpd (только раздаёт файлы репозитория и проксирует панель)";;
  *) die "--web: nginx или httpd";;
esac

SELINUX=0
command -v selinuxenabled >/dev/null && selinuxenabled 2>/dev/null && SELINUX=1
[ $SELINUX = 1 ] && ok "SELinux включён — будут выставлены метки и httpd_can_network_connect"

# sudoers
SUDO_FILES=$(grep -lsE '^\s*apache\s' /etc/sudoers.d/* 2>/dev/null | tr '\n' ' ' || true)

grep -qE '^\s*apache\s' /etc/sudoers 2>/dev/null && warn "правила apache в /etc/sudoers — удалите их вручную (visudo)"
[ -n "$SUDO_FILES" ] && warn "правила sudo для apache (root через rpm/gpg/nmap) будут закомментированы в: $SUDO_FILES"

case "$KEY_MODE" in
  existing) KEY_MODE_RU="уже на месте";; import) KEY_MODE_RU="импорт из файла";;
  migrate) KEY_MODE_RU="перенос из старой панели";; generate) KEY_MODE_RU="создание нового";; *) KEY_MODE_RU="?";;
esac
echo
cat <<EOF
План:
  1. резервная копия старой системы → $BACKUP
  2. пользователи homerepo (панель), homerepo-signer (подпись), homerepo-runner (плейбуки Ansible), код в /opt/homerepo
  3. ключ подписи ($KEY_MODE_RU) в $GNUPG_NEW, права на репозиторий — homerepo-signer
  4. службы homerepo-signer, homerepo-web и homerepo-runner, сертификат для HTTPS${ANSIBLE_PKG:+, пакет $ANSIBLE_PKG}
  5. веб-сервер: $WEB
  6. отключение старой системы, если она есть: PHP-панель repos-web, панель Ansible repo-panel, таймер update-homerepo, sudo для apache
  7. пересканирование репозитория, пересборка и подпись метаданных
Пакеты и адреса репозитория для клиентов не меняются.
EOF
[ $CHECK = 1 ] && { echo; echo "(режим --check: ничего не изменено)"; exit 0; }
if [ $YES = 0 ]; then
  read -r -p "Продолжить? [y/N] " a </dev/tty || a=n
  case "$a" in y|Y|д|Д|yes|да) ;; *) echo "отменено"; exit 1;; esac
fi

# ================================================================== установка
mkdir -p "$BACKUP"; chmod 700 "$BACKUP"
echo "$BACKUP" > /var/backups/homerepo-last
ROLLBACK_NOTE="$BACKUP/ROLLBACK.txt"
echo "Откат: sudo $SRC/deploy/rollback.sh $BACKUP" > "$ROLLBACK_NOTE"

say "1. резервная копия → $BACKUP"
for f in /etc/httpd/conf.d/repos-web.conf /etc/httpd/conf.d/homerepo.conf /etc/httpd/conf.d/ssl.conf \
         /etc/systemd/system/update-homerepo.service /etc/systemd/system/update-homerepo.timer \
         /etc/nginx/nginx.conf /etc/nginx/conf.d/homerepo.conf /etc/php-fpm.d/www.conf \
         /etc/systemd/system/repo-panel.service $SUDO_FILES; do
  [ -f "$f" ] && { mkdir -p "$BACKUP/files$(dirname "$f")"; cp -a "$f" "$BACKUP/files$f"; }
done
for s in httpd nginx php-fpm update-homerepo.timer repo-panel; do
  printf '%s %s %s\n' "$s" "$(systemctl is-enabled "$s" 2>/dev/null || echo none)" "$(systemctl is-active "$s" 2>/dev/null || echo inactive)"
done > "$BACKUP/services.txt"
if [ "$KEY_MODE" = import ]; then
  install -m 600 "$IMPORT_KEY" "$BACKUP/signing-key.secret.asc"
elif [ "$KEY_MODE" = migrate ]; then
  (umask 077; $OLD_GPG --homedir "$OLD_GNUPG" --batch --armor --export-secret-keys "$KEY_ID" > "$BACKUP/signing-key.secret.asc" 2>/dev/null) \
    && [ -s "$BACKUP/signing-key.secret.asc" ] && ok "секретный ключ сохранён в $BACKUP/signing-key.secret.asc (0600, только root)" \
    || die "не удалось экспортировать ключ из $OLD_GNUPG (ключ с паролем? укажите его в /etc/homerepo/signer.pass и перезапустите)"
fi
ok "готово"

say "2. пакеты, пользователи, код"
[ ${#NEED_PKGS[@]} -eq 0 ] || dnf -y -q install "${NEED_PKGS[@]}"
getent group homerepo >/dev/null || groupadd -r homerepo
id homerepo >/dev/null 2>&1 || useradd -r -g homerepo -d "$STATE" -s /sbin/nologin -c "HomeRepo panel" homerepo
id homerepo-signer >/dev/null 2>&1 || useradd -r -U -d "$SIGNER_HOME" -s /sbin/nologin -c "HomeRepo signer" homerepo-signer
usermod -aG homerepo homerepo-signer
id homerepo-runner >/dev/null 2>&1 || useradd -r -U -d "$RUNNER_HOME" -s /sbin/nologin -c "HomeRepo Ansible runner" homerepo-runner
usermod -aG homerepo homerepo-runner
if [ -n "$ANSIBLE_PKG" ]; then
  dnf -y -q install "$ANSIBLE_PKG" >/dev/null 2>&1 && ok "установлен $ANSIBLE_PKG: $(ansible-playbook --version 2>/dev/null | head -1)" \
    || warn "не удалось установить $ANSIBLE_PKG — поставьте позже: dnf install ansible-core"
fi
install -d -m 755 /opt/homerepo
rm -rf /opt/homerepo/lib /opt/homerepo/web /opt/homerepo/agent
cp -a "$SRC/lib" "$SRC/web" "$SRC/agent" /opt/homerepo/
mkdir -p /opt/homerepo/deploy && cp -a "$SRC/deploy/rollback.sh" /opt/homerepo/deploy/
find /opt/homerepo -name __pycache__ -prune -exec rm -rf {} +
chown -R root:root /opt/homerepo; chmod -R u=rwX,go=rX /opt/homerepo
install -m 755 "$SRC/bin/homerepo" /usr/local/bin/homerepo
install -m 755 "$SRC/bin/homerepo-inventory" /usr/local/bin/homerepo-inventory
install -d -m 2770 -o root -g homerepo "$STATE" "$STATE/incoming" "$STATE/run-secrets"
install -d -m 700 -o homerepo-runner -g homerepo-runner "$RUNNER_HOME"
install -d -m 700 -o homerepo-signer -g homerepo-signer "$SIGNER_HOME" "$GNUPG_NEW"
ok "код в /opt/homerepo, команда homerepo"

say "3. ключ подписи и права на репозиторий"
if [ "$KEY_MODE" = import ] || [ "$KEY_MODE" = migrate ]; then
  runuser -u homerepo-signer -- gpg --homedir "$GNUPG_NEW" --batch --import < "$BACKUP/signing-key.secret.asc" 2>&1 | sed 's/^/    /'
  echo "$KEY_ID:6:" | runuser -u homerepo-signer -- gpg --homedir "$GNUPG_NEW" --batch --import-ownertrust 2>/dev/null || true
elif [ "$KEY_MODE" = generate ]; then
  runuser -u homerepo-signer -- gpg --homedir "$GNUPG_NEW" --batch --pinentry-mode loopback --passphrase '' \
    --quick-gen-key "$KEY_NAME" rsa4096 sign,cert never 2>&1 | grep -v '^gpg: \(directory\|keybox\|revocation\)' | sed 's/^/    /' || true
  KEY_ID=$(runuser -u homerepo-signer -- gpg --homedir "$GNUPG_NEW" --batch --with-colons --list-secret-keys | awk -F: '/^fpr/{print $10; exit}')
  [ -n "$KEY_ID" ] || die "не удалось создать ключ подписи"
  (umask 077; runuser -u homerepo-signer -- gpg --homedir "$GNUPG_NEW" --batch --armor --export-secret-keys "$KEY_ID" > "$BACKUP/signing-key.secret.asc")
  ok "создан ключ $KEY_ID; секретная копия: $BACKUP/signing-key.secret.asc — сохраните её вне сервера"
fi
T=$(mktemp -d); chmod 755 "$T"; echo test > "$T/t"; chmod 644 "$T/t"
PASSARGS=(--pinentry-mode error); [ -f /etc/homerepo/signer.pass ] && PASSARGS=(--pinentry-mode loopback --passphrase-file /etc/homerepo/signer.pass)
if runuser -u homerepo-signer -- gpg --homedir "$GNUPG_NEW" --batch "${PASSARGS[@]}" -u "$KEY_ID" --output - --detach-sign "$T/t" >/dev/null 2>"$T/err"; then
  ok "служба подписи может подписывать ключом $KEY_ID"
else
  cat "$T/err" | sed 's/^/    /'; rm -rf "$T"
  die "тестовая подпись не удалась (если у ключа есть пароль: положите его в /etc/homerepo/signer.pass, chown homerepo-signer, chmod 600)"
fi
rm -rf "$T"
install -d -m 755 "$REPO_ROOT"
chown -R homerepo-signer:homerepo-signer "$REPO_ROOT"
find "$REPO_ROOT" -type d -exec chmod 755 {} + ; find "$REPO_ROOT" -type f -exec chmod 644 {} +
ok "репозиторий принадлежит homerepo-signer, остальным — только чтение"

say "4. конфигурация, сертификат, службы"
install -d -m 755 /etc/homerepo
if [ ! -f "$CONF" ]; then
  PF=""; [ -f /etc/homerepo/signer.pass ] && PF=/etc/homerepo/signer.pass
  cat > "$CONF" <<EOF
# HomeRepo $HR_VERSION — настройки (после изменения: systemctl restart homerepo-web homerepo-signer)
[repo]
root = $REPO_ROOT
url = $REPO_URL
arches = x86_64 aarch64
branches = os updates extras
default_branch = updates
repo_gpgcheck = no

[panel]
listen = 127.0.0.1
port = $PORT
public_url = $PUBLIC_URL
session_hours = 12
upload_max_gb = 8

[signer]
gnupg_home = $GNUPG_NEW
key_id = $KEY_ID
passphrase_file = $PF
sign_metadata = yes
workers = 2

[agent]
api_url = $PUBLIC_URL
ca_file = $TLS_DIR/server.crt
interval = 120
report_every = 900
EOF
  chmod 644 "$CONF"; ok "создан $CONF"
else
  ok "$CONF уже есть — не меняю"
fi
if [ ! -f "$TLS_DIR/server.crt" ]; then
  install -d -m 755 "$TLS_DIR"
  FQDN=$(hostname -f 2>/dev/null || uname -n); SHORT=${FQDN%%.*}
  SAN="DNS:$FQDN,DNS:$SHORT,DNS:localhost,IP:127.0.0.1"
  for ip in $(hostname -I 2>/dev/null || true); do case "$ip" in *:*) ;; *) SAN="$SAN,IP:$ip";; esac; done
  echo "$REPO_HOST" | grep -Eq '^[0-9.]+$' && ! echo "$SAN" | grep -q "IP:$REPO_HOST\b" && SAN="$SAN,IP:$REPO_HOST"
  echo "$REPO_HOST" | grep -Eq '[a-zA-Z]' && ! echo "$SAN" | grep -q "DNS:$REPO_HOST\b" && SAN="$SAN,DNS:$REPO_HOST"
  (umask 077; openssl req -x509 -newkey rsa:3072 -sha256 -days 3650 -nodes -subj "/CN=$FQDN/O=HomeRepo" \
     -addext "subjectAltName=$SAN" -addext "basicConstraints=critical,CA:TRUE" \
     -keyout "$TLS_DIR/server.key" -out "$TLS_DIR/server.crt" 2>/dev/null)
  chmod 600 "$TLS_DIR/server.key"; chmod 644 "$TLS_DIR/server.crt"
  ok "самоподписанный сертификат: $SAN (агенты доверяют ему через setup.sh)"
fi
CRT=$(conf_get ca_file); CRT=${CRT:-$TLS_DIR/server.crt}
KEY="${CRT%.crt}.key"; [ -f "$KEY" ] || KEY="$TLS_DIR/server.key"

for u in homerepo-signer homerepo-web homerepo-runner; do
  sed "s#@REPO_ROOT@#$REPO_ROOT#g" "$SRC/deploy/systemd/$u.service" > "/etc/systemd/system/$u.service"
done
systemctl daemon-reload
for d in "$STATE"/homerepo.db*; do [ -f "$d" ] && chown homerepo:homerepo "$d" && chmod 660 "$d"; done
systemctl enable -q homerepo-signer homerepo-web homerepo-runner
systemctl restart homerepo-signer homerepo-web
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/me" || true)
  [ "$code" = 401 ] && break; sleep 1
done
[ "${code:-}" = 401 ] || { journalctl -u homerepo-web -n 30 --no-pager; die "панель не запустилась"; }
systemctl is-active -q homerepo-signer || { journalctl -u homerepo-signer -n 30 --no-pager; die "служба подписи не запустилась"; }
systemctl restart homerepo-runner
sleep 2
systemctl is-active -q homerepo-runner && ok "исполнитель плейбуков работает (ключ: $RUNNER_HOME/.ssh/id_ed25519)" \
  || warn "исполнитель плейбуков не запустился: journalctl -u homerepo-runner -n 30"
ok "службы работают"
ADMIN_PW=""
if [ "$(runuser -u homerepo -- env PYTHONPATH=/opt/homerepo/lib HOMEREPO_CONF=$CONF python3 -c 'from homerepo import config, db; c=db.init(config.get().db_path); print(c.execute("select count(*) from users").fetchone()[0])')" = 0 ]; then
  ADMIN_PW=$(/usr/local/bin/homerepo user add admin --role admin | sed -n 's/^временный пароль: //p')
fi

say "5. веб-сервер ($WEB)"
NAMES="$REPO_HOST $(hostname -f 2>/dev/null || uname -n) $(hostname -s 2>/dev/null || true) $(hostname -I 2>/dev/null || true) localhost 127.0.0.1"
NAMES=$(echo $NAMES | tr ' ' '\n' | grep -v ':' | awk '!s[$0]++' | tr '\n' ' ')
[ $SELINUX = 1 ] && { setsebool -P httpd_can_network_connect 1 && ok "SELinux: httpd_can_network_connect=on"; restorecon -R "$REPO_ROOT" /etc/homerepo 2>/dev/null || true; }
restore_httpd(){
  warn "возвращаю Apache в прежнее состояние"
  systemctl stop nginx 2>/dev/null || true
  for f in repos-web.conf homerepo.conf ssl.conf; do
    [ -f "$BACKUP/files/etc/httpd/conf.d/$f" ] && cp -a "$BACKUP/files/etc/httpd/conf.d/$f" /etc/httpd/conf.d/$f
  done
  systemctl enable -q httpd 2>/dev/null || true
  systemctl start httpd 2>/dev/null || true
  grep -q '^php-fpm enabled' "$BACKUP/services.txt" && systemctl enable --now php-fpm 2>/dev/null || true
}
if [ "$WEB" = nginx ]; then
  if ! command -v nginx >/dev/null; then
    dnf -y -q install nginx || { warn "nginx не установился — использую Apache"; WEB=httpd; }
  fi
fi
if [ "$WEB" = nginx ]; then
  sed -e "s#@REPO_ROOT@#$REPO_ROOT#g" -e "s#@PORT@#$PORT#g" -e "s#@NAMES@#$NAMES#g" -e "s#@CRT@#$CRT#g" -e "s#@KEY@#$KEY#g" \
    "$SRC/deploy/nginx-homerepo.conf.in" > /etc/nginx/conf.d/homerepo.conf
  if ! nginx -t 2>"$BACKUP/nginx-t.txt"; then
    cat "$BACKUP/nginx-t.txt" | sed 's/^/    /'
    rm -f /etc/nginx/conf.d/homerepo.conf
    die "конфигурация nginx не прошла проверку — Apache не тронут"
  fi
  systemctl disable --now httpd 2>/dev/null || true
  systemctl disable --now php-fpm 2>/dev/null || true
  systemctl enable -q nginx
  if ! systemctl restart nginx; then
    journalctl -u nginx -n 20 --no-pager | sed 's/^/    /'
    restore_httpd
    die "nginx не запустился (порт 80/443 занят?) — Apache возвращён"
  fi
else
  rm -f /etc/httpd/conf.d/repos-web.conf
  sed -e "s#@REPO_ROOT@#$REPO_ROOT#g" -e "s#@PORT@#$PORT#g" "$SRC/deploy/httpd-homerepo.conf.in" > /etc/httpd/conf.d/homerepo.conf
  if [ -f /etc/httpd/conf.d/ssl.conf ]; then
    sed -i -E "s#^(\s*SSLCertificateFile\s+).*#\1$CRT#; s#^(\s*SSLCertificateKeyFile\s+).*#\1$KEY#" /etc/httpd/conf.d/ssl.conf
  fi
  if ! httpd -t 2>"$BACKUP/httpd-t.txt"; then
    cat "$BACKUP/httpd-t.txt" | sed 's/^/    /'; restore_httpd; die "конфигурация Apache не прошла проверку — возвращена прежняя"
  fi
  systemctl disable --now php-fpm 2>/dev/null || true
  systemctl enable -q httpd; systemctl restart httpd || { restore_httpd; die "Apache не перезапустился — возвращена прежняя конфигурация"; }
fi
sleep 1
c1=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1/homerepo/RPM-GPG-KEY-HOMEREPO" || true)
c2=$(curl -sk -o /dev/null -w '%{http_code}' "https://127.0.0.1/api/me" || true)
[ "$c1" = 200 ] && ok "репозиторий отдаётся: http://…/homerepo/" || warn "http://127.0.0.1/homerepo/RPM-GPG-KEY-HOMEREPO → $c1 (ключ появится после запуска службы подписи)"
[ "$c2" = 401 ] && ok "панель отвечает по HTTPS" || warn "https://127.0.0.1/api/me → $c2"
if systemctl is-active -q firewalld 2>/dev/null; then
  firewall-cmd -q --permanent --add-service=http --add-service=https 2>/dev/null && firewall-cmd -q --reload && ok "firewalld: http и https открыты"
fi

say "6. отключение старой системы"
if systemctl list-unit-files update-homerepo.timer >/dev/null 2>&1; then
  systemctl disable --now update-homerepo.timer 2>/dev/null && ok "таймер update-homerepo отключён (метаданные теперь пересобирает служба подписи)" || true
fi
for f in $SUDO_FILES; do
  tmp=$(mktemp)
  sed -E 's/^(\s*apache\s.*)$/# отключено HomeRepo '"$TS"': \1/' "$f" > "$tmp"
  if visudo -cf "$tmp" >/dev/null; then cat "$tmp" > "$f"; ok "sudo для apache отключён в $f"; else warn "$f: проверка visudo не прошла, оставлен как есть"; fi
  rm -f "$tmp"
done
if [ -n "$OLD_ANSIBLE" ]; then
  systemctl disable --now repo-panel 2>/dev/null && ok "старая панель Ansible (repo-panel, порт 5000) остановлена и отключена; файлы /opt/repo-panel не тронуты" || true
fi
if [ -n "$OLD_PLAYBOOKS" ]; then
  /usr/local/bin/homerepo playbook import $OLD_PLAYBOOKS 2>&1 | sed 's/^/    /' || true
  [ -f /opt/ansible-repo/inventories/hosts.ini ] && ok "список хостов теперь берётся из HomeRepo; старый инвентарь: /opt/ansible-repo/inventories/hosts.ini (для командной строки: homerepo-inventory)"
fi
if [ -n "$OLD_PANEL" ]; then
  mv "$OLD_PANEL" "$BACKUP/repos-web" && ok "старая панель перенесена в $BACKUP/repos-web (там пароли и журналы — каталог доступен только root)"
fi
for f in /usr/local/bin/master-repo-manager /usr/local/bin/master-repo-manager.backup /usr/local/bin/update-homerepo-smart.sh /usr/local/bin/update-homerepo.sh.bak; do
  [ -f "$f" ] && { mkdir -p "$BACKUP/bin"; mv "$f" "$BACKUP/bin/"; }
done
ok "старые скрипты перенесены в $BACKUP/bin"
if [ -d "$OLD_GNUPG/private-keys-v1.d" ] && { [ -s "$BACKUP/signing-key.secret.asc" ] || [ $HAVE_NEW_KEY = 1 ]; }; then
  $OLD_GPG --homedir "$OLD_GNUPG" --batch --list-keys >/dev/null 2>&1 || true
  ${OLD_GPG/gpg/gpgconf} --homedir "$OLD_GNUPG" --kill gpg-agent 2>/dev/null || true
  mv "$OLD_GNUPG" "$BACKUP/old-apache-gnupg" && ok "ключ удалён из домашнего каталога apache (копия в $BACKUP)"
fi

say "7. репозиторий"
/usr/local/bin/homerepo rescan --no-wait >/dev/null && ok "сканирование и пересборка метаданных запущены (homerepo status — ход работы)"

echo
printf '%sГотово.%s HomeRepo %s\n' "$C_OK" "$C_0" "$HR_VERSION"
echo "  Панель:        $PUBLIC_URL/   (сертификат самоподписанный — браузер предупредит один раз)"
if [ -n "$ADMIN_PW" ]; then
  echo "  Вход:          admin / $ADMIN_PW   ← временный пароль, панель попросит сменить"
fi
echo "  Репозиторий:   $REPO_URL (адреса для хостов не изменились)"
echo "  Резервная копия и откат: $BACKUP  →  sudo /opt/homerepo/deploy/rollback.sh $BACKUP"
echo "  Состояние:     homerepo status;  journalctl -u homerepo-signer -u homerepo-web"
