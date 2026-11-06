"""Requests per second of the same shop endpoints in django-bolt, on stock Django and on djox.

    cd benchmarks && uv run python web/bolt_load_test.py
    uv run python web/bolt_load_test.py --duration 5 --rounds 3

The same models and database as `load_test.py`, served by django-bolt's own
server (`manage.py runbolt`, one process) instead of granian. Three endpoints,
each an `async` handler (`bolt_app/api.py`): get one book, list a page of books
with their authors, and create a book. Stacks take turns, round after round, and
each number is the median of the rounds. Needs `oha` on the PATH.

Writes `results/bolt.json` and prints a Markdown table.
"""

import argparse
import json
import os
import platform
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

import django
from load_test import BASE, CONNECTIONS, PORT, load, wait_for_port

FOLDER = Path(__file__).parent
RESULTS = FOLDER.parent / "results" / "bolt.json"
STACKS = ["django", "djox"]
ENDPOINTS = [
    # (name, method, URL regex, JSON body): oha reads the URL as a regex, so the ids are random
    ("get one book", "GET", rf"{BASE}/books/[1-9][0-9]{{0,3}}", None),
    ("list a page, with authors", "GET", rf"{BASE}/books[?]page=[1-9][0-9]?", None),
    (
        "create a book",
        "POST",
        f"{BASE}/books",
        '{"title": "New book", "pages": 100, "price": "9.99", "author_id": 1}',
    ),
]


def run(stack: str, seconds: float) -> dict[str, dict[str, float]]:
    """Start django-bolt for one stack on a fresh database, and load each endpoint."""
    environment = {**os.environ, "STACK": stack}
    subprocess.run([sys.executable, "app.py", "setup"], cwd=FOLDER, env=environment, check=True)
    server = subprocess.Popen(
        [sys.executable, "bolt_app/manage.py", "runbolt", "--no-admin", "--processes", "1", "--port", str(PORT)],
        cwd=FOLDER,
        env=environment,
        start_new_session=True,  # runbolt may start child processes: stop the whole group
        stdout=subprocess.DEVNULL,  # runbolt logs every request: keep that out of the output
        stderr=subprocess.DEVNULL,
    )
    results = {}
    try:
        wait_for_port()
        for name, method, url, body in ENDPOINTS:
            load(method, url, body, seconds=2)  # warm up: connections and the first queries
            results[name] = load(method, url, body, seconds)
            print(
                f"{stack:6} {name:26} {results[name]['requests_per_second']:8,.0f} req/s"
                f"  p50 {results[name]['p50_ms']:6.1f} ms  failed {results[name]['failed']:.0f}",
                flush=True,
            )
    finally:
        os.killpg(server.pid, signal.SIGTERM)
        server.wait()
        time.sleep(0.5)  # let the port go
    return results


def median_of_rounds(rounds: list[dict[str, dict[str, float]]]) -> dict[str, dict[str, float]]:
    """Each endpoint's median over the rounds, measure by measure."""
    return {
        name: {
            measure: statistics.median(one_round[name][measure] for one_round in rounds) for measure in rounds[0][name]
        }
        for name in rounds[0]
    }


def markdown(results: dict[str, dict[str, dict[str, float]]]) -> str:
    lines = [
        "| | Django, req/s | djox, req/s | djox against Django |",
        "|---|---:|---:|---:|",
    ]
    for name, *_ in ENDPOINTS:
        django_rps = results["django"][name]["requests_per_second"]
        djox_rps = results["djox"][name]["requests_per_second"]
        lines.append(f"| {name} | {django_rps:,.0f} | {djox_rps:,.0f} | {djox_rps / django_rps:.1f}x |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=5, help="seconds per endpoint")
    parser.add_argument("--rounds", type=int, default=3)
    arguments = parser.parse_args()
    rounds: dict[str, list] = {stack: [] for stack in STACKS}
    for _ in range(arguments.rounds):
        for stack in STACKS:
            rounds[stack].append(run(stack, arguments.duration))
    results = {stack: median_of_rounds(rounds[stack]) for stack in STACKS}
    RESULTS.write_text(
        json.dumps(
            {
                "machine": f"{platform.system()} {platform.machine()}, {os.cpu_count()} CPUs",
                "python": platform.python_version(),
                "django": django.get_version(),
                "django_bolt": subprocess.run(
                    [sys.executable, "-c", "import importlib.metadata as m; print(m.version('django-bolt'))"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip(),
                "processes": 1,
                "connections": CONNECTIONS,
                "rounds": arguments.rounds,
                "seconds_per_endpoint": arguments.duration,
                "results": results,
            },
            indent=2,
        )
        + "\n"
    )
    print(markdown(results))


if __name__ == "__main__":
    main()
