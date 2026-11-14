#!/bin/bash
# يبني حزمة التوزيع لماك: يوقّع الحزمة توقيعاً ذاتياً (بلا حساب Apple) ثم يصنع DMG و zip.
#
# الاستعمال:  bash installer/make_dmg.sh [arm64|x64]
#
# التوقيع الذاتي (ad-hoc) مجاني: يمنع رسالة "التطبيق تالف" عند الفتح، ويسمح لـ Keychain
# بالتعامل مع الحزمة. لا يُغني عن notarization الخاص بـ Apple Developer (99$ سنوياً).
set -euo pipefail

ARCH="${1:-arm64}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

APP="dist/pdf2md.app"
DMG="dist/pdf2md-macos-${ARCH}.dmg"
ZIP="dist/pdf2md-macos-${ARCH}.zip"

if [ ! -d "$APP" ]; then
  echo "الحزمة غير موجودة: $APP — شغّل: python -m PyInstaller pdf2md.spec --noconfirm" >&2
  exit 1
fi

echo "→ توقيع ذاتي (ad-hoc)"
codesign --force --deep --sign - "$APP"

echo "→ فحص التوقيع"
codesign --verify --deep --strict "$APP" && echo "التوقيع سليم."

echo "→ حزمة DMG"
STAGING="$(mktemp -d)"
cp -R "$APP" "$STAGING/"
ln -s /Applications "$STAGING/Applications"
rm -f "$DMG"
hdiutil create -volname "pdf2md" -srcfolder "$STAGING" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGING"

echo "→ حزمة zip (للنقل دون DMG)"
rm -f "$ZIP"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"

ls -lh "$DMG" "$ZIP"
