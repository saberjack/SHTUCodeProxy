#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="$(tr -d '\r\n' < VERSION 2>/dev/null || echo dev)"
OS_NAME="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH_NAME="$(uname -m)"
APP_NAME="SHTUCodeProxy-v${VERSION}-${OS_NAME}-${ARCH_NAME}"
CLI_NAME="shtucodeproxyctl-v${VERSION}-${OS_NAME}-${ARCH_NAME}"
HEADLESS_BUNDLE="SHTUCodeProxy-v${VERSION}-${OS_NAME}-${ARCH_NAME}-headless-cli"
FOLDER_NAME="SHTUCodeProxy"
PACKAGE_ROOT="${APP_NAME}-python-launcher"

INSTALL_DEPS=0
ONE_DIR_ONLY=0
ONE_FILE_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --install-deps) INSTALL_DEPS=1 ;;
    --onedir-only) ONE_DIR_ONLY=1 ;;
    --onefile-only) ONE_FILE_ONLY=1 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if [[ "$INSTALL_DEPS" == "1" ]]; then
  python3 -m pip install --upgrade pip
  python3 -m pip install -r requirements-build.txt
fi

mkdir -p release

PYINSTALLER_LINUX_BINARIES=()
if [[ "$OS_NAME" == "linux" ]]; then
  collect_library() {
    local name="$1"
    local path=""
    path="$(ldconfig -p 2>/dev/null | awk -v lib="$name" '$1 == lib { print $NF; exit }' || true)"
    if [[ -n "$path" && -f "$path" ]]; then
      PYINSTALLER_LINUX_BINARIES+=(--add-binary "$path:.")
    fi
  }

  for library in \
    libGL.so.1 \
    libEGL.so.1 \
    libGLdispatch.so.0 \
    libGLX.so.0 \
    libOpenGL.so.0 \
    libglib-2.0.so.0 \
    libX11.so.6 \
    libXext.so.6 \
    libXrender.so.1 \
    libxcb.so.1 \
    libxcb-cursor.so.0 \
    libxcb-icccm.so.4 \
    libxcb-image.so.0 \
    libxcb-keysyms.so.1 \
    libxcb-randr.so.0 \
    libxcb-render-util.so.0 \
    libxcb-shape.so.0 \
    libxcb-xinerama.so.0 \
    libxkbcommon.so.0 \
    libxkbcommon-x11.so.0; do
    collect_library "$library"
  done
fi

if [[ "$ONE_FILE_ONLY" != "1" ]]; then
  python3 -m PyInstaller \
    --noconfirm \
    --clean \
    --name "$FOLDER_NAME" \
    --icon "build/shtucodeproxy.ico" \
    "${PYINSTALLER_LINUX_BINARIES[@]}" \
    --add-data "build/shtucodeproxy.ico:assets" \
    --add-data "src/proxy.py:." \
    --add-data "src/cli.py:." \
    --add-data "src/pyqt_gui.py:." \
    --add-data "src/platform_utils.py:." \
    --add-data "src/config_store.py:." \
    --add-data "src/safe_io.py:." \
    --add-data "VERSION:." \
    --add-data "docs/headless-config.example.json:." \
    app.py

  rm -rf "build/$PACKAGE_ROOT"
  mkdir -p "build/$PACKAGE_ROOT"
  cp -R "dist/$FOLDER_NAME" "build/$PACKAGE_ROOT/$FOLDER_NAME"
  cp src/linux_launcher.py "build/$PACKAGE_ROOT/run_shtucodeproxy.py"
  cp "build/shtucodeproxy.ico" "build/$PACKAGE_ROOT/shtucodeproxy.ico"
  cp docs/headless-config.example.json "build/$PACKAGE_ROOT/headless-config.example.json"
  cat > "build/$PACKAGE_ROOT/shtucodeproxy.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=SHTUCodeProxy
Comment=Claude Code and Codex local bridge
Exec=python3 run_shtucodeproxy.py
Icon=shtucodeproxy
Terminal=false
Categories=Development;Utility;
StartupWMClass=SHTUCodeProxy
EOF
  cat > "build/$PACKAGE_ROOT/README-LINUX.txt" <<EOF
SHTUCodeProxy v${VERSION} Linux package

Usage:
1. Extract this tar.gz package.
2. Run: python3 run_shtucodeproxy.py

Headless CLI examples:
- python3 run_shtucodeproxy.py configure-model --model-id glm-chat --api-key YOUR_KEY --upstream-model glm-chat --api-format chat_completions --default --codex
- cp docs/headless-config.example.json config.json; edit config.json; python3 run_shtucodeproxy.py apply-config config.json --write-claude --write-codex --start
- python3 run_shtucodeproxy.py start
- python3 run_shtucodeproxy.py status
- python3 run_shtucodeproxy.py stop

The bundled SHTUCodeProxy runtime includes Python, PyQt5, and Qt libraries.
You do not need to install Python packages such as PyQt5 or PyInstaller.

Optional desktop icon:
- Copy shtucodeproxy.ico to ~/.local/share/icons/shtucodeproxy.ico
- Copy shtucodeproxy.desktop to ~/.local/share/applications/shtucodeproxy.desktop
- Edit the Exec path in the desktop file to this extracted folder if needed.
EOF
  tar -cJf "release/${APP_NAME}-python-launcher.tar.xz" -C build "$PACKAGE_ROOT"
  echo "Python-launcher folder package complete: release/${APP_NAME}-python-launcher.tar.xz"
fi

if [[ "$ONE_DIR_ONLY" != "1" ]]; then
  python3 -m PyInstaller \
    --noconfirm \
    --clean \
    --onefile \
    --console \
    --name "$CLI_NAME" \
    --add-data "src/proxy.py:." \
    --add-data "src/cli.py:." \
    --add-data "src/platform_utils.py:." \
    --add-data "src/config_store.py:." \
    --add-data "src/safe_io.py:." \
    --add-data "VERSION:." \
    --add-data "docs/headless-config.example.json:." \
    src/cli.py
  cp "dist/$CLI_NAME" "release/$CLI_NAME"
  rm -rf "build/$HEADLESS_BUNDLE"
  mkdir -p "build/$HEADLESS_BUNDLE"
  cp "dist/$CLI_NAME" "build/$HEADLESS_BUNDLE/$CLI_NAME"
  cp docs/headless-config.example.json "build/$HEADLESS_BUNDLE/headless-config.example.json"
  cp docs/headless-config.example.json "build/$HEADLESS_BUNDLE/config.json"
  cat > "build/$HEADLESS_BUNDLE/README-HEADLESS.txt" <<EOF
SHTUCodeProxy v${VERSION} headless Linux CLI package

Files:
- $CLI_NAME: no-GUI Linux CLI executable
- headless-config.example.json: full model template
- config.json: editable config copy

Quick start:
1. chmod +x ./$CLI_NAME
2. Edit config.json and replace PUT_YOUR_API_KEY_HERE.
3. ./$CLI_NAME apply-config config.json --write-claude --write-codex --start
4. ./$CLI_NAME status
5. ./$CLI_NAME stop
EOF
  (cd build && zip -qr "../release/${HEADLESS_BUNDLE}.zip" "$HEADLESS_BUNDLE")
  echo "Headless CLI build complete: release/$CLI_NAME"
  echo "Headless CLI bundle complete: release/${HEADLESS_BUNDLE}.zip"

  python3 -m PyInstaller \
    --noconfirm \
    --clean \
    --onefile \
    --name "$APP_NAME" \
    --icon "build/shtucodeproxy.ico" \
    "${PYINSTALLER_LINUX_BINARIES[@]}" \
    --add-data "build/shtucodeproxy.ico:assets" \
    --add-data "src/proxy.py:." \
    --add-data "src/cli.py:." \
    --add-data "src/pyqt_gui.py:." \
    --add-data "src/platform_utils.py:." \
    --add-data "src/config_store.py:." \
    --add-data "src/safe_io.py:." \
    --add-data "VERSION:." \
    --add-data "docs/headless-config.example.json:." \
    app.py

  cp "dist/$APP_NAME" "release/$APP_NAME"
  echo "Single-file build complete: release/$APP_NAME"
fi

find release -type f \( -name '*_probe.py' -o -name 'local_key_probe.py' \) -delete 2>/dev/null || true
find release -type d -name 'test_support' -prune -exec rm -rf {} + 2>/dev/null || true

echo "Build complete. Release files are in: $(cd "$ROOT/.." && pwd)/release"
