import httpx
import pytest
from scripts import acceptance


@pytest.mark.parametrize("events", ["started", "started,citation", "started,citation,error,done"])
def test_incomplete_sse_does_not_pass(events: str) -> None:
    body = "".join(f"event: {event}\ndata: {{}}\n\n" for event in events.split(","))
    with httpx.Client(
        base_url="https://acceptance.example.test",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body)),
    ) as client, pytest.raises(RuntimeError):
        acceptance.first_citation(client)


def test_first_citation_ignores_started(monkeypatch: pytest.MonkeyPatch) -> None:
    timestamps = iter((10.0, 13.5))
    monkeypatch.setattr(acceptance, "perf_counter", lambda: next(timestamps))
    body = "event: started\ndata: {}\n\nevent: citation\ndata: {}\n\nevent: done\ndata: {}\n\n"
    with httpx.Client(
        base_url="https://acceptance.example.test",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body)),
    ) as client:
        assert acceptance.first_citation(client) == 3.5


@pytest.mark.parametrize(
    ("status", "built"), [("partial", 49), ("succeeded", 49), ("succeeded", 50)],
)
def test_build_accepts_only_fifty_built_documents(status: str, built: int) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"id": "test-job"})
        assert request.url.params["job_id"] == "test-job"
        return httpx.Response(200, json={"job": {"status": status, "progress": {"built": built}}})

    with httpx.Client(
        base_url="https://acceptance.example.test", transport=httpx.MockTransport(respond),
    ) as client:
        if built == 50:
            assert acceptance.build_batch(client) < 600
        else:
            with pytest.raises(RuntimeError, match="50"):
                acceptance.build_batch(client)
