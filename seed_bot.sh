#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/opt/seed_bot"
VENV_BIN="$PROJECT_DIR/.venv/bin"
PYTHON="$VENV_BIN/python"
BOT_MAIN="$PROJECT_DIR/bot.py"
PID_FILE="/tmp/seed_bot.pid"
LOG_FILE="$PROJECT_DIR/bot.log"

cd "$PROJECT_DIR"

red()   { printf "\033[31m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
yellow(){ printf "\033[33m%s\033[0m\n" "$*"; }

is_running() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid=$(cat "$PID_FILE" 2>/dev/null || echo "")
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

start_bot() {
  if is_running; then
    yellow "Бот уже запущен (PID=$(cat "$PID_FILE"))."
    exit 0
  fi

  if [[ ! -x "$PYTHON" ]]; then
    red "Не найден venv ($PYTHON). Создай его и поставь зависимости:"
    echo "  cd $PROJECT_DIR"
    echo "  python3 -m venv .venv"
    echo "  .venv/bin/pip install -r requirements.txt"
    exit 1
  fi

  green "Запускаю бота…"
  nohup "$PYTHON" "$BOT_MAIN" >>"$LOG_FILE" 2>&1 &
  sleep 1

  if is_running; then
    green "Бот запущен (PID=$(cat "$PID_FILE")). Логи: $LOG_FILE"
  else
    red "Не удалось запустить бота. Смотри логи: $LOG_FILE"
    exit 1
  fi
}

stop_bot() {
  if ! is_running; then
    yellow "Бот не запущен."
    rm -f "$PID_FILE" >/dev/null 2>&1 || true
    exit 0
  fi
  local pid
  pid=$(cat "$PID_FILE")
  yellow "Останавливаю бота (PID=$pid)…"
  kill "$pid" 2>/dev/null || true

  for _ in {1..10}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    sleep 1
  done

  if kill -0 "$pid" 2>/dev/null; then
    yellow "Процесс не завершился, посылаю SIGKILL…"
    kill -9 "$pid" 2>/dev/null || true
  fi

  rm -f "$PID_FILE" >/dev/null 2>&1 || true
  green "Бот остановлен."
}

restart_bot() {
  stop_bot || true
  start_bot
}

status_bot() {
  if is_running; then
    green "Бот запущен (PID=$(cat "$PID_FILE"))."
  else
    yellow "Бот не запущен."
  fi
}

logs_bot() {
  if [[ ! -f "$LOG_FILE" ]]; then
    yellow "Файл логов ещё не создан: $LOG_FILE"
  fi
  tail -n 50 "$LOG_FILE" 2>/dev/null || true
}

follow_logs() {
  if [[ ! -f "$LOG_FILE" ]]; then
    yellow "Файл логов ещё не создан: $LOG_FILE"
  fi
  tail -f "$LOG_FILE"
}

case "${1:-}" in
  start)   start_bot ;;
  stop)    stop_bot ;;
  restart) restart_bot ;;
  status)  status_bot ;;
  logs)    logs_bot ;;
  follow)  follow_logs ;;
  *)
    cat <<EOF
Использование: $0 {start|stop|restart|status|logs|follow}

  start    — запустить бота (если ещё не запущен)
  stop     — корректно остановить бота
  restart  — перезапустить бота
  status   — показать, запущен ли бот
  logs     — показать последние 50 строк логов
  follow   — следить за логами (tail -f)

Текущие пути:
  проект : $PROJECT_DIR
  venv   : $VENV_BIN
  main   : $BOT_MAIN
  pid    : $PID_FILE
  лог    : $LOG_FILE
EOF
    exit 1
    ;;
esac

