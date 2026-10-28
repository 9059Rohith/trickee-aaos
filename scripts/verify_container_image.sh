#!/usr/bin/env bash
# Smoke an already-built image on an isolated loopback port without touching Compose.
set -euo pipefail

image="${1:?Pass the image tag to smoke}"
mode="${2:-demo}"
if [[ "$mode" != demo && "$mode" != non-demo ]]; then
  echo "Mode must be demo or non-demo" >&2
  exit 2
fi
name="trickee-image-qa-$$"
if [[ "$mode" == non-demo ]]; then
  export TRICKEE_OPERATOR_KEY="$(openssl rand -hex 32)"
  export TRICKEE_VEHICLE_KEY="$(openssl rand -hex 32)"
  container="$(docker run --rm -d --name "$name" -p 127.0.0.1::8000 \
    -e TRICKEE_OPERATOR_KEY -e TRICKEE_VEHICLE_KEY "$image")"
  operator_key="$TRICKEE_OPERATOR_KEY"
else
  container="$(docker run --rm -d --name "$name" -p 127.0.0.1::8000 \
    -e TRICKEE_DEMO_MODE=1 "$image")"
  operator_key="local-demo-operator-key"
fi
trap 'docker stop "$container" >/dev/null 2>&1 || true' EXIT
address="$(docker port "$container" 8000/tcp | head -n 1)"
case "$address" in
  127.0.0.1:*) ;;
  *) echo "Refusing non-loopback Docker port: $address" >&2; exit 1 ;;
esac
url="http://$address"
ready=0
for _ in {1..40}; do
  if curl --fail --silent --show-error --max-time 2 "$url/ready" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.25
done
if [[ "$ready" != 1 ]]; then
  docker logs "$container" >&2
  echo "Container readiness check failed" >&2
  exit 1
fi
health="$(curl --fail --silent --show-error "$url/health")"
expected_mode="CONFIGURED"
if [[ "$mode" == demo ]]; then expected_mode="DEMO"; fi
if [[ "$health" != *"\"mode\":\"$expected_mode\""* ]]; then
  echo "Health mode mismatch: expected $expected_mode" >&2
  exit 1
fi
authorized="$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --header "X-Operator-Key: $operator_key" "$url/api/v1/operator/me")"
denied="$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --header 'X-Operator-Key: invalid-qa-key' "$url/api/v1/operator/me")"
if [[ "$authorized" != 200 || "$denied" != 401 ]]; then
  echo "Auth smoke failed: authorized=$authorized denied=$denied" >&2
  exit 1
fi
if [[ "$mode" == non-demo ]]; then
  demo_denied="$(curl --silent --output /dev/null --write-out '%{http_code}' \
    --header 'X-Operator-Key: local-demo-operator-key' "$url/api/v1/operator/me")"
  cache="$(curl --silent --dump-header - --output /dev/null \
    --header "X-Operator-Key: $operator_key" "$url/api/v1/operator/me" \
    | tr -d '\r' | grep -i '^cache-control:' | head -n 1 || true)"
  if [[ "$demo_denied" != 401 || "${cache,,}" != 'cache-control: no-store' ]]; then
    echo "Non-demo policy failed: demo_denied=$demo_denied cache=$cache" >&2
    exit 1
  fi
  echo "Container image smoke passed: mode=non-demo health=200 ready=200 authorized=200 denied=401 demo_denied=401 cache=no-store image=$image"
else
  echo "Container image smoke passed: mode=demo health=200 ready=200 authorized=200 denied=401 image=$image"
fi
