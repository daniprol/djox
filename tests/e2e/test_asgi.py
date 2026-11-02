"""`djox.asgi`: Django's ASGI handler, with the request signals on the event loop when that is safe."""

import asyncio
import io
import threading
from collections.abc import Callable, Iterator, Mapping
from typing import Any

import pytest
from django.core.signals import request_finished, request_started
from django.http import FileResponse, HttpRequest, HttpResponse, JsonResponse
from django.test import override_settings
from django.urls import path

from djox import _request_signals
from djox.asgi import get_asgi_application
from tests.e2e.library.models import Author

pytestmark = pytest.mark.django_db(transaction=True)


# ---------------------------------------------------------------- the views


async def author_view(request: HttpRequest, author_id: int) -> JsonResponse:
    author = await Author.objects.aget(pk=author_id)
    return JsonResponse({"name": author.name})


def sync_author_view(request: HttpRequest, author_id: int) -> JsonResponse:
    return JsonResponse({"name": Author.objects.get(pk=author_id).name})


async def failing_view(request: HttpRequest) -> HttpResponse:
    raise ValueError("the view failed")


async def file_view(request: HttpRequest) -> FileResponse:
    return FileResponse(io.BytesIO(b"one two"))


view_started = asyncio.Event()


async def slow_view(request: HttpRequest) -> HttpResponse:
    view_started.set()
    await asyncio.sleep(60)
    return HttpResponse("too late")


urlpatterns = [
    path("authors/<int:author_id>", author_view),
    path("sync-authors/<int:author_id>", sync_author_view),
    path("failing", failing_view),
    path("file", file_view),
    path("slow", slow_view),
]


# ---------------------------------------------------------------- helpers


def scope(url: str) -> dict[str, Any]:
    """An ASGI scope for `GET url`, like an ASGI server makes."""
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": url,
        "raw_path": url.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }


class Client:
    """Talks to djox's ASGI application like an ASGI server would, for one request."""

    def __init__(self) -> None:
        self.messages_sent: list[dict[str, Any]] = []
        self.body_read = False
        self.leaves = asyncio.Event()  # set it to disconnect

    async def receive(self) -> dict[str, Any]:
        """What the application reads: the request's body, then (if ever) a disconnect."""
        if not self.body_read:
            self.body_read = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self.leaves.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: Mapping[str, Any]) -> None:
        """What the application writes: the response's start, then its body."""
        self.messages_sent.append(dict(message))

    async def request(self, url: str) -> None:
        """`GET url`, until the application is done with it."""
        await get_asgi_application()(scope(url), self.receive, self.send)

    async def get(self, url: str) -> tuple[int, bytes]:
        """`GET url`: the status and the whole body."""
        await self.request(url)
        status = self.messages_sent[0]["status"]
        body = b"".join(message.get("body", b"") for message in self.messages_sent[1:])
        return status, body


async def get(url: str) -> tuple[int, bytes]:
    """`GET url` on djox's ASGI application: the status and the whole body."""
    return await Client().get(url)


class ThreadRecorder:
    """A receiver of the request signals that notes which thread it ran on.

    It is added to the receivers djox knows can't block, so it doesn't make
    the signals hop to a thread by itself.
    """

    def __init__(self) -> None:
        self.threads: list[str] = []

    def __call__(self, signal: Any, **kwargs: Any) -> None:
        name = "started" if signal is request_started else "finished"
        on_the_loop = threading.current_thread() is self.loop_thread
        self.threads.append(f"{name} on {'the event loop' if on_the_loop else 'a worker thread'}")

    loop_thread: threading.Thread | None = None


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Iterator[ThreadRecorder]:
    recorder = ThreadRecorder()
    receiver: Callable[..., Any] = recorder
    monkeypatch.setitem(_request_signals.RECEIVERS_THAT_CANNOT_BLOCK_NOW, receiver, _request_signals.always)
    request_started.connect(recorder, weak=False)
    request_finished.connect(recorder, weak=False)
    recorder.loop_thread = threading.current_thread()
    yield recorder
    request_started.disconnect(recorder)
    request_finished.disconnect(recorder)


@pytest.fixture(autouse=True)
def urls() -> Iterator[None]:
    """This module's `urlpatterns`, for every test here."""
    with override_settings(ROOT_URLCONF=__name__):
        yield


@pytest.fixture
def author() -> Author:
    return Author.objects.create(name="Ursula K. Le Guin")


# ---------------------------------------------------------------- the tests


async def test_an_async_view_answers_through_djox_handler(author: Author) -> None:
    status, body = await get(f"/authors/{author.pk}")
    assert (status, body) == (200, b'{"name": "Ursula K. Le Guin"}')


async def test_an_async_view_on_djox_sends_the_request_signals_without_a_thread(
    author: Author, recorder: ThreadRecorder
) -> None:
    await get(f"/authors/{author.pk}")
    assert recorder.threads == ["started on the event loop", "finished on the event loop"]


async def test_a_sync_view_closes_its_connections_on_a_thread_like_django(
    author: Author, recorder: ThreadRecorder
) -> None:
    status, body = await get(f"/sync-authors/{author.pk}")
    assert (status, body) == (200, b'{"name": "Ursula K. Le Guin"}')
    # The view opened a sync connection: `close_old_connections()` may close it, which blocks.
    assert recorder.threads == ["started on the event loop", "finished on a worker thread"]


async def test_a_receiver_of_the_programs_own_runs_on_a_thread_like_django(
    author: Author, recorder: ThreadRecorder
) -> None:
    def own_receiver(**kwargs: Any) -> None:
        own_receiver_threads.append(threading.current_thread())

    own_receiver_threads: list[threading.Thread] = []
    request_started.connect(own_receiver)
    try:
        await get(f"/authors/{author.pk}")
    finally:
        request_started.disconnect(own_receiver)
    assert own_receiver_threads[0] is not threading.current_thread()
    # The request's thread now exists, so `request_finished` goes there too, like in Django.
    assert recorder.threads == ["started on a worker thread", "finished on a worker thread"]


# Django warns that it reads a file response's sync iterator on a thread: expected here.
@pytest.mark.filterwarnings("ignore:StreamingHttpResponse must consume synchronous iterators")
async def test_a_response_holding_a_file_is_closed_on_a_thread(recorder: ThreadRecorder) -> None:
    status, body = await get("/file")
    assert (status, body) == (200, b"one two")
    assert recorder.threads == ["started on the event loop", "finished on a worker thread"]


@override_settings(DEBUG=False)
async def test_a_failing_view_gets_djangos_error_response() -> None:
    status, _body = await get("/failing")
    assert status == 500


async def test_a_client_that_leaves_cancels_the_view_and_finishes_the_request(recorder: ThreadRecorder) -> None:
    view_started.clear()
    client = Client()
    request = asyncio.create_task(client.request("/slow"))
    await asyncio.wait_for(view_started.wait(), timeout=5)
    client.leaves.set()
    await asyncio.wait_for(request, timeout=5)
    assert client.messages_sent == [], "no response was sent"
    assert recorder.threads == ["started on the event loop", "finished on the event loop"]
