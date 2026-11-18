#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p build/test-classes
javac_bin="${JAVA_HOME:+$JAVA_HOME/bin/}javac"
java_bin="${JAVA_HOME:+$JAVA_HOME/bin/}java"
java_home_option=()
if [[ -n "${JAVA_HOME:-}" ]]; then java_home_option=("-J-Djava.home=$JAVA_HOME"); fi
"$javac_bin" "${java_home_option[@]}" --release 8 -Xlint:all,-options,-try -d build/test-classes \
    app/src/main/java/org/usbportal/core/*.java tests/org/usbportal/core/*.java
if [[ "${1:-}" == "--serve" ]]; then
    exec "$java_bin" -ea -cp build/test-classes org.usbportal.core.ProtocolTest "$@"
fi
"$java_bin" -ea -cp build/test-classes org.usbportal.core.ProtocolTest "$@"
"$java_bin" -ea -cp build/test-classes org.usbportal.core.DeviceKindTest
