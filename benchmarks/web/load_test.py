"""Requests per second of the same Django app on stock Django and on djox.

    cd benchmarks && uv run python web/load_test.py                 # SQLite and Postgres
    uv run python web/load_test.py --databases sqlite --duration 5   # quicker

For each database, stack (`django`, `djox`) and API (`sync`, `async`, `drf`),
it fills a fresh database (`app.py setup`), starts the app on one granian
worker, and loads each endpoint with oha (32 connections, after a warm-up).
Stacks take turns, round after round, and each number is the median of the
rounds: this keeps a slow minute of the machine from favoring one stack.
Needs `oha` on the PATH (`cargo install oha`). Writes `results/web.json` and
prints a Markdown table.
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

import django
import psycopg

FOLDER = Path(__file__).parent
RESULTS = FOLDER.parent / "results" / "web.json"
PORT = 8765
BASE = f"http://127[.]0[.]0[.]1:{PORT}"  # oha reads the URL as a regex (random ids, below)
STACKS = ["django", "djox"]
APIS = ["sync", "async", "drf"]
CONNECTIONS = 32


def endpoints(api: str) -> list[tuple[str, str, str, str | None]]:
    """(name, method, URL regex, JSON body) for each endpoint of an API, writes last."""
    slash = "/" if api == "drf" else ""  # DRF's router adds a trailing slash
    author_field = "author" if api == "drf" else "author_id"
    book = rf"{BASE}/{api}/books/[1-9][0-9]{{0,3}}{slash}"  # a random book, 1 to 9999
    return [
        ("get one book", "GET", book, None),
        ("list a page, with authors", "GET", rf"{BASE}/{api}/books{slash}[?]page=[1-9][0-9]?", None),
        ("update a book", "PATCH", book, '{"pages": 123}'),
        (
            "create a book",
            "POST",
            rf"{BASE}/{api}/books{slash}",
            f'{{"title": "New book", "pages": 100, "price": "9.99", "{author_field}": 1}}',
        ),
    ]


def load(method: str, url: str, body: str | None, seconds: float) -> dict[str, float]:
    """Run oha for `seconds` and return requests per second, latencies and failures."""
    command = [
        "oha",
        "--no-tui",
        "--output-format",
        "json",
        "-z",
        f"{seconds}s",
        "-c",
        str(CONNECTIONS),
        "-m",
        method,
        "--rand-regex-url",
        url,
    ]
    if body is not None:
        command += ["-T", "application/json", "-d", body]
    # No HTTP proxy for a server on this machine.
    environment = {name: value for name, value in os.environ.items() if not name.lower().endswith("_proxy")}
    report = json.loads(subprocess.run(command, check=True, capture_output=True, text=True, env=environment).stdout)
    codes = report["statusCodeDistribution"]
    failed = sum(count for code, count in codes.items() if not code.startswith("2"))
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
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=0.1).close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("the server didn't start")


def create_postgres_database() -> None:
    url = os.environ.setdefault("DJOX_POSTGRES_URL", "postgresql://postgres:postgres@localhost/postgres")
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute("DROP DATABASE IF EXISTS djox_web_benchmark")
        connection.execute("CREATE DATABASE djox_web_benchmark")


def run(database: str, stack: str, api: str, seconds: float) -> dict[str, dict[str, float]]:
    server_kind = "asgi" if api == "async" else "wsgi"
    environment = {**os.environ, "STACK": stack, "DATABASE": database, "SERVER": server_kind}
    subprocess.run([sys.executable, "app.py", "setup"], cwd=FOLDER, env=environment, check=True)
    interface, application = ("asginl", "app:asgi_app") if server_kind == "asgi" else ("wsgi", "app:wsgi_app")
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "granian",
            "--interface",
            interface,
            "--workers",
            "1",
            "--blocking-threads",
            "1",
            "--port",
            str(PORT),
            "--log-level",
            "warning",
            "--no-ws",
            application,
        ],
        cwd=FOLDER,
        env=environment,
    )
    results = {}
    try:
        wait_for_port()
        for name, method, url, body in endpoints(api):
            load(method, url, body, seconds=2)  # warm up: connections, caches, the fast lane's SQL
            results[name] = load(method, url, body, seconds)
            print(
                f"{database:8} {stack:6} {api:5} {name:26} {results[name]['requests_per_second']:8,.0f} req/s"
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


def markdown(all_results: dict) -> str:
    lines = []
    for database, by_stack in all_results.items():
        lines += [
            f"\n{database}: requests per second, higher is better\n",
            "| | " + " | ".join(f"{api} Django | {api} djox" for api in APIS) + " |",
            "|---|" + "---:|" * (2 * len(APIS)),
        ]
        for name, *_ in endpoints("sync"):
            cells = []
            for api in APIS:
                django_rps = by_stack["django"][api][name]["requests_per_second"]
                djox_rps = by_stack["djox"][api][name]["requests_per_second"]
                cells += [f"{django_rps:,.0f}", f"{djox_rps:,.0f} ({djox_rps / django_rps:.1f}x)"]
            lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--databases", nargs="+", default=["sqlite", "postgres"])
    parser.add_argument("--duration", type=float, default=5, help="seconds per endpoint")
    parser.add_argument("--rounds", type=int, default=3)
    arguments = parser.parse_args()
    all_results: dict = {}
    for database in arguments.databases:
        if database == "postgres":
            create_postgres_database()
        rounds: dict = {stack: {api: [] for api in APIS} for stack in STACKS}
        for _ in range(arguments.rounds):
            for api in APIS:
                for stack in STACKS:
                    rounds[stack][api].append(run(database, stack, api, arguments.duration))
        all_results[database] = {stack: {api: median_of_rounds(rounds[stack][api]) for api in APIS} for stack in STACKS}
    RESULTS.write_text(
        json.dumps(
            {
                "machine": f"{platform.system()} {platform.machine()}, {os.cpu_count()} CPUs",
                "python": platform.python_version(),
                "django": django.get_version(),
                "connections": CONNECTIONS,
                "seconds_per_endpoint": arguments.duration,
                "rounds": arguments.rounds,
                "results": all_results,
            },
            indent=2,
        )
        + "\n"
    )
    print(markdown(all_results))


if __name__ == "__main__":
    main()
