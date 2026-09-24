# Change Data Capture (CDC)

Every insert, update and delete on the `kpi_sample` source table is captured as a CDC event,
stored exactly once, and later folded into an immutable **CDC data version** that the adaptation
pipeline can train and evaluate on like any uploaded version.

```
kpi_sample ──► source (Kafka topic  or  cdc_changelog) ──► consumer ──► cdc_event ──► materialize ──► DataVersion (kind CDC)
```

## Modes (`CDC_MODE`)

| Mode       | Where events come from | Use |
|------------|------------------------|-----|
| `kafka`    | Debezium reads PostgreSQL's WAL (logical decoding, `pgoutput`) and publishes to the topic `oran.public.kpi_sample`. The consumer reads it as a member of a consumer group. Needs the `kafka` extra (`confluent-kafka`). | Production (docker-compose) |
| `polling`  | Database triggers (migration 0005) copy every change into `cdc_changelog`. The consumer reads the rows past its stored offset. Works on SQLite and PostgreSQL. | Local fallback, tests |
| `disabled` | Nothing. Data arrives only through uploads. | Default |

Settings (environment variable = field name, see `core/config.py`):

| Variable | Default | Meaning |
|----------|---------|---------|
| `CDC_MODE` | `disabled` | `kafka`, `polling` or `disabled` |
| `CDC_BATCH_SIZE` | `500` | Events per batch |
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | Kafka brokers (`kafka:9092` in compose) |
| `CDC_KAFKA_TOPIC` | `oran.public.kpi_sample` | Debezium topic: `<topic.prefix>.<schema>.<table>` |
| `CDC_CONSUMER_GROUP` | `oran-adapt-cdc` | Kafka consumer group |
| `CDC_KAFKA_POLL_TIMEOUT_S` | `1.0` | How long one Kafka poll waits |

## Delivery guarantees

The consumer loop (`cdc/consumer.py`) runs one batch at a time:

1. **Fetch** a batch from the source. If the source is unreachable (broker down), nothing is
   stored or acknowledged, and the batch is simply fetched again later.
2. **Store** the events and the consumer's new position **in one database transaction**
   (`cdc/store.py`). Each event has a unique `event_id`, so an event that arrives again (a Kafka
   redelivery, or a poller restarted before its offset was saved) is counted as a duplicate
   and skipped.
3. **Acknowledge.** Kafka offsets are committed synchronously, and only after the database commit
   (`enable.auto.commit=false`). In polling mode the offset lives in `cdc_offset` and moves in
   the same transaction as step 2.

If the process crashes between steps 2 and 3, Kafka redelivers the batch and the unique
`event_id` drops the duplicates. The result is **at-least-once delivery with exactly-once
storage**. If storing fails, the transaction rolls back, nothing is acknowledged, and
`CdcProcessingError` is raised.

Metrics on `/api/v1/metrics`: `cdc_events_total{source,operation}` and `cdc_processing_lag`
(seconds between a source row change and the consumer processing it).

## Materializing a version

`materialize_cdc` (`cdc/materialize.py`) takes all of a dataset's events that have not been
materialized yet, in the order they were consumed, and works out the net change per source row:
either the row's last image or a deletion. The result becomes one new version:

- kind `CDC`, whose parent is the dataset's previous CDC version;
- rows keyed by the source primary key (`record_key`);
- deleted keys listed in the version metadata;
- `cdc_range` and `source_tx` recording which offsets and transactions it covers.

The events are marked with that version in the same transaction, so each event lands in exactly
one version. With nothing pending it returns nothing, so running it twice does no harm.

## PostgreSQL + Debezium setup (as in docker-compose)

- PostgreSQL runs with `wal_level=logical` (compose sets this on the command line).
- **Migration 0006** sets `ALTER TABLE kpi_sample REPLICA IDENTITY FULL`. By default a DELETE
  event carries only the primary key, but events are filed under the row's `dataset_id`, so
  deletes need the full old row. This costs more WAL per update or delete on that table.
- The connector config is in `deploy/debezium/kpi-connector.json`. The `debezium-init` service
  sends it with `PUT /connectors/oran-kpi-sample/config` (idempotent) once the API is healthy,
  which means the migrations, including the table, have run. Key settings:
  - `plugin.name=pgoutput`, `topic.prefix=oran`, `table.include.list=public.kpi_sample`
  - slot and publication `oran_kpi_sample` (`publication.autocreate.mode=filtered`)
  - `snapshot.mode=initial`: existing rows are streamed once as reads (`op: r`)
  - JSON converter with schemas disabled, which is the plain envelope that `from_debezium` parses
  - `database.password=${env:POSTGRES_PASSWORD}`, so the secret is resolved inside the Connect
    container and never written into the config
- The consumer skips delete tombstones (null values) and heartbeat messages.

**Operational note:** a replication slot keeps WAL until the connector reads it. If Debezium is
stopped for a long time, PostgreSQL's disk use grows. Drop the slot
(`SELECT pg_drop_replication_slot('oran_kpi_sample')`) if the connector is removed for good.

## CLI and API

```
oran-adapt cdc run                     # consume with CDC_MODE until interrupted
oran-adapt cdc run --mode polling --once
oran-adapt cdc run --mode kafka --max-batches 10
oran-adapt cdc materialize --dataset kpi
```

`POST /api/v1/datasets/{dataset_id}/cdc/materialize` does the same as `cdc materialize`, and
records the caller in the audit trail.

In docker-compose, the `cdc-consumer` service runs `oran-adapt cdc run --mode kafka`.

## Verification status

- Polling mode, the consumer loop, deduplication, offsets, materialization and the Debezium
  envelope parsing are covered by unit tests. The Kafka source is tested with a fake consumer.
- The real Debezium → Kafka → consumer path (docker-compose) has **not been run**.
