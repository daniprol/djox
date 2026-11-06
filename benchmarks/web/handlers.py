"""Microseconds per request through Django's ASGI handler and through djox's (`djox.asgi`).

    cd benchmarks && uv run python web/handlers.py

The web app's views (`app.py`), called in this process like an ASGI server
would call them, with no HTTP in between: what is left is the handler's own
cost, plus the view's. Both handlers serve the same djox stack (djox's
`ENGINE`, every model on djox), so only the handler changes, with the
connection settings each one advises: `CONN_MAX_AGE = 0` with Django's
(Django's docs), kept connections with djox's (`docs/asgi.md`).

- one at a time: one request after another;
- 32 at once: batches of 32 requests started together, the way a busy server
  runs them (the time is per request).

Writes `results/handlers.json` and prints a Markdown table.
"""

import asyncio
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

FOLDER = Path(__file__).parent
RESULTS = FOLDER.parent / "results" / "handlers.json"
ENDPOINTS = {
    "a sync view: get one book": "/sync/books/7",
    "an async view: get one book": "/async/books/7",
    "a sync view: list a page, with authors": "/sync/books?page=3",
    "an async view: list a page, with authors": "/async/books?page=3",
}
HANDLERS = ["Django's handler", "djox's handler"]
AT_ONCE = 32
ROUNDS = 7


def scope(url: str) -> dict[str, Any]:
    """An ASGI scope for `GET url`, like an ASGI server makes."""
    url_path, _, query = url.partition("?")
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": url_path,
        "raw_path": url_path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(b"host", b"127.0.0.1")],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 80),
    }


async def request(application: Any, url: str) -> None:
    """One `GET url` through `application`, checking it answered 200."""
    body_read = False
    disconnected = asyncio.Event()  # never set: the client stays until the end
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal body_read
        if not body_read:
            body_read = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await application(scope(url), receive, send)
    assert messages[0]["status"] == 200, messages


async def microseconds_per_request(application: Any, url: str, at_once: int) -> float:
    """The median, over `ROUNDS`, of the time per request."""
    batches = max(1, 2000 // at_once)
    for _ in range(100 // at_once + 1):  # warm up: connections, the fast lane's SQL
        await asyncio.gather(*(request(application, url) for _ in range(at_once)))
    times = []
    for _ in range(ROUNDS):
        start = time.perf_counter()
        for _ in range(batches):
            await asyncio.gather(*(request(application, url) for _ in range(at_once)))
        times.append((time.perf_counter() - start) / (batches * at_once) * 1e6)
    return statistics.median(times)


async def measure_this_handler(handler: str) -> dict[str, dict[str, float]]:
    """Every endpoint, one at a time and `AT_ONCE` at once, through `handler`."""
    import app  # imported here: it reads STACK, SERVER and HANDLER from the environment

    application = app.asgi_app
    assert app.STACK == "djox"
    results = {}
    for name, url in ENDPOINTS.items():
        results[name] = {
            "one at a time": await microseconds_per_request(application, url, at_once=1),
            f"{AT_ONCE} at once": await microseconds_per_request(application, url, at_once=AT_ONCE),
        }
    return results


def measure(handler: str) -> dict[str, dict[str, float]]:
    """Run `measure_this_handler()` in a new process, so the handlers don't share threads or connections."""
    handler_setting = "django" if handler == HANDLERS[0] else "djox"
    environment = {**os.environ, "STACK": "djox", "SERVER": "asgi", "DATABASE": "sqlite", "HANDLER": handler_setting}
    output = subprocess.run(
        [sys.executable, __file__, "--measure", handler], cwd=FOLDER, env=environment, check=True, capture_output=True
    ).stdout
    return json.loads(output)


def markdown(results: dict[str, dict[str, dict[str, float]]]) -> str:
    measures = ["one at a time", f"{AT_ONCE} at once"]
    lines = [
        "µs per request, lower is better\n",
        "| | " + " | ".join(f"{measure}: Django's handler | {measure}: djox's handler" for measure in measures) + " |",
        "|---|" + "---:|" * (2 * len(measures)),
    ]
    for name in ENDPOINTS:
        cells = []
        for measure in measures:
            django_time = results[HANDLERS[0]][name][measure]
            djox_time = results[HANDLERS[1]][name][measure]
            cells += [f"{django_time:,.0f}", f"{djox_time:,.0f} ({django_time / djox_time:.1f}x)"]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main() -> None:
    environment = {**os.environ, "STACK": "djox", "SERVER": "asgi", "DATABASE": "sqlite"}
    subprocess.run([sys.executable, "app.py", "setup"], cwd=FOLDER, env=environment, check=True)
    results = {handler: measure(handler) for handler in HANDLERS}
    import django

    RESULTS.write_text(
        json.dumps(
            {
                "machine": f"{platform.system()} {platform.machine()}, {os.cpu_count()} CPUs",
                "python": platform.python_version(),
                "django": django.get_version(),
                "results": results,
            },
            indent=2,
        )
        + "\n"
    )
    print(markdown(results))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--measure"]:
        sys.path.insert(0, str(FOLDER))
        print(json.dumps(asyncio.run(measure_this_handler(sys.argv[2]))))
    else:
        main()
