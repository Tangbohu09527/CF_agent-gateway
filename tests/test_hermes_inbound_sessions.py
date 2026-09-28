"""Pinned API shape tests; real official API evidence lives in hermes_host_probe."""

import json

import httpx
import pytest

from cf_agent_gateway.hermes.client import HermesClient, _history_digest
from cf_agent_gateway.hermes.errors import HermesResponseError

HISTORY = [{"id": 4, "session_id": "parent", "role": "user", "content": "retained history"}]
RUNTIME = {
    "runtime_model": "approved-model",
    "runtime_provider": "custom",
    "runtime_model_options": {"reasoning": {"enabled": False}},
}


class OfficialShapePeer:
    """HTTP contract test double, never a real Hermes/FileBrowser plugin."""

    def __init__(self):
        self.calls = []
        self.created = False
        self.corrupt = False
        self.uncertain = False
        self.old_tip = False

    def __call__(self, request):
        self.calls.append((request.method, request.url.path))
        assert request.headers["authorization"] == "Bearer synthetic-hermes-key"
        path = request.url.path
        if request.method == "POST" and path.endswith("/fork"):
            assert json.loads(request.content) == {"id": "child"}
            self.created = True
            if self.uncertain:
                raise httpx.ReadTimeout("synthetic lost response")
            return self.session("child", status=201)
        if path.endswith("/messages"):
            sid = path.split("/")[-2]
            history = [] if self.corrupt and sid == "child" else HISTORY
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "session_id": "rotated" if self.old_tip else sid,
                    "data": history,
                },
            )
        if path.endswith("/model"):
            assert json.loads(request.content) == {
                "model": "approved-model",
                "provider": "custom",
                "model_options": {"reasoning": {"enabled": False}},
                "require_model_lock": True,
            }
            return httpx.Response(
                200,
                json={
                    "object": "hermes.session.model_lock",
                    "session_id": "child",
                    "runtime": {
                        "model": "approved-model",
                        "provider": "custom",
                        "model_lock": "accepted",
                        "requested": {"model": "approved-model", "provider": "custom"},
                    },
                },
            )
        sid = path.split("/")[-1]
        if sid == "child" and not self.created:
            return httpx.Response(404)
        return self.session(sid)

    @staticmethod
    def session(sid, status=200):
        return httpx.Response(
            status,
            json={
                "object": "hermes.session",
                "session": {
                    "id": sid,
                    "parent_session_id": "parent" if sid == "child" else None,
                    "model": "approved-model",
                    "has_system_prompt": True,
                },
            },
        )


def prepare(peer, **overrides):
    client = HermesClient(
        "https://hermes.test",
        "synthetic-hermes-key",
        "unchanged-text-model",
        transport=httpx.MockTransport(peer),
    )
    args = {
        "parent_session_id": "parent",
        "allow_create": True,
        "expected_history_digest": None,
        "record_history": lambda _value: None,
        **RUNTIME,
    }
    args.update(overrides)
    client.prepare_inbound_session("child", **args)
    return client


def test_fork_history_configuration_and_exact_tip_verified():
    peer = OfficialShapePeer()
    proofs = []
    client = prepare(peer, record_history=proofs.append)
    assert proofs == [_history_digest(HISTORY)]
    assert peer.calls.index(("GET", "/api/sessions/parent/messages")) < peer.calls.index(
        ("POST", "/api/sessions/parent/fork")
    )
    client.verify_inbound_session_tip("child")


def test_lost_create_reply_recovery_inspects_same_child_without_refork():
    peer = OfficialShapePeer()
    peer.uncertain = True
    proofs = []
    with pytest.raises(HermesResponseError):
        prepare(peer, record_history=proofs.append)
    assert peer.created
    prepare(peer, allow_create=False, expected_history_digest=proofs[0])
    assert peer.calls.count(("POST", "/api/sessions/parent/fork")) == 1


@pytest.mark.parametrize("failure", ["history", "old_tip", "no_proof", "unknown_child"])
def test_unprovable_history_or_unknown_execution_fails_closed(failure):
    peer = OfficialShapePeer()
    peer.corrupt = failure == "history"
    peer.old_tip = failure == "old_tip"
    args = {}
    if failure in {"no_proof", "unknown_child"}:
        args["allow_create"] = False
        args["expected_history_digest"] = (
            None if failure == "no_proof" else _history_digest(HISTORY)
        )
    with pytest.raises(HermesResponseError):
        prepare(peer, **args)


def test_attachment_chat_explicit_runtime_preserves_failed_200_detection():
    calls = []

    def peer(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["model"] == "approved-model" and body["provider"] == "custom"
        assert body["model_options"] == {"reasoning": {"enabled": False}}
        return httpx.Response(
            200,
            headers={"X-Hermes-Session-Id": "child"},
            json={
                "choices": [{"message": {"role": "assistant", "content": "model failed"}}],
                "hermes": {"failed": True},
            },
        )

    client = HermesClient(
        "https://hermes.test",
        "synthetic-hermes-key",
        "unchanged-text-model",
        transport=httpx.MockTransport(peer),
    )
    with pytest.raises(HermesResponseError):
        client.chat("only attachment ID", hermes_thread_id="child", **RUNTIME)
    assert len(calls) == 1


def test_preparation_never_falls_back_to_empty_session_for_unknown_parent():
    calls = []

    def peer(request):
        calls.append(request.method)
        return httpx.Response(404)

    with pytest.raises(HermesResponseError):
        prepare(peer)
    assert calls == ["GET"]
