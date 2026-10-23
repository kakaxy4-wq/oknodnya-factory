#!/usr/bin/env bash
# Установка фабрики «Окно дня» на сервер (Ubuntu/Debian), запуск от root:
#   bash /opt/oknodnya-factory/deploy/install.sh
# Не трогает /opt/kaxy4 и соседние сервисы; работает от отдельного системного пользователя okno.
set -euo pipefail
REPO_URL="${REPO_URL:-https://github.com/kakaxy4-wq/oknodnya-factory.git}"
DIR=/opt/oknodnya-factory
SITE=/opt/lofi-goroda-public

echo "== пакеты"
apt-get update -q
apt-get install -y -q ffmpeg rhvoice rhvoice-russian python3-venv git nodejs npm

echo "== пользователь okno"
id okno >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin okno

echo "== код"
[ -d "$DIR/.git" ] || git clone "$REPO_URL" "$DIR"
[ -d "$SITE/.git" ] || git clone --depth 1 https://github.com/kakaxy4-wq/lofi-goroda-public "$SITE"
chown -R okno:okno "$DIR" "$SITE"

echo "== python и браузер"
sudo -u okno python3 -m venv "$DIR/.venv"
sudo -u okno "$DIR/.venv/bin/pip" install -q pillow numpy scipy
cd "$DIR/recorder"
sudo -u okno npm install --silent
npx --yes playwright@1.48.2 install-deps chromium
sudo -u okno npx playwright install chromium

echo "== настройки: токен хранится только здесь, в git его нет"
if [ ! -f /etc/oknodnya.env ]; then
  install -m 600 -o okno -g okno "$DIR/deploy/oknodnya.env.example" /etc/oknodnya.env
fi

echo "== расписание: systemd-таймеры по московскому времени"
install -m 644 "$DIR"/deploy/systemd/* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now okno-build.timer okno-publish@1.timer okno-publish@2.timer okno-publish@3.timer
systemctl list-timers 'okno-*' --no-pager

echo
echo "Готово. Осталось вписать токен бота: nano /etc/oknodnya.env"
echo "Проверка без публикации: sudo -u okno $DIR/.venv/bin/python $DIR/pipeline.py build 2026-10-24"
