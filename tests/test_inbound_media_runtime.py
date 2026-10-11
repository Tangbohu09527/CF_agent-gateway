from threading import Event, current_thread
from types import SimpleNamespace

from cf_agent_gateway.config import InboundMediaSettings, Settings, WechatSettings
from cf_agent_gateway.runtime import inbound_media as runtime


def test_disabled_media_runtime_does_not_read_credentials_or_database(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("disabled runtime touched a dependency")

    monkeypatch.setattr(runtime, "resolve_wechat_token", forbidden)
    monkeypatch.setattr(runtime, "create_database_engine", forbidden)
    with runtime.media_intake_runtime(Settings()):
        pass


def test_background_intake_recovers_cycle_failure_and_drains_on_exit(monkeypatch, caplog):
    events = []
    recovered = Event()
    finished = Event()
    engine = SimpleNamespace(dispose=lambda: events.append("dispose"))
    sessions = object()
    staging = object()
    client = object()

    monkeypatch.setattr(runtime, "InboundMediaStaging", lambda _root: staging)
    monkeypatch.setattr(runtime, "resolve_wechat_token", lambda _name: "synthetic-only")
    monkeypatch.setattr(runtime, "create_database_engine", lambda _url: engine)
    monkeypatch.setattr(runtime, "database_startup_check_enabled", lambda: True)
    monkeypatch.setattr(
        runtime, "check_database_migrations", lambda _engine: events.append("check")
    )
    monkeypatch.setattr(runtime, "create_database_session_factory", lambda _engine: sessions)
    monkeypatch.setattr(runtime, "InboundMediaHTTPClient", lambda _url, _token: client)

    class Intake:
        def __init__(self, candidate_sessions, candidate_client, candidate_staging):
            assert (candidate_sessions, candidate_client, candidate_staging) == (
                sessions,
                client,
                staging,
            )
            self.calls = 0

        def run_once(self):
            assert current_thread().name == "inbound-media"
            self.calls += 1
            events.append("cycle")
            if self.calls == 1:
                raise RuntimeError("sensitive upstream detail must not be logged")
            recovered.set()
            finished.set()

    monkeypatch.setattr(runtime, "InboundMediaWorker", Intake)
    settings = Settings(
        wechat=WechatSettings(enabled=True),
        inbound_media=InboundMediaSettings(enabled=True, public_base_url="http://127.0.0.1"),
    )
    assert not settings.hermes.enabled
    with runtime.media_intake_runtime(settings):
        assert recovered.wait(timeout=5)
        assert "dispose" not in events
    assert finished.is_set() and events == ["check", "cycle", "cycle", "dispose"]
    assert "sensitive upstream" not in caplog.text
