-- WHAT: creates the Phase 2 database tables and safety rules.
-- WHY: rules enforced here still hold if application code has a bug.
-- USED BY: 0001_payment_core.py, which Alembic runs.
-- SAFE TO CHANGE: never edit this after it has run on data you keep. Add a new migration instead.

CREATE TABLE merchants (  -- Businesses allowed to use the API.
  id            UUID PRIMARY KEY,  -- Unique merchant ID.
  api_key_hash  BYTEA NOT NULL UNIQUE,  -- CAREFUL: only a hash of the key, so a database leak does not reveal it.
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()  -- When the merchant was added.
);

CREATE TABLE payments (  -- One row per customer payment.
  id                UUID PRIMARY KEY,  -- Unique payment ID.
  merchant_id       UUID NOT NULL REFERENCES merchants(id),  -- Owner of this payment.
  amount_paise      BIGINT NOT NULL CHECK (amount_paise > 0),  -- CAREFUL: invariant I7, whole paise above zero.
  currency          CHAR(3) NOT NULL DEFAULT 'INR' CHECK (currency = 'INR'),  -- Version 1 supports rupees only.
  method            TEXT NOT NULL CHECK (method IN ('UPI', 'CARD', 'NETBANKING')),  -- Payment method.
  network           TEXT,  -- Card network, empty for other methods.
  instrument_token  TEXT NOT NULL CHECK (instrument_token LIKE 'tok\_%'),  -- CAREFUL: fake token only, never a card number.
  bank_code         TEXT NOT NULL,  -- Customer's fictional bank.
  state             TEXT NOT NULL CHECK (state IN ('CREATED', 'PROCESSING', 'AWAITING_CUSTOMER', 'PENDING_CONFIRMATION', 'SUCCEEDED', 'FAILED')),  -- Current state.
  deadline_at       TIMESTAMPTZ NOT NULL,  -- After this, no new attempts should start.
  version           INT NOT NULL DEFAULT 0,  -- Counts changes, which helps debugging.
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),  -- Creation time.
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()  -- Last change time.
);

CREATE TABLE idempotency_keys (  -- Remembers each request so retries cannot create duplicates.
  merchant_id    UUID NOT NULL REFERENCES merchants(id),  -- Keys are private to each merchant.
  key            TEXT NOT NULL,  -- The caller's Idempotency-Key.
  request_hash   BYTEA NOT NULL,  -- Fingerprint of the body, to catch one key reused for a different payment.
  payment_id     UUID REFERENCES payments(id),  -- Payment created by this key.
  response_code  INT,  -- Saved HTTP code. Empty means the request has not finished.
  response_body  JSONB,  -- Saved response, returned exactly on a retry.
  expires_at     TIMESTAMPTZ NOT NULL,  -- When a future cleanup job may remove it.
  PRIMARY KEY (merchant_id, key)  -- CAREFUL: invariant I1, one key creates at most one payment.
);

CREATE TABLE payment_attempts (  -- One row per try on one gateway.
  id                 UUID PRIMARY KEY,  -- Also sent to the gateway as its idempotency key.
  payment_id         UUID NOT NULL REFERENCES payments(id),  -- Parent payment.
  attempt_no         SMALLINT NOT NULL CHECK (attempt_no BETWEEN 1 AND 3),  -- At most 3 tries.
  gateway            TEXT NOT NULL,  -- Gateway used.
  state              TEXT NOT NULL CHECK (state IN ('INITIATED', 'AWAITING_CUSTOMER', 'UNKNOWN', 'DECLINED', 'FAILED_SAFE', 'SUCCEEDED', 'REVERSED')),  -- Current state.
  is_winner          BOOLEAN NOT NULL DEFAULT false CHECK (NOT is_winner OR state = 'SUCCEEDED'),  -- Only a success can win.
  customer_prompted  BOOLEAN NOT NULL DEFAULT false,  -- After a prompt, automatic failover is unsafe.
  failure_class      TEXT,  -- Gateway's failure code, if any.
  next_check_at      TIMESTAMPTZ,  -- Used by the Phase 3 Recovery Worker.
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),  -- Creation time.
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),  -- Last change time.
  UNIQUE (payment_id, attempt_no)  -- A payment cannot have two "attempt 1" rows.
);

CREATE UNIQUE INDEX one_active_attempt ON payment_attempts (payment_id)  -- CAREFUL: invariant I2.
  WHERE state IN ('INITIATED', 'AWAITING_CUSTOMER', 'UNKNOWN');  -- At most one unresolved attempt.

CREATE UNIQUE INDEX one_winner ON payment_attempts (payment_id)  -- CAREFUL: invariant I3.
  WHERE is_winner;  -- At most one winning success.

CREATE TABLE attempt_transitions (  -- Permanent history of every attempt state change.
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,  -- Order of history rows.
  attempt_id  UUID NOT NULL REFERENCES payment_attempts(id),  -- Attempt that changed.
  from_state  TEXT,  -- Previous state. Empty for the first row.
  to_state    TEXT NOT NULL,  -- New state.
  at          TIMESTAMPTZ NOT NULL DEFAULT now()  -- When it changed.
);

CREATE TABLE routing_decisions (  -- Why each attempt used its gateway.
  attempt_id      UUID PRIMARY KEY REFERENCES payment_attempts(id),  -- The attempt.
  options         TEXT[] NOT NULL,  -- Gateways that were eligible.
  chosen          TEXT NOT NULL,  -- Gateway selected.
  policy_version  TEXT NOT NULL,  -- Router version, so results can be compared later.
  at              TIMESTAMPTZ NOT NULL DEFAULT now()  -- Decision time.
);

CREATE TABLE gateway_config (  -- What each gateway supports.
  gateway     TEXT PRIMARY KEY,  -- Gateway name.
  priority    INT NOT NULL UNIQUE,  -- Static order. Lower is tried first.
  methods     TEXT[] NOT NULL,  -- Supported payment methods.
  skip_banks  TEXT[] NOT NULL,  -- Banks it cannot serve.
  networks    TEXT[] NOT NULL  -- Supported card networks.
);

-- CHANGE ME: keep these rows in step with simulator/catalog.toml. A test checks that they match.
INSERT INTO gateway_config VALUES
  ('alpha', 1, '{UPI,CARD,NETBANKING}', '{}', '{NET_X,NET_Y}'),  -- Default first choice.
  ('bravo', 2, '{UPI,CARD,NETBANKING}', '{BANK_F}', '{NET_X}'),  -- Second choice.
  ('cosmos', 3, '{CARD,NETBANKING}', '{}', '{NET_X,NET_Y}'),  -- No UPI.
  ('delta', 4, '{UPI,CARD,NETBANKING}', '{BANK_E}', '{NET_Y}'),  -- Fourth choice.
  ('echo', 5, '{UPI,CARD}', '{BANK_D}', '{NET_X,NET_Y}');  -- No netbanking.

CREATE FUNCTION block_change() RETURNS trigger AS $$  -- Shared guard for append-only history.
BEGIN  -- Function body starts.
  RAISE EXCEPTION 'attempt_transitions is append-only';  -- Refuse the update or delete.
END;  -- Function body ends.
$$ LANGUAGE plpgsql;  -- plpgsql is Postgres's built-in procedural language.

CREATE TRIGGER history_is_append_only BEFORE UPDATE OR DELETE ON attempt_transitions  -- Runs before any edit.
  FOR EACH ROW EXECUTE FUNCTION block_change();  -- Applies to every affected row.

CREATE FUNCTION keep_final_state() RETURNS trigger AS $$  -- Guard for invariant I5.
BEGIN  -- Function body starts.
  IF OLD.state IN ('SUCCEEDED', 'FAILED') AND NEW.state <> OLD.state THEN  -- A final state is being changed.
    RAISE EXCEPTION 'final payment state cannot change';  -- Refuse it.
  END IF;  -- End of the check.
  RETURN NEW;  -- Otherwise allow the update.
END;  -- Function body ends.
$$ LANGUAGE plpgsql;  -- Same built-in language.

CREATE TRIGGER final_state_is_permanent BEFORE UPDATE OF state ON payments  -- Runs before a state update.
  FOR EACH ROW EXECUTE FUNCTION keep_final_state();  -- Applies to every affected row.
