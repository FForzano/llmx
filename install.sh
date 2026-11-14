#!/usr/bin/env bash
# Install / remove llmx: skill in ~/.claude/skills, CLI as ~/.local/bin/llmx.   ./install.sh [--copy] [--uninstall] [--status]
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"; BIN="${LLMX_BIN_DIR:-$HOME/.local/bin}"
mode=link; action=install
for a in "$@"; do case "$a" in --copy) mode=copy;; --uninstall) action=uninstall;; --status) action=status;; *) echo "unknown: $a" >&2; exit 2;; esac; done
t="$DEST/llmx"
case "$action" in
  install) mkdir -p "$DEST" "$BIN"
    [ -e "$t" ] && [ ! -L "$t" ] && mv "$t" "$t.bak.$(date +%Y%m%d%H%M%S)"; [ -L "$t" ] && rm "$t"
    if [ $mode = link ]; then ln -s "$REPO/skills/llmx" "$t"; else cp -R "$REPO/skills/llmx" "$t"; fi
    ln -sfn "$t/scripts/llmx.py" "$BIN/llmx"; chmod +x "$REPO/skills/llmx/scripts/llmx.py"
    echo "installed: $t, $BIN/llmx"; case ":$PATH:" in *":$BIN:"*) ;; *) echo "warning: $BIN not in PATH";; esac
    python3 -c 'import sys; assert sys.version_info>=(3,11)' 2>/dev/null || echo "WARNING: python3 >= 3.11 required"
    [ -f "$HOME/.config/llmx/config.toml" ] || echo "next: llmx init   (then edit ~/.config/llmx/config.toml; see skills/llmx/references/setup.md)";;
  uninstall) [ -L "$t" ] && rm "$t"; [ -L "$BIN/llmx" ] && rm "$BIN/llmx"; echo removed;;
  status) ls -ld "$t" "$BIN/llmx" 2>&1;;
esac
