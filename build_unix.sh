#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VERSION="$(tr -d '\r\n' < VERSION 2>/dev/null || echo dev)"
OS_NAME="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH_NAME="$(uname -m)"
APP_NAME="SHTUCodeProxy-v${VERSION}-${OS_NAME}-${ARCH_NAME}"
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

if [[ "$ONE_FILE_ONLY" != "1" ]]; then
  python3 -m PyInstaller \
    --noconfirm \
    --clean \
    --name "$FOLDER_NAME" \
    --icon "assets/shtucodeproxy.ico" \
    --add-data "assets:assets" \
    --add-data "proxy.py:." \
    --add-data "pyqt_gui.py:." \
    --add-data "config_store.py:." \
    --add-data "safe_io.py:." \
    app.py

  rm -rf "build/$PACKAGE_ROOT"
  mkdir -p "build/$PACKAGE_ROOT"
  cp -R "dist/$FOLDER_NAME" "build/$PACKAGE_ROOT/$FOLDER_NAME"
  cp linux_launcher.py "build/$PACKAGE_ROOT/run_shtucodeproxy.py"
  cat > "build/$PACKAGE_ROOT/README-LINUX.txt" <<EOF
SHTUCodeProxy v${VERSION} Linux package

Usage:
1. Extract this tar.gz package.
2. Run: python3 run_shtucodeproxy.py

The bundled SHTUCodeProxy runtime includes Python, PyQt5, and Qt libraries.
You do not need to install Python packages such as PyQt5 or PyInstaller.
EOF
  tar -czf "release/${APP_NAME}-python-launcher.tar.gz" -C build "$PACKAGE_ROOT"
  echo "Python-launcher folder package complete: release/${APP_NAME}-python-launcher.tar.gz"
fi

if [[ "$ONE_DIR_ONLY" != "1" ]]; then
  python3 -m PyInstaller \
    --noconfirm \
    --clean \
    --onefile \
    --name "$APP_NAME" \
    --icon "assets/shtucodeproxy.ico" \
    --add-data "assets:assets" \
    --add-data "proxy.py:." \
    --add-data "pyqt_gui.py:." \
    --add-data "config_store.py:." \
    --add-data "safe_io.py:." \
    app.py

  cp "dist/$APP_NAME" "release/$APP_NAME"
  echo "Single-file build complete: release/$APP_NAME"
fi

echo "Build complete. Release files are in: $ROOT/release"
