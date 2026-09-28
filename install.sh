#!/usr/bin/env bash
# Kārya installer for macOS. One line from anywhere:
#
#   curl -fsSL https://raw.githubusercontent.com/Ketan3102/karya/main/install.sh | bash
#
# or from a clone:  ./install.sh
#
# It sets up, asking before each step that touches something of yours:
#   the board (http://127.0.0.1:8788, starts at login)
#   Claude Code and Codex: the karya MCP server, and the instructions that
#     make them track your work (~/.claude/CLAUDE.md, ~/.codex/AGENTS.md)
#   optional: the Slack / Gmail / Linear sync, the Friday recap email, the
#     Slack bot for phone reminders, and an import of your past GitHub PRs
#
#   ./install.sh            install or repair (safe to re-run)
#   ./install.sh --yes      accept the defaults, ask nothing (optional parts stay off)
#   ./install.sh update     pull the latest code and reload what is installed
#   ./install.sh uninstall  remove the jobs, the MCP entries and the instructions (keeps data/)
#   ./install.sh jobs | mcp redo just that part
#
# Written for the bash 3.2 that macOS ships. The whole script is read before
# anything runs (main is called on the last line), so a cut-off download
# does nothing.
set -euo pipefail

REPO_URL="${KARYA_REPO:-https://github.com/Ketan3102/karya.git}"
DEFAULT_DIR="${KARYA_DIR:-$HOME/karya}"
AGENTS="$HOME/Library/LaunchAgents"
LABELS="ai.karya.server ai.karya.sync ai.karya.catchup ai.karya.recap"
YES=0
ROOT=""
PY=""

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31mx %s\033[0m\n' "$*" >&2; exit 1; }

# ask "Question" default(y|n) -> 0 for yes. Reads the terminal, so it works under curl | bash.
ask() {
  local q="$1" def="${2:-y}" hint reply
  if [ "$YES" = 1 ] || ! [ -r /dev/tty ]; then [ "$def" = y ]; return; fi
  if [ "$def" = y ]; then hint="[Y/n]"; else hint="[y/N]"; fi
  read -r -p "$q $hint " reply </dev/tty || reply=""
  reply="$(printf '%s' "${reply:-$def}" | tr '[:upper:]' '[:lower:]')"
  [ "$reply" = y ] || [ "$reply" = yes ]
}

# prompt "Question" default -> prints the answer
prompt() {
  local q="$1" def="${2:-}" reply
  if [ "$YES" = 1 ] || ! [ -r /dev/tty ]; then printf '%s' "$def"; return; fi
  if [ -n "$def" ]; then read -r -p "$q [$def]: " reply </dev/tty || reply=""; else read -r -p "$q: " reply </dev/tty || reply=""; fi
  printf '%s' "${reply:-$def}"
}

# ------------------------------------------------------------------ bootstrap
# Run through curl | bash there is no checkout yet: clone one, then run its installer.
bootstrap() {
  command -v git >/dev/null 2>&1 || die "git is missing. Run: xcode-select --install   then try again."
  if [ -d "$DEFAULT_DIR/.git" ]; then
    say "Updating the Kārya checkout in $DEFAULT_DIR"
    git -C "$DEFAULT_DIR" pull --ff-only --quiet || warn "could not fast-forward $DEFAULT_DIR; using it as it is"
  elif [ -e "$DEFAULT_DIR" ]; then
    die "$DEFAULT_DIR exists and is not a Kārya checkout. Set KARYA_DIR=/another/path and run again."
  else
    say "Downloading Kārya into $DEFAULT_DIR"
    git clone --quiet "$REPO_URL" "$DEFAULT_DIR"
  fi
  exec bash "$DEFAULT_DIR/install.sh" "$@"
}

# ------------------------------------------------------------------- checks
find_python() {
  local py
  for py in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 2>/dev/null || true)" /usr/bin/python3; do
    if [ -z "$py" ] || ! [ -x "$py" ]; then continue; fi
    if "$py" -c 'import sqlite3, sys; assert sys.version_info >= (3, 9); sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(a)")' 2>/dev/null; then
      echo "$py"; return 0
    fi
  done
  return 1
}

checks() {
  [ "$(uname -s)" = Darwin ] || warn "Kārya's background jobs use macOS launchd. On $(uname -s) the board and MCP work; run python3 server.py yourself."
  PY="$(find_python)" || die "Need Python 3.9+ with SQLite FTS5. Install it with: xcode-select --install   (or: brew install python)"
  note "Python: $PY"
  case "$ROOT" in
    "$HOME/Desktop"*|"$HOME/Documents"*|"$HOME/Downloads"*)
      warn "Kārya is in $ROOT. macOS privacy protection blocks background jobs in Desktop, Documents and Downloads."
      warn "Move it (for example: mv \"$ROOT\" ~/karya) and run ~/karya/install.sh."
      ask "Install anyway (the board may not start at login)?" n || exit 1 ;;
  esac
}

# ------------------------------------------------------------------- config
write_config() {  # write_config key json-value [key json-value ...]; keeps the file private
  "$PY" - "$ROOT/config.json" "$@" <<'PY'
import json, os, sys
path, pairs = sys.argv[1], sys.argv[2:]
config = json.load(open(path)) if os.path.exists(path) else {}
for key, value in zip(pairs[::2], pairs[1::2]):
    config[key] = json.loads(value)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
os.chmod(path, 0o600)
PY
}

json_str() { "$PY" -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$1"; }

configure() {
  local name email tz claude_bin
  if "$PY" -c 'import json, sys; sys.exit(0 if json.load(open(sys.argv[1])).get("name") else 1)' "$ROOT/config.json" 2>/dev/null; then
    note "config.json is already set up"
    return
  fi
  say "About you (kept in $ROOT/config.json, readable only by you)"
  name="$(prompt "Your name" "$(id -F 2>/dev/null | awk '{print $1}' || true)")"
  email="$(prompt "Your email (for the Gmail sync and the recap)" "$(git config --global user.email 2>/dev/null || true)")"
  tz="$(readlink /etc/localtime 2>/dev/null | sed 's#.*zoneinfo/##' || true)"
  claude_bin="$(command -v claude 2>/dev/null || echo "$HOME/.local/bin/claude")"
  write_config name "$(json_str "$name")" email "$(json_str "$email")" timezone "$(json_str "${tz:-UTC}")" \
               claude_bin "$(json_str "$claude_bin")"
  note "saved: ${name:-no name}, ${email:-no email}, time zone ${tz:-UTC}"
}

port() { "$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); from store import load_config; print(load_config()["port"])' "$ROOT"; }

# ------------------------------------------------------------------ launchd
load_job() {
  local name="$1"
  [ "$(uname -s)" = Darwin ] || return 0
  mkdir -p "$AGENTS" "$ROOT/data"
  sed -e "s#__ROOT__#$ROOT#g" -e "s#__HOME__#$HOME#g" -e "s#__PYTHON__#$PY#g" "$ROOT/launchd/$name.plist" > "$AGENTS/$name.plist"
  launchctl bootout "gui/$(id -u)/$name" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$name.plist"
  note "loaded $name"
}

unload_job() {
  launchctl bootout "gui/$(id -u)/$1" 2>/dev/null || true
  rm -f "$AGENTS/$1.plist"
}

reload_jobs() {  # reload whatever is installed, and the board at least
  local name any=0
  for name in $LABELS; do
    if [ -f "$AGENTS/$name.plist" ]; then load_job "$name"; any=1; fi
  done
  if [ "$any" = 0 ]; then load_job ai.karya.server; fi
}

# ---------------------------------------------------------------------- MCP
register_mcp() {
  if command -v claude >/dev/null 2>&1; then
    claude mcp remove --scope user karya >/dev/null 2>&1 || true
    claude mcp add --scope user karya -- "$PY" "$ROOT/mcp_server.py" >/dev/null
    note "Claude Code: karya MCP server registered for all projects"
  else
    note "Claude Code not found; skipped (install it, then run ./install.sh mcp)"
  fi
  if command -v codex >/dev/null 2>&1 || [ -d "$HOME/.codex" ]; then
    mkdir -p "$HOME/.codex"
    "$PY" - "$HOME/.codex/config.toml" "$PY" "$ROOT/mcp_server.py" <<'PY'
import json, re, sys
path, python, server = sys.argv[1:]
try:
    text = open(path).read()
except FileNotFoundError:
    text = ""
block = f"[mcp_servers.karya]\ncommand = {json.dumps(python)}\nargs = [{json.dumps(server)}]\n"
pattern = re.compile(r"^\[mcp_servers\.karya\]\n(?:(?!\[).*\n?)*", re.M)
text = pattern.sub(lambda _: block + "\n", text, count=1) if pattern.search(text) else (text.rstrip() + "\n\n" if text.strip() else "") + block
open(path, "w").write(text.rstrip() + "\n")
PY
    note "Codex: karya MCP server registered in ~/.codex/config.toml"
  else
    note "Codex not found; skipped"
  fi
}

unregister_mcp() {
  if command -v claude >/dev/null 2>&1 && claude mcp remove --scope user karya >/dev/null 2>&1; then note "removed from Claude Code"; fi
  if [ -f "$HOME/.codex/config.toml" ]; then
    "$PY" - "$HOME/.codex/config.toml" <<'PY'
import re, sys
path = sys.argv[1]
text = open(path).read()
open(path, "w").write(re.sub(r"^\[mcp_servers\.karya\]\n(?:(?!\[).*\n?)*", "", text, flags=re.M).rstrip() + "\n")
PY
    note "removed from Codex"
  fi
}

# ------------------------------------------------------------ optional parts
sync_setup() {
  say "Sync (optional)"
  note "Three times a day a headless Claude run reads your Slack, Gmail and Linear (read-only) through the"
  note "connectors on your claude.ai account, and adds what people ask of you. It uses your Claude plan."
  if ask "Turn on the Slack / Gmail / Linear sync?" n; then
    local member
    member="$(prompt "Your Slack member ID (Slack > your profile > ... > Copy member ID; blank leaves Slack out)" "")"
    if [ -n "$member" ]; then write_config slack_user_id "$(json_str "$member")"; fi
    load_job ai.karya.sync
    load_job ai.karya.catchup
    note "connect Slack, Gmail and Linear at https://claude.ai/settings/connectors if you haven't yet"
    if ask "Also email yourself a recap of the week's work every Friday at 18:30?" n; then load_job ai.karya.recap; fi
  fi
}

slack_bot_setup() {
  if ask "Set up the Slack bot for reminders on your phone (about 10 minutes, see SLACK_SETUP.md)?" n; then
    if pbcopy < "$ROOT/slack-app-manifest.json" 2>/dev/null; then note "the app manifest is on your clipboard"; fi
    open "https://api.slack.com/apps?new_app=1" 2>/dev/null || true
    note "Create New App > From a manifest > your workspace > paste > Create > Install App > copy the xoxb- token."
    if ask "Done, and the token is copied?" y; then
      "$PY" "$ROOT/setup_slack.py" --from-clipboard || note "later: python3 $ROOT/setup_slack.py --from-clipboard"
    fi
  fi
}

backfill_setup() {
  command -v gh >/dev/null 2>&1 || return 0
  gh auth status >/dev/null 2>&1 || return 0
  if ask "Import your past GitHub PRs, so your history is on the board and searchable?" n; then
    local repos r args=""
    repos="$(prompt "Repos, space separated (owner/repo)" "")"
    for r in $repos; do args="$args --github $r"; done
    if [ -n "$args" ]; then
      # shellcheck disable=SC2086
      (cd "$ROOT" && "$PY" backfill_git.py $args --save) || warn "the import stopped; run python3 backfill_git.py --help"
    fi
  fi
}

# --------------------------------------------------------------------- main
install_all() {
  say "Installing Kārya in $ROOT"
  checks
  mkdir -p "$ROOT/data"
  configure
  say "The board"
  load_job ai.karya.server
  say "Claude Code and Codex"
  register_mcp
  if ask "Add Kārya's instructions to ~/.claude/CLAUDE.md and ~/.codex/AGENTS.md, so they track your work without being asked?" y; then
    "$PY" "$ROOT/update_instructions.py" | sed 's/^/  /'
  fi
  sync_setup
  slack_bot_setup
  backfill_setup
  local url
  url="http://127.0.0.1:$(port)"
  say "Done. Kārya is at $url"
  note "Start a new Claude Code or Codex session: the work you ask for is recorded as it happens."
  note "Update: $ROOT/install.sh update    Remove: $ROOT/install.sh uninstall"
  if [ "$YES" = 0 ]; then open "$url" 2>/dev/null || true; fi
}

main() {
  local cmd="install" arg here name
  for arg in "$@"; do
    case "$arg" in
      --yes|-y) YES=1 ;;
      install|update|uninstall|jobs|mcp) cmd="$arg" ;;
      -h|--help) printf '%s\n' "Kārya installer: ./install.sh [--yes] [install|update|uninstall|jobs|mcp]  (details at the top of install.sh)"; exit 0 ;;
      *) die "unknown option: $arg (try --help)" ;;
    esac
  done
  here="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" 2>/dev/null && pwd || true)"
  if [ -z "${BASH_SOURCE[0]:-}" ] || [ ! -f "$here/store.py" ]; then bootstrap "$@"; fi
  ROOT="$here"
  case "$cmd" in
    install) install_all ;;
    update)
      PY="$(find_python)" || die "Python 3.9+ not found"
      git -C "$ROOT" pull --ff-only
      reload_jobs
      register_mcp
      "$PY" "$ROOT/update_instructions.py" | sed 's/^/  /'
      say "Updated." ;;
    uninstall)
      PY="$(find_python)" || die "Python 3.9+ not found"
      for name in $LABELS; do unload_job "$name"; done
      note "background jobs removed"
      unregister_mcp
      "$PY" "$ROOT/update_instructions.py" --remove | sed 's/^/  /'
      say "Kārya is uninstalled. Your tasks are still in $ROOT/data; delete $ROOT to remove everything." ;;
    jobs) PY="$(find_python)" || die "Python 3.9+ not found"; reload_jobs ;;
    mcp)  PY="$(find_python)" || die "Python 3.9+ not found"; register_mcp ;;
  esac
}

main "$@"
