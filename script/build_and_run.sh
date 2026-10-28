#!/usr/bin/env bash
# 开发入口：构建独立 .app，再由 macOS 正常启动；用户日常直接双击应用。
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-run}"
APP_BUNDLE="$ROOT_DIR/余料仓库.app"
BUNDLE_ID="cn.upinking.leftoverwarehouse"
BUILD_PYTHON="$ROOT_DIR/.venv-build/bin/python"
cd "$ROOT_DIR"

case "$MODE" in
  run|--build-only|--verify|--logs|--debug) ;;
  *) echo "用法：$0 [--build-only|--verify|--logs|--debug]" >&2; exit 2 ;;
esac

if [[ "$MODE" != "--build-only" ]]; then
  /usr/bin/osascript >/dev/null 2>&1 <<'APPLESCRIPT' || true
if application id "cn.upinking.leftoverwarehouse" is running then
  tell application id "cn.upinking.leftoverwarehouse" to quit
end if
APPLESCRIPT
fi

if [[ ! -x "$BUILD_PYTHON" ]]; then
  command -v uv >/dev/null || { echo "源码构建需要 uv；日常使用请直接双击已生成的 .app。" >&2; exit 1; }
  uv venv --python 3.12 "$ROOT_DIR/.venv-build"
fi
if ! "$BUILD_PYTHON" -c 'import PyInstaller' >/dev/null 2>&1; then
  uv pip install --python "$BUILD_PYTHON" -r "$ROOT_DIR/requirements-build.txt"
fi
mkdir -p "$ROOT_DIR/build" "$ROOT_DIR/dist"

# 后端和 Python 一起打包。缓存仅减少重复构建，运行时不依赖此虚拟环境。
BACKEND_HASH="$("$BUILD_PYTHON" - <<'PY'
from pathlib import Path
import hashlib, platform
h = hashlib.sha256(platform.python_version().encode())
for p in [Path('app.py'), Path('warehouse.py'), Path('desktop_server.py'), Path('ai_assistant.py'), Path('ai_credentials.py'), Path('bom_import.py'), Path('requirements-build.txt'), *sorted(Path('web').glob('*')), Path('data/seed.json')]:
    h.update(str(p).encode()); h.update(p.read_bytes())
print(h.hexdigest())
PY
)"
CACHED_HASH="$(cat "$ROOT_DIR/build/backend.hash" 2>/dev/null || true)"
if [[ "$BACKEND_HASH" != "$CACHED_HASH" || ! -x "$ROOT_DIR/build/backend-dist/warehouse-server/warehouse-server" ]]; then
  "$BUILD_PYTHON" -m PyInstaller --noconfirm --clean --onedir --noupx \
    --name warehouse-server --distpath "$ROOT_DIR/build/backend-dist" \
    --workpath "$ROOT_DIR/build/pyinstaller" --specpath "$ROOT_DIR/build/spec" \
    --add-data "$ROOT_DIR/web:web" --add-data "$ROOT_DIR/data/seed.json:data" \
    --exclude-module tkinter "$ROOT_DIR/desktop_server.py"
  printf '%s' "$BACKEND_HASH" > "$ROOT_DIR/build/backend.hash"
fi

STAGE="$(mktemp -d "$ROOT_DIR/build/app.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/Contents/MacOS" "$STAGE/Contents/Resources"
/usr/bin/ditto "$ROOT_DIR/build/backend-dist/warehouse-server" "$STAGE/Contents/Resources/backend"
xcrun swiftc -swift-version 5 -O -target arm64-apple-macosx13.0 \
  -framework Security "$ROOT_DIR/native/AIKeychain.swift" -o "$ROOT_DIR/build/keychain-helper"
cp "$ROOT_DIR/build/keychain-helper" "$STAGE/Contents/Resources/backend/keychain-helper"
cp "$ROOT_DIR/native/Info.plist" "$STAGE/Contents/Info.plist"
/usr/bin/plutil -replace WarehouseLegacyDatabasePath -string "$ROOT_DIR/app_data/warehouse.sqlite3" "$STAGE/Contents/Info.plist"
xcrun swiftc -swift-version 5 -O -target arm64-apple-macosx13.0 \
  -framework Cocoa -framework WebKit "$ROOT_DIR/native/main.swift" \
  -o "$STAGE/Contents/MacOS/Warehouse"

ICONSET="$ROOT_DIR/build/Warehouse.iconset"
mkdir -p "$ICONSET"
xcrun swift "$ROOT_DIR/script/make_icon.swift" "$ROOT_DIR/build/icon-1024.png"
for SIZE in 16 32 128 256 512; do
  /usr/bin/sips -z "$SIZE" "$SIZE" "$ROOT_DIR/build/icon-1024.png" --out "$ICONSET/icon_${SIZE}x${SIZE}.png" >/dev/null
  DOUBLE=$((SIZE * 2))
  /usr/bin/sips -z "$DOUBLE" "$DOUBLE" "$ROOT_DIR/build/icon-1024.png" --out "$ICONSET/icon_${SIZE}x${SIZE}@2x.png" >/dev/null
done
/usr/bin/iconutil -c icns "$ICONSET" -o "$STAGE/Contents/Resources/Warehouse.icns"
/usr/bin/codesign --force --deep --sign - "$STAGE"
/usr/bin/codesign --verify --deep --strict "$STAGE"
rm -rf "$APP_BUNDLE"
mv "$STAGE" "$APP_BUNDLE"
trap - EXIT
echo "已构建：$APP_BUNDLE"

case "$MODE" in
  --build-only) exit 0 ;;
  --debug) /usr/bin/lldb -- "$APP_BUNDLE/Contents/MacOS/Warehouse" ;;
  --logs)
    /usr/bin/open "$APP_BUNDLE"
    /usr/bin/log stream --info --style compact --predicate 'process == "Warehouse"'
    ;;
  *)
    /usr/bin/open "$APP_BUNDLE"
    if [[ "$MODE" == "--verify" ]]; then
      for ATTEMPT in {1..30}; do
        if pgrep -f "$APP_BUNDLE/Contents/MacOS/Warehouse" >/dev/null; then
          echo "应用进程已启动；窗口和数据另行验收。"
          exit 0
        fi
        sleep 0.2
      done
      echo "应用未能启动，请查看应用数据目录的 server.log。" >&2
      exit 1
    fi
    ;;
esac
