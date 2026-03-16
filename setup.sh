#!/usr/bin/env bash
# ============================================================
# setup.sh — установка SEED-бота
#
# Что делает:
#   1. Создаёт Python venv и ставит зависимости
#   2. Создаёт пользователя MongoDB seed_bot с паролем
#   3. Инициализирует коллекции и индексы в MongoDB
#   4. Создаёт .env из .env.example если его ещё нет
#
# Использование:
#   bash setup.sh
#   bash setup.sh --mongo-host 192.168.1.10   # если монга не на localhost
#   bash setup.sh --no-mongo                  # только pip, без монги
# ============================================================
set -euo pipefail

# ── Цвета ──────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}▶ $*${RESET}"; }
ok()      { echo -e "${GREEN}✔ $*${RESET}"; }
warn()    { echo -e "${YELLOW}⚠ $*${RESET}"; }
err()     { echo -e "${RED}✖ $*${RESET}" >&2; }
header()  { echo -e "\n${BOLD}══════════════════════════════════════${RESET}"; \
            echo -e "${BOLD}  $*${RESET}"; \
            echo -e "${BOLD}══════════════════════════════════════${RESET}"; }

# ── Параметры по умолчанию ──────────────────────────────────
MONGO_HOST="localhost"
MONGO_PORT="27017"
MONGO_ADMIN_USER="admin"
MONGO_ADMIN_PASS="devil12M1991!"
MONGO_BOT_USER="seed_bot"
MONGO_BOT_PASS="seed_bot_$(openssl rand -hex 8)"  # генерируем случайный
MONGO_DB="seed_bot"
SETUP_MONGO=true
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Разбор аргументов ───────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mongo-host)     MONGO_HOST="$2"; shift 2 ;;
    --mongo-port)     MONGO_PORT="$2"; shift 2 ;;
    --mongo-db)       MONGO_DB="$2"; shift 2 ;;
    --bot-user)       MONGO_BOT_USER="$2"; shift 2 ;;
    --bot-pass)       MONGO_BOT_PASS="$2"; shift 2 ;;
    --no-mongo)       SETUP_MONGO=false; shift ;;
    -h|--help)
      echo "Usage: bash setup.sh [--mongo-host HOST] [--mongo-port PORT]"
      echo "                     [--mongo-db DB] [--bot-user USER] [--bot-pass PASS]"
      echo "                     [--no-mongo]"
      exit 0 ;;
    *) err "Unknown argument: $1"; exit 1 ;;
  esac
done

MONGO_URI="mongodb://${MONGO_BOT_USER}:${MONGO_BOT_PASS}@${MONGO_HOST}:${MONGO_PORT}/${MONGO_DB}?authSource=${MONGO_DB}"
MONGO_ADMIN_URI="mongodb://${MONGO_ADMIN_USER}:${MONGO_ADMIN_PASS}@${MONGO_HOST}:${MONGO_PORT}/admin"

cd "$SCRIPT_DIR"

# ══════════════════════════════════════
header "1. Python virtualenv + зависимости"
# ══════════════════════════════════════

if [[ ! -d ".venv" ]]; then
  info "Создаём .venv..."
  python3 -m venv .venv
  ok ".venv создан"
else
  ok ".venv уже существует"
fi

info "Устанавливаем зависимости..."
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q
ok "Зависимости установлены"

# ══════════════════════════════════════
header "2. MongoDB"
# ══════════════════════════════════════

if [[ "$SETUP_MONGO" == "false" ]]; then
  warn "Пропускаем настройку MongoDB (--no-mongo)"
else
  # Проверяем доступность монги
  info "Проверяем подключение к MongoDB ${MONGO_HOST}:${MONGO_PORT}..."
  if ! mongosh --quiet \
       --host "$MONGO_HOST" --port "$MONGO_PORT" \
       -u "$MONGO_ADMIN_USER" -p "$MONGO_ADMIN_PASS" \
       --authenticationDatabase admin \
       --eval "db.runCommand({ping:1}).ok" 2>/dev/null | grep -q "^1$"; then
    err "Не могу подключиться к MongoDB. Проверь:"
    err "  - mongosh доступен (apt install mongodb-mongosh)"
    err "  - MongoDB запущена: sudo systemctl status mongod"
    err "  - Логин/пароль: ${MONGO_ADMIN_USER} / ${MONGO_ADMIN_PASS}"
    exit 1
  fi
  ok "MongoDB доступна"

  # Создаём пользователя seed_bot если его ещё нет
  info "Создаём пользователя '${MONGO_BOT_USER}' в БД '${MONGO_DB}'..."
  mongosh --quiet \
    --host "$MONGO_HOST" --port "$MONGO_PORT" \
    -u "$MONGO_ADMIN_USER" -p "$MONGO_ADMIN_PASS" \
    --authenticationDatabase admin \
    --eval "
      db = db.getSiblingDB('${MONGO_DB}');
      var existing = db.getUser('${MONGO_BOT_USER}');
      if (existing) {
        print('user_exists');
      } else {
        db.createUser({
          user: '${MONGO_BOT_USER}',
          pwd:  '${MONGO_BOT_PASS}',
          roles: [{role:'readWrite', db:'${MONGO_DB}'}]
        });
        print('user_created');
      }
    " 2>/dev/null | tail -1 | while read -r line; do
      case "$line" in
        user_created) ok "Пользователь '${MONGO_BOT_USER}' создан" ;;
        user_exists)
          warn "Пользователь '${MONGO_BOT_USER}' уже существует, сбрасываем пароль..."
          mongosh --quiet \
            --host "$MONGO_HOST" --port "$MONGO_PORT" \
            -u "$MONGO_ADMIN_USER" -p "$MONGO_ADMIN_PASS" \
            --authenticationDatabase admin \
            --eval "
              db = db.getSiblingDB('${MONGO_DB}');
              db.updateUser('${MONGO_BOT_USER}', {pwd:'${MONGO_BOT_PASS}'});
            " 2>/dev/null
          ok "Пароль обновлён"
          ;;
      esac
    done

  # Инициализируем коллекции и индексы
  info "Инициализируем коллекции и индексы..."
  .venv/bin/python scripts/mongo_init.py \
    --uri "$MONGO_ADMIN_URI" \
    --db  "$MONGO_DB"
  ok "Коллекции и индексы созданы"
fi

# ══════════════════════════════════════
header "3. Файл .env"
# ══════════════════════════════════════

if [[ -f ".env" ]]; then
  ok ".env уже существует — не трогаем"
else
  info "Создаём .env из .env.example..."
  cp .env.example .env

  if [[ "$SETUP_MONGO" != "false" ]]; then
    # Вписываем MONGO_URI и MONGO_DB
    if grep -q "^# MONGO_URI=" .env; then
      sed -i \
        "s|^# MONGO_URI=.*|MONGO_URI=${MONGO_URI}|" \
        .env
      sed -i \
        "s|^# MONGO_DB=.*|MONGO_DB=${MONGO_DB}|" \
        .env
      ok "MONGO_URI вписан в .env"
    fi
  fi

  warn "Отредактируй .env: вставь токены Mattermost и GigaChat!"
fi

# ══════════════════════════════════════
header "Готово!"
# ══════════════════════════════════════

echo ""
echo -e "${BOLD}Данные MongoDB:${RESET}"
echo -e "  БД:         ${CYAN}${MONGO_DB}${RESET}"
echo -e "  Пользователь: ${CYAN}${MONGO_BOT_USER}${RESET}"
echo -e "  Пароль:     ${CYAN}${MONGO_BOT_PASS}${RESET}"
echo -e "  URI:        ${CYAN}${MONGO_URI}${RESET}"
echo ""
echo -e "${BOLD}Следующий шаг:${RESET}"
echo -e "  1. Отредактируй ${CYAN}.env${RESET} (токены Mattermost, GigaChat)"
echo -e "  2. Запусти бота:"
echo -e "     ${CYAN}. .venv/bin/activate && python -m bot${RESET}"
echo ""
