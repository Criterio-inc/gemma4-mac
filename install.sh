#!/usr/bin/env bash
set -euo pipefail

# gemma-mlx installer (macOS / Apple Silicon)
#  - creates ./venv
#  - installs mlx-lm (git main) + mlx-vlm + flask
#  - generates ./bin/gemma, gemma-photos, gemma-yearbook, gemma-web wrappers
#  - generates ./Gemma.command (double-click launcher for the web UI)
#  - adds aliases to ~/.zshrc (idempotent, marker-delimited block)

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; RESET=$'\033[0m'

note()  { echo "${BOLD}==>${RESET} $*"; }
ok()    { echo "${GREEN}✓${RESET} $*"; }
warn()  { echo "${YELLOW}!${RESET} $*"; }
fail()  { echo "${RED}✗${RESET} $*" >&2; exit 1; }

# ---- preflight ----
[[ "$(uname -s)" == "Darwin" ]] || fail "This installer is macOS only."
[[ "$(uname -m)" == "arm64"  ]] || fail "Apple Silicon (M-series) required — Intel Macs are not supported."

# Find a Python >= 3.10. macOS ships /usr/bin/python3 as 3.9 (too old for the
# mlx gemma4 model files), so don't just grab `python3` — probe a list of
# likely interpreters (newest first) and pick the first one that qualifies.
# Honour an explicit override: PYTHON=/path/to/python ./install.sh
py_ok() {  # py_ok <interpreter> -> prints "X.Y" and succeeds if >= 3.10
  local p="$1" v
  command -v "$p" >/dev/null 2>&1 || return 1
  v="$("$p" -c 'import sys; print("%d.%d"%sys.version_info[:2])' 2>/dev/null)" || return 1
  local maj="${v%%.*}" min="${v#*.}"
  (( maj > 3 || (maj == 3 && min >= 10) )) || return 1
  echo "$v"
}

PY=""; PYV=""
CANDIDATES=(
  ${PYTHON:-}
  python3.13 python3.12 python3.11 python3.10
  /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12
  /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.10
  /opt/homebrew/bin/python3
  python3
)
for cand in "${CANDIDATES[@]}"; do
  [[ -n "$cand" ]] || continue
  if PYV="$(py_ok "$cand")"; then
    PY="$(command -v "$cand")"
    break
  fi
done

if [[ -z "$PY" ]]; then
  sys="$(command -v python3 || echo none)"
  sysv="$([[ "$sys" != none ]] && "$sys" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "?")"
  fail "Python >= 3.10 required (default python3 is $sysv at $sys).
    Install a newer Python and re-run:
      ${BOLD}brew install python@3.12${RESET}
    or point the installer at an existing one:
      ${BOLD}PYTHON=/path/to/python3.12 ./install.sh${RESET}"
fi
ok "Python $PYV at $PY"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$REPO_DIR/venv"
BINDIR="$REPO_DIR/bin"

# ---- venv ----
if [[ ! -x "$VENV/bin/python" ]]; then
  note "Creating virtual environment in $VENV"
  "$PY" -m venv "$VENV"
fi
ok "Virtualenv ready"

# ---- deps ----
note "Installing dependencies (a few minutes on first run)"
"$VENV/bin/pip" install --quiet --upgrade pip
# mlx-lm git main has the gemma4 model files; PyPI lags behind for new architectures.
# osxphotos: read Photos library directly so iCloud-only items can be analysed.
# pillow-heif: HEIC support for PIL (iPhone originals are HEIC by default).
# holidays: country-aware holiday calendars for the yearbook curator.
# flask: serves the local web UI (webapp.py) so everyday use needs no terminal.
"$VENV/bin/pip" install --quiet --upgrade \
    "git+https://github.com/ml-explore/mlx-lm.git" \
    "mlx-vlm" \
    "osxphotos" \
    "pillow-heif" \
    "holidays" \
    "imagehash" \
    "flask"
ok "Python dependencies installed"

# ---- wrappers ----
mkdir -p "$BINDIR"
cat > "$BINDIR/gemma" <<EOF
#!/usr/bin/env bash
exec "$VENV/bin/python" "$REPO_DIR/gemma.py" "\$@"
EOF
cat > "$BINDIR/gemma-photos" <<EOF
#!/usr/bin/env bash
exec "$VENV/bin/python" "$REPO_DIR/photos_caption.py" "\$@"
EOF
cat > "$BINDIR/gemma-yearbook" <<EOF
#!/usr/bin/env bash
exec "$VENV/bin/python" "$REPO_DIR/yearbook.py" "\$@"
EOF
cat > "$BINDIR/gemma-web" <<EOF
#!/usr/bin/env bash
exec "$VENV/bin/python" "$REPO_DIR/webapp.py" "\$@"
EOF
chmod +x "$BINDIR/gemma" "$BINDIR/gemma-photos" "$BINDIR/gemma-yearbook" "$BINDIR/gemma-web"
ok "Wrappers written to $BINDIR"

# ---- double-clickable launcher (no terminal needed for everyday use) ----
# Finder runs *.command in Terminal; this starts the web UI and opens the
# browser. Users who don't want the terminal at all can keep this in the Dock.
cat > "$REPO_DIR/Gemma.command" <<EOF
#!/usr/bin/env bash
# Double-click in Finder to launch the gemma4-mac web UI.
exec "$VENV/bin/python" "$REPO_DIR/webapp.py"
EOF
chmod +x "$REPO_DIR/Gemma.command"
ok "Launcher written to $REPO_DIR/Gemma.command (double-click in Finder)"

# ---- shell aliases (zsh only — macOS default since Catalina) ----
ZSHRC="$HOME/.zshrc"
START="# >>> gemma-mlx (managed by install.sh) >>>"
END="# <<< gemma-mlx <<<"

# Strip any previous managed block before appending the new one.
if [[ -f "$ZSHRC" ]] && grep -qF "$START" "$ZSHRC"; then
  awk -v s="$START" -v e="$END" '
    $0==s {skip=1; next}
    $0==e {skip=0; next}
    !skip {print}
  ' "$ZSHRC" > "$ZSHRC.tmp" && mv "$ZSHRC.tmp" "$ZSHRC"
fi

cat >> "$ZSHRC" <<EOF

$START
alias gemma='$BINDIR/gemma'
alias gemma-photos='$BINDIR/gemma-photos'
alias gemma-yearbook='$BINDIR/gemma-yearbook'
alias gemma-web='$BINDIR/gemma-web'
$END
EOF
ok "Aliases added to ~/.zshrc"

echo
echo "${BOLD}${GREEN}Installation complete.${RESET}"
echo
echo "Open a new terminal (or run: ${BOLD}source ~/.zshrc${RESET}) and try:"
echo "  ${BOLD}gemma${RESET} 'hej, vem är du?'"
echo "  ${BOLD}gemma${RESET} -i path/to/photo.jpg 'beskriv vad du ser'"
echo "  ${BOLD}gemma-photos${RESET} --dry-run    # after selecting photos in Photos.app"
echo
echo "Prefer a graphical UI instead of the terminal?"
echo "  ${BOLD}gemma-web${RESET}                  # starts the local web UI + opens your browser"
echo "  …or just ${BOLD}double-click Gemma.command${RESET} in Finder."
echo
echo "First run will download the model (~3.5 GB) from Hugging Face."
