#!/usr/bin/env bash
# Makes the certificates the TLS tests use, in the folder given (default:
# `target/test-certificates`):
#
#   root.crt                 a certificate authority, made up for the tests
#   server.crt, server.key   the server's certificate, made out to `localhost`
#                            and signed by root.crt
#
# Point Postgres at server.crt and server.key (`ssl = on`), then run the
# tests with `DJOX_POSTGRES_ROOT_CERT=<folder>/root.crt`.
set -euo pipefail

folder="${1:-target/test-certificates}"
mkdir -p "$folder"
cd "$folder"

# The authority: a key, and a certificate it signs itself.
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=djox test authority" \
    -keyout root.key -out root.crt 2>/dev/null

# The server: a key, a request, and the authority's signature on it.
# `subjectAltName` is the name clients check (`verify-full`).
openssl req -newkey rsa:2048 -nodes -subj "/CN=localhost" -keyout server.key -out server.csr 2>/dev/null
printf "subjectAltName=DNS:localhost\nbasicConstraints=CA:FALSE\n" > server.ext
openssl x509 -req -in server.csr -CA root.crt -CAkey root.key -CAcreateserial -days 3650 \
    -extfile server.ext -out server.crt 2>/dev/null

# Postgres refuses a key file that others can read.
chmod 600 server.key
echo "Made $folder/root.crt, $folder/server.crt and $folder/server.key"
