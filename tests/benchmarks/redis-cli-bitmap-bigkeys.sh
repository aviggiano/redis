#!/usr/bin/env bash

set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "usage: $0 /path/to/redis-server /path/to/redis-cli" >&2
    exit 2
fi

server_bin=$(realpath "$1")
cli_bin=$(realpath "$2")
keys=${BITMAP_BENCH_KEYS:-20000}
port=${BITMAP_BENCH_PORT:-16341}
bench_dir=$(mktemp -d)
pidfile="$bench_dir/redis.pid"
logfile="$bench_dir/redis.log"

cleanup() {
    "$cli_bin" -p "$port" shutdown nosave >/dev/null 2>&1 || true
    if [[ -f "$pidfile" ]]; then
        kill "$(<"$pidfile")" >/dev/null 2>&1 || true
    fi
    rm -rf "$bench_dir"
}
trap cleanup EXIT

"$server_bin" \
    --port "$port" \
    --bind 127.0.0.1 \
    --save '' \
    --appendonly no \
    --daemonize yes \
    --dir "$bench_dir" \
    --pidfile "$pidfile" \
    --logfile "$logfile" \
    --bitmap-default-roaring yes

ready=0
for _ in $(seq 1 100); do
    if "$cli_bin" -p "$port" ping >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 0.05
done
if (( ready == 0 )); then
    cat "$logfile" >&2
    exit 1
fi

# Populate one-bit native bitmaps, then make the final key eight bits wide.
# Fixture creation is deliberately outside the measured region.
{
    seq 1 "$keys" | awk '{printf "SETBIT bitmap:%d 0 1\r\n", $1}'
    for bit in $(seq 1 7); do
        printf 'SETBIT bitmap:%s %s 1\r\n' "$keys" "$bit"
    done
} |
    "$cli_bin" -p "$port" --pipe >/dev/null

start_ns=$(date +%s%N)
output=$("$cli_bin" -p "$port" --bigkeys)
end_ns=$(date +%s%N)
elapsed_ms=$(( (end_ns - start_ns) / 1000000 ))

summary=$(awk '$2 == "bitmaps" && $3 == "with" {
    unit = $5;
    if ($5 == "set") unit = $5 "_" $6;
    print $1, $4, unit;
}' <<<"$output")
read -r bitmap_count bitmap_total unit <<<"$summary"

if [[ ! "$bitmap_count" =~ ^[0-9]+$ || ! "$bitmap_total" =~ ^[0-9]+$ || -z "$unit" ]]; then
    echo "unable to parse bitmap summary from redis-cli --bigkeys" >&2
    printf '%s\n' "$output" >&2
    exit 1
fi

printf 'elapsed_ms=%s bitmap_count=%s bitmap_total=%s unit=%s\n' \
    "$elapsed_ms" "$bitmap_count" "$bitmap_total" "$unit"
