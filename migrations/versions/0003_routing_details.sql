-- WHAT: stores why the router made each decision.
-- WHY: fair evaluation and debugging need the scores, exploration flag and fallback flag.
-- USED BY: 0003_routing_details.py, which Alembic runs.
-- SAFE TO CHANGE: never edit this after it has run on data you keep. Add a new migration instead.

ALTER TABLE routing_decisions
  ADD COLUMN scores JSONB NOT NULL DEFAULT '{}',  -- Recent success rate of each option.
  ADD COLUMN explored BOOLEAN NOT NULL DEFAULT false,  -- True if this deliberately tested a non-best gateway.
  ADD COLUMN fallback BOOLEAN NOT NULL DEFAULT false;  -- True if the policy failed and static priority was used.
