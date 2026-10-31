"""Support code for the API tests.

build_api_data makes a small data folder in a temporary directory, with a graph and a few
real source files. make_app builds the app over that folder, and parse_events reads a
streamed response back into (event, data) pairs. serving, RawClient and CountingApp run
the app on a real local server, so a test can close the connection and see what the app
does when a client goes away.
"""

import json
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import uvicorn
from crew_stubs import build_crew_kg
from llm_stubs import SETTINGS
from openpyxl import Workbook
from reportlab.pdfgen import canvas

from app.api.app import create_app
from app.config import Settings

LOG_TEXT = (
    "01/04/2026 06:45:00 | ERR 200 | PUMP START\n"
    "19/04/2026 01:19:00 | ERR 217 | MAIN CYLINDER PRESSURE LOW\n"
)
NOTE_TEXT = "shift note\nnote about INC-2026-001\nsomething else\n"
SHEET_ROWS = [
    ["Part no", "Description", "On hand", "Unit", "Bin", "Lead time"],
    ["SEAL-KIT-50", "Main cylinder seal kit", 3, "pc", "B12", 14],
    ["HP-01", "Hydraulic press"],
]
PDF_PAGES = {
    1: "Press overview BH-250",
    3: "Alarm E-217 main cylinder pressure low. Check the seals for wear.",
    5: "Replace main cylinder seal kit. Lock out the main isolator first.",
}
REPORT = {"files": [], "run": {"mode": "full"}, "totals": {"nodes": {"Asset": 3}}}
NO_KEYS = Settings()
WITH_KEYS = Settings(
    openai_api_key=SETTINGS.openai_api_key,
    typesafe_api_key="ts-test-secret-value",
    langfuse_public_key="pk-lf-test-public",
    langfuse_secret_key="sk-lf-test-secret",
)


def _pdf(path: Path) -> None:
    sheet = canvas.Canvas(str(path))
    for number in range(1, 6):
        sheet.drawString(72, 700, PDF_PAGES.get(number, f"page {number}"))
        sheet.showPage()
    sheet.save()


def build_api_data(root: Path) -> Path:
    """The data folder under root; returns the path of its kg.sqlite."""
    data = root / "data"
    kg_path = build_crew_kg(data / "kg.sqlite")
    for folder in ("logs", "inventory", "manuals", "notes"):
        (data / "raw" / folder).mkdir(parents=True)
    (data / "raw" / "logs" / "hp.log").write_text(LOG_TEXT)
    (data / "raw" / "notes" / "note.txt").write_text(NOTE_TEXT)
    book = Workbook()
    book.active.title = "Spares"
    for row in SHEET_ROWS:
        book.active.append(row)
    book.save(data / "raw" / "inventory" / "spares.xlsx")
    _pdf(data / "raw" / "manuals" / "hp.pdf")
    (data / "pipeline").mkdir()
    (data / "pipeline" / "latest.json").write_text(json.dumps(REPORT))
    return kg_path


def make_app(root: Path, settings: Settings = NO_KEYS, **options):
    kg_path = (
        build_api_data(root)
        if not (root / "data").exists()
        else root / "data/kg.sqlite"
    )
    options.setdefault("evals_path", root / "evals" / "results" / "latest.json")
    options.setdefault("replay_check", lambda question: False)
    return create_app(settings, kg_path, **options)


def parse_events(body: str) -> list[tuple[str, object]]:
    """The (event, data) pairs of a text/event-stream body, comments left out."""
    events = []
    for block in body.split("\n\n"):
        name, data = None, []
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data.append(line[len("data: ") :])
        if name is not None:
            events.append((name, json.loads("\n".join(data)) if data else None))
    return events


# --- a scripted crew ---

QUESTION = "HP-01 stopped with E-217, oil under the press"


def scripted_crew(monkeypatch, triage=None, models=None, decider=None):
    """Replace the crew with scripted answers. Return a list that records each run."""
    from ask_stubs import FakeEmbedder, calls, plan_model, say, terra_model
    from crew_stubs import FakeDecider, choice, sol_model, triage_answers

    from app.ask import crew
    from app.ask.crew import Models

    fixes = [
        ("fixes_for", {"failure_mode_id": mode})
        for mode in ("FM-SEAL-WEAR", "FM-RELIEF-MISADJUSTED", "FM-SPINDLE-WEAR")
    ]
    supported = choice("supported", 0.9, {"partly": 0.05, "not_supported": 0.05})

    def answer(purpose, state, questions):
        if purpose == "triage":
            return triage or triage_answers()
        return dict.fromkeys(questions, supported)

    def make_models():
        return Models(
            graph=terra_model(
                script=[
                    calls(
                        ("alarm_causes", {"asset_id": "HP-01", "code": "E-217"}), *fixes
                    ),
                    say(),
                ]
            ),
            manual=plan_model(["seal kit", "oil leak"]),
            synth=sol_model(
                "Seal wear is the likely cause. Check the seals first.",
                [
                    ("Seals wear first on this press.", ["C1"]),
                    ("Past breakdowns had oil on the floor.", ["C2"]),
                ],
            ),
            embedder=FakeEmbedder(),
        )

    real, runs = crew.stream_ask, []

    def stream_ask(question, history=None, **kwargs):
        runs.append((question, history))
        kwargs.update(
            decider=decider or FakeDecider(answer), models=models or make_models()
        )
        return real(question, history, **kwargs)

    monkeypatch.setattr(crew, "stream_ask", stream_ask)
    return runs


# --- a real server and a raw client ---


class CountingApp:
    """Wraps the app and counts the body bytes it receives."""

    def __init__(self, app):
        self.app = app
        self.bytes_read = 0

    async def __call__(self, scope, receive, send):
        async def counted():
            message = await receive()
            if message["type"] == "http.request":
                self.bytes_read += len(message.get("body", b""))
            return message

        await self.app(scope, counted if scope["type"] == "http" else receive, send)


@contextmanager
def serving(app):
    """Run the app on a real local server; yield its port."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert time.monotonic() < deadline and thread.is_alive(), "no server"
        time.sleep(0.02)
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(20)


class RawClient:
    """A plain connection that reads a stream as it arrives and can close it early."""

    opened: list["RawClient"] = []  # every client of the test, closed at the end

    def __init__(self, port: int):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.text = ""
        RawClient.opened.append(self)

    @classmethod
    def close_all(cls) -> None:
        while cls.opened:
            cls.opened.pop().close()

    def send(
        self, method: str, path: str, headers: dict[str, str], body: bytes
    ) -> "RawClient":
        head = "".join(f"{k}: {v}\r\n" for k, v in {"Host": "x", **headers}.items())
        self.sock.sendall(f"{method} {path} HTTP/1.1\r\n{head}\r\n".encode() + body)
        return self

    def post_json(self, path: str, payload) -> "RawClient":
        body = json.dumps(payload).encode()
        return self.send(
            "POST",
            path,
            {"Content-Type": "application/json", "Content-Length": str(len(body))},
            body,
        )

    def post_file(self, path: str, name: str, data: bytes) -> "RawClient":
        files = {"file": (name, data, "text/plain")}
        request = httpx.Request("POST", "http://x" + path, files=files)
        body = request.read()
        headers = {
            "Content-Type": request.headers["content-type"],
            "Content-Length": str(len(body)),
        }
        return self.send("POST", path, headers, body)

    def events(self, count: int, within: float = 10.0) -> list[tuple[str, object]]:
        """Read until the stream holds count events (or the time is up); return all."""
        deadline = time.monotonic() + within
        while time.monotonic() < deadline and len(self.parsed()) < count:
            self.sock.settimeout(max(0.05, deadline - time.monotonic()))
            try:
                chunk = self.sock.recv(65536)
            except TimeoutError:
                break
            if not chunk:
                break
            self.text += chunk.decode("utf-8", "replace")
        return self.parsed()

    def head(self, within: float = 10.0) -> int:
        """Read until the head of the response is in and return its status code."""
        deadline = time.monotonic() + within
        while "\r\n\r\n" not in self.text and time.monotonic() < deadline:
            self.sock.settimeout(max(0.05, deadline - time.monotonic()))
            chunk = self.sock.recv(65536)
            if not chunk:
                break
            self.text += chunk.decode("utf-8", "replace")
        return self.status

    def wait_for(self, name: str, within: float = 10.0) -> bool:
        """Read until an event of that name has arrived; False when the time is up."""
        deadline = time.monotonic() + within
        count = 0
        while time.monotonic() < deadline:
            left = max(0.05, deadline - time.monotonic())
            found = self.events(count + 1, within=left)
            if any(n == name for n, _ in found):
                return True
            if len(found) == count:
                break  # the stream ended
            count = len(found)
        return False

    def parsed(self) -> list[tuple[str, object]]:
        body = self.text.partition("\r\n\r\n")[2]
        return parse_events(body[: body.rfind("\n\n") + 2])  # whole events only

    @property
    def status(self) -> int:
        return int(self.text.split(" ", 2)[1])

    def close(self) -> None:
        self.sock.close()
