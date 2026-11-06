"""Requests per second of django-bolt with Django's async ORM and with djox.

    cd benchmarks/bolt && uv run python load_test.py
    uv run python load_test.py --duration 3 --rounds 1      # quicker

django-bolt is a Rust HTTP server for Django (Actix Web). Its async handlers
call the ORM like any async Django code, so the same `api.py` runs on two
stacks: stock Django (`await Book.objects.aget()` on a thread) and djox
(the same call on djox's Rust engine). Only the settings differ (`settings.py`).

For each stack it fills a fresh SQLite database (the web benchmark's
`setup`), starts `runbolt` with one process, and loads each endpoint with oha
(32 connections, after a warm-up). Stacks take turns, round after round, and
each number is the median of the rounds. Needs `oha` on the PATH
(`cargo install oha`). Writes `../results/bolt.json` and prints a Markdown table.
"""

import argparse
import json
import os
import platform
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

FOLDER = Path(__file__).parent
RESULTS = FOLDER.parent / "results" / "bolt.json"
PORT = 8766
BASE = f"http://127[.]0[.]0[.]1:{PORT}"  # oha reads the URL as a regex (random ids, below)
STACKS = ["django", "djox"]
CONNECTIONS = 32
ENDPOINTS = {
    "get one book": rf"{BASE}/books/[1-9][0-9]{{0,3}}",  # a random book, 1 to 9999
    "list a page, with authors": rf"{BASE}/books[?]page=[1-9][0-9]?",
}


def environment_without_proxy(stack: str) -> dict[str, str]:
    """The environment of a child process: this stack, and no HTTP proxy for a server on this machine."""
    variables = {name: value for name, value in os.environ.items() if not name.lower().endswith("_proxy")}
    return {**variables, "STACK": stack}


def load(url: str, seconds: float, stack: str) -> dict[str, float]:
    """Run oha for `seconds` and return requests per second, latencies and failures."""
    command = ["oha", "--no-tui", "--output-format", "json", "-z", f"{seconds}s", "-c", str(CONNECTIONS)]
    command += ["--rand-regex-url", url]
    output = subprocess.run(
        command, check=True, capture_output=True, text=True, env=environment_without_proxy(stack)
    ).stdout
    report = json.loads(output)
    failed = sum(count for code, count in report["statusCodeDistribution"].items() if not code.startswith("2"))
    for error, count in report.get("errorDistribution", {}).items():
        if error != "aborted due to deadline":  # requests still running when the time is up
            failed += count
    return {
        "requests_per_second": report["summary"]["requestsPerSec"],
        "p50_ms": report["latencyPercentiles"]["p50"] * 1000,
        "p99_ms": report["latencyPercentiles"]["p99"] * 1000,
        "failed": failed,
    }


def wait_for_port() -> None:
    for _ in range(200):
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=0.1).close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("the server didn't start")


def run(stack: str, seconds: float) -> dict[str, dict[str, float]]:
    """One stack: a fresh database, a server, every endpoint loaded."""
    environment = environment_without_proxy(stack)
    web_folder = FOLDER.parent / "web"
    sqlite_file = environment.setdefault("SQLITE_FILE", "/tmp/djox-bolt-benchmark.sqlite3")
    setup_environment = {**environment, "SQLITE_FILE": sqlite_file}
    subprocess.run([sys.executable, "app.py", "setup"], cwd=web_folder, env=setup_environment, check=True)
    server = subprocess.Popen(
        [sys.executable, "manage.py", "runbolt", "--port", str(PORT), "--processes", "1"],
        cwd=FOLDER,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    results = {}
    try:
        wait_for_port()
        for name, url in ENDPOINTS.items():
            load(url, 2, stack)  # warm up: connections, caches, the fast lane's SQL
            results[name] = load(url, seconds, stack)
            print(
                f"{stack:6} {name:26} {results[name]['requests_per_second']:8,.0f} req/s"
                f"  p50 {results[name]['p50_ms']:6.1f} ms  failed {results[name]['failed']:.0f}",
                flush=True,
            )
    finally:
        server.terminate()
        server.wait()
    return results


def median_of_rounds(rounds: list[dict[str, dict[str, float]]]) -> dict[str, dict[str, float]]:
    """Each endpoint's median over the rounds, measure by measure."""
    medians = {}
    for name in rounds[0]:
        medians[name] = {
            measure: statistics.median(one_round[name][measure] for one_round in rounds) for measure in rounds[0][name]
        }
    return medians


def markdown(results: dict[str, dict[str, dict[str, float]]]) -> str:
    lines = ["| | django-bolt + Django | django-bolt + djox |", "|---|---:|---:|"]
    for name in ENDPOINTS:
        django_rps = results["django"][name]["requests_per_second"]
        djox_rps = results["djox"][name]["requests_per_second"]
        lines.append(f"| {name} | {django_rps:,.0f} | {djox_rps:,.0f} ({djox_rps / django_rps:.1f}x) |")
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
                "results": results,
                "rounds": arguments.rounds,
                "seconds_per_endpoint": arguments.duration,
                "connections": CONNECTIONS,
                "machine": f"{platform.system()} {platform.machine()}, {os.cpu_count()} CPUs",
                "python": platform.python_version(),
            },
            indent=2,
        )
        + "\n"
    )
    print(markdown(results))


if __name__ == "__main__":
    main()
