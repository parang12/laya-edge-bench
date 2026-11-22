"""Replay the fixed AI4I test set against the Docker MQTT gateway."""

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sys
from threading import Event
from time import perf_counter

from ai4i_evaluation import TEST_DATA, score
from mqtt_bench import DEFAULT_DOCKER_PORT, latency_summary, run_mqtt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=1000, help="sequential requests (default: 1000)")
    parser.add_argument("--port", type=int, default=os.environ.get("LAYA_MQTT_PORT", DEFAULT_DOCKER_PORT),
                        help="localhost MQTT port (default: LAYA_MQTT_PORT or 18883)")
    parser.add_argument("--timeout", type=float, default=10, help="request timeout in seconds (default: 10)")
    parser.add_argument("--output", type=Path, help="path for the JSON summary and request log")
    args = parser.parse_args(argv)
    if args.requests < 1:
        parser.error("--requests must be positive")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a finite positive number")

    started_at = datetime.now().astimezone()
    output = args.output or (
        TEST_DATA.parent.parent / "results" / f"docker-mqtt-{started_at:%Y%m%d_%H%M%S_%f}.json"
    )
    if output.exists():
        parser.error(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    cases = json.loads(TEST_DATA.read_text(encoding="utf-8"))["cases"]
    case_bytes = json.dumps(cases, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    rows = []
    gateway = {"memory_peak_mib": None}
    stop = Event()
    consecutive_timeouts = 0

    def record(row):
        nonlocal consecutive_timeouts
        rows.append(row)
        consecutive_timeouts = consecutive_timeouts + 1 if row["status"] == "timeout" else 0
        if len(rows) % 100 == 0 or len(rows) == args.requests:
            print(f"{len(rows)}/{args.requests}", flush=True)
        if consecutive_timeouts >= 5:
            stop.set()

    failure = None
    started = perf_counter()
    try:
        run_mqtt(
            cases, on_record=record, stop_event=stop, timeout=args.timeout,
            broker_address=("127.0.0.1", args.port), on_gateway=gateway.update,
            request_count=args.requests,
        )
    except KeyboardInterrupt:
        failure = "KeyboardInterrupt"
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    elapsed = round(perf_counter() - started, 3)
    counts = Counter(row["status"] for row in rows)
    statuses = {status: counts[status] for status in ("ok", "timeout", "error")}
    unique = [row for row in rows if not row["repeated"]]
    completed = len(rows) == args.requests and failure is None
    report = {
        "configuration": {
            "request_count": args.requests,
            "unique_cases": len(cases),
            "test_cases_sha256": hashlib.sha256(case_bytes).hexdigest(),
            "parallel_requests": 1,
            "timeout_seconds": args.timeout,
            "mqtt_port": args.port,
            "host_os": platform.system(),
            "host_architecture": platform.machine(),
            "python_version": platform.python_version(),
        },
        "measured_at": started_at.isoformat(timespec="seconds"),
        "elapsed_seconds": elapsed,
        "attempted": len(rows),
        "statuses": statuses,
        "completed": completed,
        "stopped_after_five_timeouts": stop.is_set(),
        "failure": failure,
        "classification": score(unique) if len(unique) == len(cases) else None,
        "inference_ms": dict(zip(("median", "p95"), latency_summary(rows, "inference_ms"))),
        "mqtt_round_trip_ms": dict(zip(("median", "p95"), latency_summary(rows, "total_ms"))),
        "gateway": gateway,
    }
    with output.open("x", encoding="utf-8") as handle:
        json.dump({**report, "requests": rows}, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"Saved: {output.resolve()}")
    print(f"Responses: {statuses['ok']}/{args.requests} (timeouts: {statuses['timeout']}, errors: {statuses['error']})")
    if failure:
        print(failure, file=sys.stderr)
    if stop.is_set():
        print("Stopped after five consecutive timeouts.", file=sys.stderr)
    return 0 if completed and statuses["ok"] == args.requests else 1


if __name__ == "__main__":
    raise SystemExit(main())
