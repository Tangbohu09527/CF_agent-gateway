"""Exercise the actual observed runtime builder, not a bare dispatcher."""

from sqlalchemy import select
from test_inbound_media_queue import HOST, MEDIA, Hermes, admit, worker
from test_inbound_media_queue import rig as queue_rig

from cf_agent_gateway.config import Settings
from cf_agent_gateway.hermes.errors import HermesAPIError
from cf_agent_gateway.inbound.host_binding_models import InboundHostBinding
from cf_agent_gateway.runtime.dispatch_worker import build_dispatch_worker
from cf_agent_gateway.task.model import HermesDispatchStatus

rig = queue_rig


def test_runtime_observer_forwards_preparation_runtime_and_tip_verification(rig):
    admit(rig)
    assert worker(rig).run_once() == "ready"
    observations = []
    client = Hermes()
    runtime = build_dispatch_worker(
        Settings(inbound_media=MEDIA, host_binding=HOST),
        session_factory=rig.sessions,
        hermes_client=client,
        sender_factory=None,
        operation_observer=observations.append,
    )
    result = runtime.run_once()
    assert result.status is HermesDispatchStatus.SUCCESS
    assert observations == [True]
    assert len(client.contents) == 1
    assert client.invocations[0]["runtime_model"] == HOST.runtime_model
    with rig.sessions() as session:
        binding = session.scalar(select(InboundHostBinding))
        assert binding.state == "revoked" and binding.grant_ciphertext is None


def test_prepared_child_then_http_400_is_uncertain_and_never_auto_recreated(rig):
    admit(rig)
    assert worker(rig).run_once() == "ready"

    class RejectedHost(Hermes):
        def chat(self, content, **kwargs):
            raise HermesAPIError(operation="chat", status_code=400)

    observed = []
    runtime = build_dispatch_worker(
        Settings(inbound_media=MEDIA, host_binding=HOST),
        session_factory=rig.sessions,
        hermes_client=RejectedHost(),
        sender_factory=None,
        operation_observer=observed.append,
    )
    assert runtime.run_once().status is HermesDispatchStatus.UNCERTAIN
    assert runtime.run_once() is None
    assert observed == [False]
    with rig.sessions() as session:
        bindings = session.scalars(select(InboundHostBinding)).all()
        assert len(bindings) == 1 and bindings[0].grant_ciphertext is None
