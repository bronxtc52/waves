#!/usr/bin/env bash
# Установка скилла wave-autobot для Claude Code.
#
# 1. Проверяет зависимости: claude, tmux >= 3.2, gh (и что он авторизован), git >= 2.36,
#    python3 >= 3.10, модуль rich. Все нехватки печатаются разом, затем выход с кодом 1.
# 2. Ставит симлинк ~/.claude/skills/wave-autobot на каталог, где лежит этот скрипт.
#    Симлинк уже туда же — ничего не делает (повторный запуск безопасен).
#    Симлинк в другое место — заменяется с сообщением, куда он указывал раньше (симлинк данных не
#    хранит: прежний каталог не трогается).
#    Каталог или файл на этом месте — не трогается: ошибка и код 1.
#
# Без sudo; сеть — только внутри `gh auth status`. Переменные: HOME (куда ставить).
set -euo pipefail

errors=()
add_error() { errors+=("$1"); }

# tmux -V → "tmux 3.4", "tmux 3.2a", "tmux next-3.5", "tmux master"
version_ok() {  # version_ok <текст версии> <нужно major> <нужно minor>
  local text=$1 need_major=$2 need_minor=$3
  if [[ $text == *master* ]]; then
    return 0
  fi
  if [[ $text =~ ([0-9]+)\.([0-9]+) ]]; then
    local major=${BASH_REMATCH[1]} minor=${BASH_REMATCH[2]}
    if (( major > need_major || (major == need_major && minor >= need_minor) )); then
      return 0
    fi
  fi
  return 1
}

if ! command -v claude >/dev/null 2>&1; then
  add_error "не найден claude (Claude Code CLI). Установите Claude Code: https://docs.claude.com/en/docs/claude-code/setup"
fi

if ! command -v tmux >/dev/null 2>&1; then
  add_error "не найден tmux (нужен >= 3.2). Установите: sudo apt-get install tmux (Debian/Ubuntu) или brew install tmux (macOS)"
else
  tmux_v=$(tmux -V 2>/dev/null || true)
  if ! version_ok "$tmux_v" 3 2; then
    add_error "tmux слишком старый или версия не определена («${tmux_v:-нет ответа}»), нужен >= 3.2. Обновите tmux: brew install tmux (macOS) или пакет из свежего дистрибутива"
  fi
fi

if ! command -v gh >/dev/null 2>&1; then
  add_error "не найден gh (GitHub CLI). Установите: brew install gh (macOS) или https://cli.github.com (Linux)"
elif ! gh auth status >/dev/null 2>&1; then
  add_error "gh не авторизован (gh auth status вернул ошибку). Войдите: gh auth login"
fi

if ! command -v git >/dev/null 2>&1; then
  add_error "не найден git (нужен >= 2.36). Установите: sudo apt-get install git (Debian/Ubuntu) или brew install git (macOS)"
else
  git_v=$(git --version 2>/dev/null || true)
  if ! version_ok "$git_v" 2 36; then
    add_error "git слишком старый («${git_v:-нет ответа}»), нужен >= 2.36 (git worktree list -z). Обновите git"
  fi
fi

if ! command -v python3 >/dev/null 2>&1; then
  add_error "не найден python3 (нужен >= 3.10). Установите Python 3.10+: https://www.python.org/downloads/"
elif ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
  add_error "python3 старше 3.10 ($(python3 --version 2>&1 || true)). Установите Python 3.10+ и сделайте его python3 в PATH"
elif ! python3 -c 'import rich' >/dev/null 2>&1; then
  add_error "нет модуля rich для python3 (нужен дашборду dash.py). Установите: python3 -m pip install --user rich"
fi

if (( ${#errors[@]} > 0 )); then
  echo "wave-autobot: установка не выполнена — не хватает зависимостей:" >&2
  for e in "${errors[@]}"; do
    echo "  - $e" >&2
  done
  echo "Исправьте и запустите ./install.sh снова." >&2
  exit 1
fi

src_dir=${BASH_SOURCE[0]%/*}
if [[ $src_dir == "${BASH_SOURCE[0]}" ]]; then
  src_dir=.
fi
src=$(cd "$src_dir" && pwd -P)
skills="$HOME/.claude/skills"
dst="$skills/wave-autobot"
mkdir -p "$skills"

if [[ -L $dst ]]; then
  current=$(cd "$dst" 2>/dev/null && pwd -P || true)
  if [[ $current == "$src" ]]; then
    echo "wave-autobot уже установлен: $dst -> $src"
  else
    old=$(readlink "$dst" 2>/dev/null || true)
    rm "$dst"
    ln -s "$src" "$dst"
    echo "Симлинк $dst указывал на «${old:-?}» — заменён: теперь -> $src"
  fi
elif [[ -e $dst ]]; then
  echo "ошибка: $dst уже существует и это не симлинк (каталог или файл). Ничего не тронуто." >&2
  echo "Уберите или переименуйте его вручную и запустите ./install.sh снова." >&2
  exit 1
else
  ln -s "$src" "$dst"
  echo "Установлено: $dst -> $src"
fi

cat <<EOF

Дальше:
  1. Клавиша Ctrl+\\ (открыть/закрыть окно текущей волны) — добавьте в ~/.tmux.conf строку
       source-file "$src/keys.tmux"
     и перечитайте конфиг: tmux source-file ~/.tmux.conf
  2. Быстрый старт — раздел «Быстрый старт за 5 шагов» в $src/README.md
     (пример плана и конфига — $src/examples/).
EOF
