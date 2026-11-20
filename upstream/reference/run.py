"""HTTP and trial helpers from I7, with authentication supplied by the Elsa launcher."""

import json
import math
import socket
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from observer import await_writer, committed, lock_instance, session


class Api:
    def __init__(self, url, authorization):
        self.url = url.rstrip("/")
        self.authorization = authorization

    def call(self, path, body=None, content_type="application/json"):
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
        headers = {"Content-Type": content_type}
        if self.authorization:
            headers["Authorization"] = self.authorization
        req = urllib.request.Request(self.url + path, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as response:
            data = response.read()
            return json.loads(data) if data else None


def timed(api, operation, request, gate=None):
    if gate:
        gate.wait()
    started = time.monotonic()
    error = None
    timed_out = False
    try:
        result = api.call(*request)
        if operation == "import" and (not result or result.get("imported") != 1):
            raise ValueError(f"Expected one imported instance, got {result!r}")
    except urllib.error.HTTPError as exc:
        error = f"HTTP {exc.code}: {exc.read(400).decode(errors='replace')}"
        timed_out = exc.code in (408, 504)
    except (OSError, ValueError) as exc:
        error = str(exc)
        timed_out = isinstance(getattr(exc, "reason", exc), (TimeoutError, socket.timeout))
    ended = time.monotonic()
    return {"end": ended, "ms": round(1000 * (ended - started), 3), "error": error, "timeout": timed_out}


def trial(api, observer, holder, definition, correlations, operation, held, requests):
    hot, *peers = correlations
    gate = threading.Barrier(len(peers) + 1, timeout=20)
    witness = None
    before = None
    released = None
    with ThreadPoolExecutor(len(correlations)) as pool:
        workers = [pool.submit(timed, api, operation, req, gate) for req in requests[1:]]
        try:
            if held:
                holder_pid = lock_instance(holder, hot)
            anchor = pool.submit(timed, api, operation, requests[0])
            if held:
                witness = await_writer(observer, holder_pid, anchor)
                confirmed = time.monotonic()
            gate.wait()
            if held:
                # Sample durable state near the end of the hold, before sending COMMIT.
                time.sleep(max(0, confirmed + 0.98 - time.monotonic()))
                before = committed(observer, definition, correlations, finished=True)
                sampled = time.monotonic()
                time.sleep(max(0, confirmed + 1 - sampled))
                released = time.monotonic()
                holder("COMMIT")
        except BaseException:
            gate.abort()
            raise
        finally:
            if held and released is None:
                holder("ROLLBACK")
        anchor_result = anchor.result()
        results = [future.result() for future in workers]

    final = committed(observer, definition, correlations, finished=True)
    missing = sorted(correlations.keys() - final)
    all_results = [anchor_result, *results]
    latencies = sorted(r["ms"] for r in results)
    errors = [dict(id=i, message=r["error"]) for i, r in zip(correlations, all_results) if r["error"]]
    row = {
        "operation": operation, "case": "held" if held else "control", "unrelated": len(peers),
        "anchor_id": hot, "anchor_ms": anchor_result["ms"],
        "p50_ms": latencies[math.ceil(0.50 * len(peers)) - 1],
        "p95_ms": latencies[math.ceil(0.95 * len(peers)) - 1],
        "p99_ms": latencies[math.ceil(0.99 * len(peers)) - 1],
        "errors": len(errors), "timeouts": sum(r["timeout"] for r in all_results),
        "error_details": errors, "committed": len(final), "audit_failed_ids": missing,
        "release_before_count": None, "committed_before_release": None,
    }
    if held:
        row.update(
            holder_pid=holder_pid, writer=witness, hold_ms=round(1000 * (released - confirmed), 3),
            audit_at_ms=round(1000 * (sampled - confirmed), 3),
            release_before_count=sum(r["end"] < released and not r["error"] for r in results),
            committed_before_release=len(before.intersection(peers)),
            anchor_pending_before_release=hot not in before and anchor_result["end"] >= released,
        )
    return row
