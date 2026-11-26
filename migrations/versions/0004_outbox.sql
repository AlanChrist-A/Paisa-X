-- WHAT: the transactional outbox, where events wait to be published to Kafka.
-- WHY: an event is saved in the SAME transaction as its state change, so a crash can never keep one without the other (invariant I6).
-- USED BY: 0004_outbox.py, which Alembic runs.
-- SAFE TO CHANGE: never edit this after it has run on data you keep. Add a new migration instead.

CREATE TABLE outbox (  -- One row per event.
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,  -- Publish order.
  event_id      UUID NOT NULL UNIQUE,  -- Consumers use this to remove duplicates.
  topic         TEXT NOT NULL,  -- Kafka topic it goes to.
  key           TEXT NOT NULL,  -- Kafka key: the payment ID, so one payment's events stay in order.
  event         JSONB NOT NULL,  -- The full event envelope.
  created_at    TIMESTAMPTZ NOT NULL,  -- When it was saved.
  published_at  TIMESTAMPTZ  -- Empty until Kafka confirms it.
);

-- Lets the relay find unpublished events quickly, while the index stays small.
CREATE INDEX outbox_unpublished ON outbox (id) WHERE published_at IS NULL;
