# Source connectors

A connector pulls events from somewhere the customer controls and feeds them into the same pipeline as
the webhook (`events.in` -> worker). Adding a source of an existing protocol is configuration (UI or
`POST /v1/sources`), zero code. **Adding a new protocol is one class implementing `async def run(publish)`.**

```python
# app/connectors/base.py
Publish = Callable[[Sequence[SourceEvent]], Awaitable[int]]

class Connector(ABC):
    protocol: str                                   # sources.type this class serves
    def __init__(self, source: SourceSpec): ...     # id, name, type, preset, default_severity, config
    @abstractmethod
    async def run(self, publish: Publish) -> None:  # run until cancelled, reconnect internally
```

* `SourceEvent(raw: dict, key: str | None)` is one raw event. A stable `key` (an upstream message id)
  gives the event a deterministic `event_id`, so a redelivered message is dropped by the worker's
  idempotency check.
* `publish(events)` applies the source's preset (`split`, e.g. Alertmanager `alerts[]` -> one event
  each), builds the standard envelope (priority from the source's default severity), publishes the batch
  to `events.in` **with a broker confirm** and increments `alerting_ingested_total{source}`. It returns the
  number of events published. Raise = nothing was accepted; retry.
* Acknowledge your upstream only after `publish` returned: that is what makes the chain loss-free.
* A message that cannot become an event: `count_invalid(source)` and acknowledge it (never requeue it
  forever).
* Register the class in `CONNECTORS` (`runner.py`, `type -> class`). The runner starts one task per
  matching source, rescans the `sources` table every 30 s (new / changed / deleted sources), and cancels
  the task on shutdown.

## RabbitMQ (`type: rabbitmq`)

`config: {url?, queue}`. `url` defaults to our own `RABBITMQ_URL`; `queue` is the customer's queue name,
used literally (no `MQ_PREFIX`). The queue is declared durable if it does not exist yet. One consumer per
source, prefetch 200, batches of up to 100 messages per confirm round-trip. Connection or channel loss
ends the session and the supervisor reconnects with exponential backoff (1 s ... 30 s). Invalid JSON /
non-object / oversized messages are counted (`alerting_events_outcome_total{outcome="invalid"}`) and
acked.

The seeded `trade-gateway` source (`seed/fixtures/sources.yaml`) has `config: {queue: vistula.trade-gateway}`
(the seed adds our own `url`); the simulator publishes its trade events there.

## Presets and auto-resolve (`app/pipeline/presets/`)

`name -> Preset(mapping, split, on_event)`. `split` runs at ingest (webhook and connectors): an Alertmanager
body with `alerts[]` becomes one event per alert, each counted separately. When `on_event` returns
`RESOLVE` (Alertmanager `status: resolved`), the worker resolves the open alert of the matching rule with the
same fingerprint (actor `system`, note "Resolved by source (Alertmanager status=resolved)") and creates no
alert. Metric outcome label: `alerting_events_outcome_total{outcome="auto_resolved"}` (nothing open to
resolve counts as `unrouted`), so `ingested = sum(outcomes)` still holds.
