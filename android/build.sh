#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Dependency-free SDK build, also usable without Gradle or Android Studio.
sdk="${ANDROID_SDK_ROOT:-${ANDROID_HOME:-}}"
if [[ -z "$sdk" ]]; then echo "Set ANDROID_SDK_ROOT to an Android SDK with platform 35 and build-tools 35.0.0." >&2; exit 1; fi
platform="$sdk/platforms/android-35/android.jar"
build_tools="${ANDROID_BUILD_TOOLS:-$sdk/build-tools/35.0.0}"
[[ -f "$platform" && -x "$build_tools/aapt2" ]] || { echo "Missing platform 35 or build-tools 35.0.0." >&2; exit 1; }
java_bin="${JAVA_HOME:+$JAVA_HOME/bin/}java"
javac_bin="${JAVA_HOME:+$JAVA_HOME/bin/}javac"
keytool_bin="${JAVA_HOME:+$JAVA_HOME/bin/}keytool"
command -v "$java_bin" "$javac_bin" zip >/dev/null
mkdir -p build/generated build/classes build/dex dist
rm -rf build/generated/* build/classes/* build/dex/*
"$build_tools/aapt2" compile --dir app/src/main/res -o build/resources.zip
"$build_tools/aapt2" link -I "$platform" --manifest app/src/main/AndroidManifest.xml \
    --java build/generated -o build/resources.apk build/resources.zip
mapfile -t sources < <(find app/src/main/java build/generated -name '*.java' -print | sort)
java_home_option=()
if [[ -n "${JAVA_HOME:-}" ]]; then java_home_option=("-J-Djava.home=$JAVA_HOME"); fi
"$javac_bin" "${java_home_option[@]}" -encoding UTF-8 -source 8 -target 8 -Xlint:-options \
    -bootclasspath "$platform:$build_tools/core-lambda-stubs.jar" -d build/classes "${sources[@]}"
"$java_bin" -m jdk.jartool/sun.tools.jar.Main --create --file build/classes.jar -C build/classes .
"$java_bin" -Xmx512m -cp "$build_tools/lib/d8.jar" com.android.tools.r8.D8 \
    --min-api 26 --lib "$platform" --output build/dex build/classes.jar
cp build/resources.apk build/unsigned.apk
zip -q -j build/unsigned.apk build/dex/classes*.dex
"$build_tools/zipalign" -f -p 4 build/unsigned.apk build/aligned.apk

# Preserve this local key for APK updates. It is never included in the source/APK.
keystore="${USBPORTAL_KEYSTORE:-$PWD/.signing/development.jks}"
export USBPORTAL_KEYSTORE_PASSWORD="${USBPORTAL_KEYSTORE_PASSWORD:-android}"
alias_name="${USBPORTAL_KEY_ALIAS:-usbportal}"
if [[ ! -f "$keystore" ]]; then
    [[ -z "${USBPORTAL_KEYSTORE:-}" ]] || { echo "Configured keystore does not exist." >&2; exit 1; }
    mkdir -p .signing; chmod 700 .signing
    "$keytool_bin" -genkeypair -keystore "$keystore" -alias "$alias_name" -keyalg RSA -keysize 3072 \
        -validity 10000 -storepass:env USBPORTAL_KEYSTORE_PASSWORD -keypass:env USBPORTAL_KEYSTORE_PASSWORD \
        -dname 'CN=USBPortal Development' >/dev/null
    chmod 600 "$keystore"
fi
apk="dist/usbportal-android-1.3.0-android.2.apk"
"$java_bin" -jar "$build_tools/lib/apksigner.jar" sign --ks "$keystore" --ks-key-alias "$alias_name" \
    --ks-pass env:USBPORTAL_KEYSTORE_PASSWORD --key-pass env:USBPORTAL_KEYSTORE_PASSWORD --out "$apk" build/aligned.apk
"$java_bin" -jar "$build_tools/lib/apksigner.jar" verify --verbose "$apk"
sha256sum "$apk" > "$apk.sha256"
echo "Built $PWD/$apk"
