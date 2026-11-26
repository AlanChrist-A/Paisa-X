# Decision 0003: How the Gateway Simulator is built

**Decision:** keep all simulation rules in a pure engine with no real clock or network, and wrap it in a thin FastAPI layer.

## Why

| Choice | Reason | Trade-off |
|---|---|---|
| Time is passed into the engine | The same run can be repeated exactly, and Replay Lab can run faster than real time | Callers must supply the time |
| Randomness comes from SHA-256 of seed + payment + gateway | Every routing method faces identical luck, across separate processes | Slower than a normal random generator |
| Market and faults live in TOML files | Anyone can change them without touching logic | Changing them creates a new experiment |
| Separate HTTP service | Paisa-X cannot read the hidden truth during payments | Adds a network hop |
| Tuning and locked scenarios are separate files | The final evaluation stays fair | Requires discipline, so the locked file needs an explicit unlock |

## Failure reproduced

**Case:** a request times out and the gateway answers "not found". Retrying elsewhere looks safe, but the delayed request later lands and charges the customer.

**Proof:** `test_in_flight_request_lands_later_unless_voided`. Without a void the charge appears. With a void the net charge stays zero.

## Verified

- All 38 tests pass, including the 8 from Phase 0.
- Two separate Python processes with different hash seeds produced the same run fingerprint.
- A real local server accepted a payment, repeated it safely, refused one during an injected outage, and delivered a correctly signed webhook.

## Known limits

- Byte-identical runs apply to the engine with supplied time. A live HTTP run uses the real clock, so its timing varies.
- A simulated timeout returns HTTP 504 quickly. It does not hold the connection open like a real hung gateway.
- All market numbers are planning assumptions, not real gateway data.
- Locked scenarios were only loaded to check their format. No router has been evaluated on them.
