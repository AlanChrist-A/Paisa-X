-- WHAT: adds webhook storage, charge reversals and recovery scheduling.
-- WHY: unclear payments must be resumable after any crash, using only what is saved here.
-- USED BY: 0002_recovery.py, which Alembic runs.
-- SAFE TO CHANGE: never edit this after it has run on data you keep. Add a new migration instead.

ALTER TABLE payment_attempts
  ADD COLUMN check_count INT NOT NULL DEFAULT 0,  -- How many status checks so far, used for backoff.
  ADD COLUMN must_void BOOLEAN NOT NULL DEFAULT false;  -- Another attempt won, so stop this one.

CREATE INDEX attempts_due ON payment_attempts (next_check_at)  -- Finds attempts that need checking.
  WHERE state IN ('INITIATED', 'AWAITING_CUSTOMER', 'UNKNOWN');  -- Only unresolved ones, keeping it small.

CREATE INDEX payments_interrupted ON payments (updated_at)  -- Finds payments stopped between steps.
  WHERE state IN ('CREATED', 'PROCESSING');  -- Only payments that may be stuck.

CREATE INDEX idempotency_by_payment ON idempotency_keys (payment_id);  -- Finds a payment's saved response quickly.

CREATE TABLE webhook_inbox (  -- Stores each genuine webhook before it is processed.
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,  -- Arrival order.
  gateway           TEXT NOT NULL,  -- Sending gateway.
  gateway_event_id  TEXT NOT NULL,  -- The gateway's own event ID.
  attempt_id        UUID NOT NULL,  -- Attempt it describes. No foreign key, so unknown IDs can be kept for inspection.
  status            TEXT NOT NULL,  -- Status reported by the gateway.
  payload           JSONB NOT NULL,  -- The full event, kept for debugging.
  received_at       TIMESTAMPTZ NOT NULL DEFAULT now(),  -- When it arrived.
  processed_at      TIMESTAMPTZ,  -- Empty means the worker still has to handle it.
  UNIQUE (gateway, gateway_event_id)  -- CAREFUL: duplicate deliveries are stored only once.
);

CREATE INDEX webhooks_waiting ON webhook_inbox (id) WHERE processed_at IS NULL;  -- Finds unprocessed webhooks quickly.

CREATE TABLE reversal_tasks (  -- Duplicate or late charges that must be returned.
  attempt_id   UUID PRIMARY KEY REFERENCES payment_attempts(id),  -- CAREFUL: one reversal per attempt.
  tries        INT NOT NULL DEFAULT 0,  -- How many times we tried.
  next_try_at  TIMESTAMPTZ NOT NULL,  -- When it is next due.
  done_at      TIMESTAMPTZ,  -- Empty means the money has not been confirmed returned.
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()  -- When the problem was found.
);

CREATE INDEX reversals_due ON reversal_tasks (next_try_at) WHERE done_at IS NULL;  -- Finds due reversals quickly.
