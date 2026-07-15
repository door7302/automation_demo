#!/usr/bin/env bash
#
# Generate a self-signed TLS certificate for the Temporal Web UI reverse proxy.
# Good for local/dev use only — browsers will warn about the untrusted cert.
#
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

openssl req -x509 -nodes -newkey rsa:2048 \
    -keyout "$DIR/server.key" \
    -out    "$DIR/server.crt" \
    -days   365 \
    -subj   "/CN=localhost" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"

echo "Self-signed certificate generated:"
echo "  $DIR/server.crt"
echo "  $DIR/server.key"
