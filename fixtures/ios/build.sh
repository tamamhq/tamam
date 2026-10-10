#!/usr/bin/env bash
# Build both fixture variants for the iOS Simulator and print their .app paths.
# Uses swiftc directly (single-file app) — no Xcode project / XcodeGen, so it works
# across Xcode versions. Run: ./build.sh
set -euo pipefail
cd "$(dirname "$0")"

SDK=$(xcrun --sdk iphonesimulator --show-sdk-path)
TRIPLE=arm64-apple-ios16.0-simulator   # Apple-Silicon simulator; fine on newer runtimes

build_variant() {
  local name="$1"; shift
  local app="build/$name/Shoply.app"
  rm -rf "$app"; mkdir -p "$app"
  xcrun swiftc -sdk "$SDK" -target "$TRIPLE" -parse-as-library "$@" \
    -o "$app/Shoply" Sources/ShoplyApp.swift
  /usr/libexec/PlistBuddy \
    -c "Add :CFBundleExecutable string Shoply" \
    -c "Add :CFBundleIdentifier string dev.tamam.shoply" \
    -c "Add :CFBundleName string Shoply" \
    -c "Add :CFBundlePackageType string APPL" \
    -c "Add :CFBundleShortVersionString string 1.0" \
    -c "Add :CFBundleVersion string 1" \
    -c "Add :LSRequiresIPhoneOS bool true" \
    -c "Add :MinimumOSVersion string 16.0" \
    -c "Add :DTPlatformName string iphonesimulator" \
    -c "Add :CFBundleSupportedPlatforms array" \
    -c "Add :CFBundleSupportedPlatforms:0 string iPhoneSimulator" \
    -c "Add :UIDeviceFamily array" -c "Add :UIDeviceFamily:0 integer 1" \
    -c "Add :UILaunchScreen dict" \
    "$app/Info.plist" >/dev/null
  codesign -f -s - "$app" >/dev/null 2>&1 || true
  echo "$PWD/$app"
}

correct=$(build_variant Correct)
buggy=$(build_variant Buggy -D BUGGY)
echo ""
echo "Built. (Only the eval suite needs these exports; tamam run finds the builds itself.)"
echo "  export TAMAM_FIXTURE_CORRECT_APP=\"$correct\""
echo "  export TAMAM_FIXTURE_BUGGY_APP=\"$buggy\""
