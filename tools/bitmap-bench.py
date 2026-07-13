#!/usr/bin/env python3
"""Reproducible native-bitmap latency and memory smoke benchmark.

Copyright (c) 2006-Present, Redis Ltd.
All rights reserved.

Licensed under your choice of (a) the Redis Source Available License 2.0
(RSALv2); or (b) the Server Side Public License v1 (SSPLv1); or (c) the
GNU Affero General Public License v3 (AGPLv3).
"""

import argparse
import json
import math
import os
import socket
import statistics
import threading
import time
from pathlib import Path
from typing import Any, Callable


class RespError(RuntimeError):
    pass


class Resp:
    def __init__(self, host: str, port: int, timeout: float):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.file = self.sock.makefile("rb")

    def close(self) -> None:
        self.file.close()
        self.sock.close()

    def command(self, *parts: Any) -> Any:
        encoded = []
        for part in parts:
            encoded.append(part if isinstance(part, bytes) else str(part).encode())
        request = [f"*{len(encoded)}\r\n".encode()]
        for part in encoded:
            request.extend((f"${len(part)}\r\n".encode(), part, b"\r\n"))
        self.sock.sendall(b"".join(request))
        return self._read()

    def _line(self) -> bytes:
        line = self.file.readline()
        if not line.endswith(b"\r\n"):
            raise RespError("truncated RESP response")
        return line[:-2]

    def _read(self) -> Any:
        prefix = self.file.read(1)
        if prefix == b"+":
            return self._line().decode("utf-8", "replace")
        if prefix == b"-":
            raise RespError(self._line().decode("utf-8", "replace"))
        if prefix == b":":
            return int(self._line())
        if prefix == b"$":
            length = int(self._line())
            if length == -1:
                return None
            value = self.file.read(length)
            if self.file.read(2) != b"\r\n":
                raise RespError("invalid bulk terminator")
            return value
        if prefix == b"*":
            length = int(self._line())
            return None if length == -1 else [self._read() for _ in range(length)]
        raise RespError(f"unknown RESP prefix {prefix!r}")


def info_map(raw: bytes) -> dict[str, str]:
    result = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        if line and not line.startswith("#") and ":" in line:
            key, value = line.split(":", 1)
            result[key] = value
    return result


class RssSampler:
    def __init__(self, pid: int | None):
        self.pid = pid
        self.peak_kib = 0
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.pid is None or not Path(f"/proc/{self.pid}/status").exists():
            return
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> int | None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()
            return self.peak_kib * 1024
        return None

    def _run(self) -> None:
        status = Path(f"/proc/{self.pid}/status")
        while not self.stop_event.is_set():
            try:
                for line in status.read_text().splitlines():
                    if line.startswith("VmRSS:"):
                        self.peak_kib = max(self.peak_kib, int(line.split()[1]))
                        break
            except (FileNotFoundError, ProcessLookupError):
                return
            time.sleep(0.001)


def percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1,
                       max(0, math.ceil(len(ordered) * fraction) - 1))]


def benchmark(name: str, iterations: int, operation: Callable[[], Any],
              prepare: Callable[[], Any] | None, pid: int | None) -> dict[str, Any]:
    samples = []
    sampler = RssSampler(pid)
    sampler.start()
    for _ in range(iterations):
        if prepare is not None:
            prepare()
        start = time.perf_counter_ns()
        operation()
        samples.append((time.perf_counter_ns() - start) / 1_000_000)
    peak = sampler.stop()
    return {
        "workload": name,
        "iterations": iterations,
        "mean_ms": statistics.mean(samples),
        "median_ms": statistics.median(samples),
        "p95_ms": percentile(samples, 0.95),
        "max_ms": max(samples),
        "process_peak_rss_bytes": peak,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6379)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--bytes", type=int, default=1024 * 1024 + 1)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--prefix", default=f"bench:bitmap:{os.getpid()}")
    parser.add_argument("--include-max-offset", action="store_true",
                        help="also time SETBIT at the v1 maximum offset")
    parser.add_argument("--include-save", action="store_true",
                        help="also run blocking SAVE against the benchmark dataset")
    args = parser.parse_args()
    if args.bytes < 1 or args.iterations < 1:
        parser.error("--bytes and --iterations must be positive")

    client = Resp(args.host, args.port, args.timeout)
    prefix = args.prefix
    old_default: Any = b"no"
    keys = [f"{prefix}:{name}" for name in
            ("string", "native", "convert", "all-result", "mixed-result", "restore")]
    results = []
    try:
        server_info = info_map(client.command("INFO", "server"))
        pid_text = server_info.get("process_id")
        pid = int(pid_text) if pid_text and args.host in ("127.0.0.1", "localhost") else None
        old_default = client.command("CONFIG", "GET", "bitmap-default-native")[1]
        client.command("CONFIG", "SET", "bitmap-default-native", "no")
        client.command("DEL", *keys)

        dense = bytes((0x55,)) * args.bytes
        client.command("SET", keys[0], dense)
        client.command("SET", keys[2], dense)
        client.command("CONFIG", "SET", "bitmap-default-native", "yes")
        client.command("SETBIT", keys[1], args.bytes * 8 - 2, 1)
        client.command("CONFIG", "SET", "bitmap-default-native", "no")

        results.append(benchmark(
            "convert_string_to_native", args.iterations,
            lambda: client.command("BITMAP", "CONVERT", keys[2], "NATIVE"),
            lambda: client.command("BITMAP", "CONVERT", keys[2], "STRING"), pid))
        results.append(benchmark(
            "convert_native_to_string", args.iterations,
            lambda: client.command("BITMAP", "CONVERT", keys[2], "STRING"),
            lambda: client.command("BITMAP", "CONVERT", keys[2], "NATIVE"), pid))

        results.append(benchmark(
            "bitcount_dense_string", args.iterations,
            lambda: client.command("BITCOUNT", keys[0]), None, pid))
        client.command("BITMAP", "CONVERT", keys[2], "NATIVE")
        results.append(benchmark(
            "bitcount_dense_native", args.iterations,
            lambda: client.command("BITCOUNT", keys[2]), None, pid))
        results.append(benchmark(
            "getbit_sparse_native", args.iterations,
            lambda: client.command("GETBIT", keys[1], args.bytes * 8 - 2), None, pid))

        client.command("CONFIG", "SET", "bitmap-default-native", "yes")
        results.append(benchmark(
            "bitop_all_string_native_destination", args.iterations,
            lambda: client.command("BITOP", "OR", keys[3], *([keys[0]] * 8)), None, pid))
        client.command("CONFIG", "SET", "bitmap-default-native", "no")
        results.append(benchmark(
            "bitop_large_mixed_duplicate_strings", args.iterations,
            lambda: client.command("BITOP", "OR", keys[4], keys[1], *([keys[0]] * 6)), None, pid))

        payload = client.command("DUMP", keys[1])
        results.append(benchmark(
            "restore_native_payload", args.iterations,
            lambda: client.command("RESTORE", keys[5], 0, payload, "REPLACE"), None, pid))

        if args.include_max_offset:
            client.command("CONFIG", "SET", "bitmap-default-native", "yes")
            results.append(benchmark(
                "setbit_v1_max_offset", 1,
                lambda: client.command("SETBIT", f"{prefix}:max", 4294967295, 1), None, pid))
            client.command("CONFIG", "SET", "bitmap-default-native", "no")
            keys.append(f"{prefix}:max")
        if args.include_save:
            results.append(benchmark("blocking_save", 1,
                                     lambda: client.command("SAVE"), None, pid))

        memory = {key: client.command("MEMORY", "USAGE", key) for key in keys}
        print(json.dumps({
            "server_version": server_info.get("redis_version"),
            "logical_bytes": args.bytes,
            "results": results,
            "key_memory_bytes": memory,
            "notes": [
                "Round-trip time includes RESP transport; run on localhost and an idle server.",
                "Per-workload RSS is sampled from /proc only for a local server.",
                "Run separately with AOF/replicas enabled to measure propagation and rewrite behavior.",
            ],
        }, indent=2, sort_keys=True))
    finally:
        try:
            client.command("CONFIG", "SET", "bitmap-default-native",
                           old_default.decode() if isinstance(old_default, bytes) else old_default)
            client.command("DEL", *keys)
        finally:
            client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
