#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
exec java -jar target/psp-reference-java-2.0.0.jar
