from __future__ import annotations

import logging

from nats.aio.client import Client as NatsClient
from nats.errors import NoRespondersError, TimeoutError as NatsTimeoutError

from axis_control.domain.commands import Command, DeliveryHint
from axis_shared.protocol import CommandMessage

log = logging.getLogger(__name__)


class NatsCommandPublisher:
    """Publish a command to `commands.<instance_id>` and report whether
    a subscriber was reachable.

    Implementation note: we use `nc.request()` (not `nc.publish()`) so the
    NATS server's "no responders" feature gives us a fast, definitive
    "no subscribers" signal — a 503 reply that surfaces as
    `NoRespondersError` in ~1 ms. When at least one subscriber exists,
    the request times out after `probe_timeout` (the agent does not reply
    to commands); that latency is intentional and configurable.
    """

    def __init__(
        self,
        client: NatsClient,
        probe_timeout: float = 0.1,
    ) -> None:
        self._client = client
        self._probe_timeout = probe_timeout

    async def publish(self, command: Command) -> DeliveryHint:
        message = CommandMessage(
            command_id=command.id,
            instance_id=command.instance_id,
            type=command.type,
            issued_at=command.issued_at,
        )
        subject = CommandMessage.subject_for(command.instance_id)
        payload = message.model_dump_json().encode("utf-8")
        try:
            await self._client.request(
                subject, payload, timeout=self._probe_timeout
            )
            return DeliveryHint.DELIVERED_NOW
        except NoRespondersError:
            return DeliveryHint.NO_LISTENERS
        except NatsTimeoutError:
            return DeliveryHint.DELIVERED_NOW
        except Exception:
            log.exception("unexpected error publishing command %s", command.id)
            return DeliveryHint.UNKNOWN
