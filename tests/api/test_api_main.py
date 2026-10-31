"""Tests of the command line: one command starts the server and another warms the
caches.
"""

from llm_stubs import isolated  # noqa: F401  (an autouse fixture)

from app.api import __main__ as cli
from app.api import suggestions
from app.api.suggestions import NoKeys, Warmed, WarmStopped
from app.config import Settings


def test_no_arguments_serve_on_localhost_port_8000(monkeypatch):
    served = []
    monkeypatch.setattr(
        cli.uvicorn, "run", lambda app, **kwargs: served.append((app.title, kwargs))
    )
    monkeypatch.setattr(
        cli, "create_app", lambda: type("App", (), {"title": "the app"})()
    )
    assert cli.main([]) == 0
    assert served == [("the app", {"host": "127.0.0.1", "port": 8000})]


def test_replay_serves_the_app_on_keyless_settings(monkeypatch):
    # no key from the environment or the .env file may reach the replay server
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    made = []
    served = []

    def fake_create_app(settings=None, *args, **kwargs):
        made.append(settings)
        return type("App", (), {"title": "the app"})()

    monkeypatch.setattr(cli, "create_app", fake_create_app)
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: served.append(kwargs))
    assert cli.main(["--replay"]) == 0
    assert made == [Settings()]
    assert made[0].has_openai is False
    assert served == [{"host": "127.0.0.1", "port": 8000}]


def test_warm_prints_a_line_per_question_and_the_total(monkeypatch, capsys):
    done = [
        Warmed("first question", "act", 0.0123, 5, 1),
        Warmed("second question", "clarify", 0.0004, 1, 0),
    ]
    monkeypatch.setattr(cli.suggestions, "warm", lambda: done)
    assert cli.main(["warm"]) == 0
    out = capsys.readouterr().out
    assert "first question" in out and "act" in out and "$0.0123" in out
    assert "total: $0.0127" in out and "6 paid calls" in out


def test_warm_without_a_key_prints_one_line_and_exits_1(monkeypatch, capsys):
    def warm():
        raise NoKeys("make warm needs OPENAI_API_KEY in .env")

    monkeypatch.setattr(cli.suggestions, "warm", warm)
    assert cli.main(["warm"]) == 1
    assert capsys.readouterr().err == "make warm needs OPENAI_API_KEY in .env\n"


def test_a_warm_that_stops_says_where_and_exits_1(monkeypatch, capsys):
    def warm():
        raise WarmStopped(
            "Is it?", [Warmed("done one", "act", 0.01, 3, 0)], "budget reached"
        )

    monkeypatch.setattr(cli.suggestions, "warm", warm)
    assert cli.main(["warm"]) == 1
    captured = capsys.readouterr()
    assert "done one" in captured.out
    assert "stopped at 'Is it?': budget reached" in captured.err


def test_the_budget_cap_is_the_brief_s_15_cents():
    assert suggestions.WARM_CAP_USD == 0.15
