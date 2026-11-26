"""
WHAT: reads and writes live gateway-health counts in Redis.
WHY: every API and worker process can share one view of gateway health.
KEYS: health:{gateway}:{method}:{minute} = hash {ok, fail}
      seen:{event_id} = marks an event as already counted
      features:watermark:{partition} = newest event time in that partition minus the allowed lateness
      features:stats = hash {late} = events that arrived too late to count live
CAREFUL: Redis is NOT the source of truth. Everything here can be rebuilt from Kafka.
"""

import os  # Reads the Redis address.

import redis  # Redis client.

URL = os.getenv("PAISA_REDIS_URL", "redis://127.0.0.1:6379/0")  # CHANGE ME: Redis address.
TTL = 24 * 3600  # Keys expire after a day, so Redis cannot fill up.

# Runs inside Redis as ONE atomic step: no other client can see a half-finished update.
# It returns 0 = duplicate, 1 = counted, 2 = too late.
APPLY = """
if not redis.call('SET', KEYS[1], '1', 'NX', 'EX', ARGV[4]) then return 0 end
local at = tonumber(ARGV[2])
local mark = tonumber(redis.call('GET', KEYS[3]) or '-1')
if at < mark then
  redis.call('HINCRBY', KEYS[4], 'late', 1)
  return 2
end
redis.call('HINCRBY', KEYS[2], ARGV[1], 1)
redis.call('EXPIRE', KEYS[2], ARGV[4])
local new_mark = at - tonumber(ARGV[3])
if new_mark > mark then redis.call('SET', KEYS[3], new_mark) end
return 1
"""


def client(url=None):  # Connects to Redis. Short timeouts, so a dead Redis fails fast.
    return redis.Redis.from_url(url or URL, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)


def apply(r, event_id, gateway, method, minute, at, ok, lateness, partition=0):  # Counts one signal exactly once. Returns 0, 1 or 2.
    script = r.register_script(APPLY)  # Cached by Redis after the first call.
    # One watermark PER Kafka partition: partitions are read at different speeds, so one shared
    # watermark would race ahead on a fast partition and wrongly mark the others' events as late.
    keys = [f"seen:{event_id}", f"health:{gateway}:{method}:{minute}", f"features:watermark:{partition}", "features:stats"]  # CAREFUL: on a Redis cluster these must share one slot.
    return script(keys=keys, args=["ok" if ok else "fail", at, lateness, TTL])  # Run it atomically.


def all_health(r):  # Every minute bucket: {(gateway, method, minute): (ok, fail)}.
    out = {}  # Results.
    for key in r.scan_iter("health:*", count=500):  # SCAN does not block Redis, unlike KEYS.
        _, gateway, method, minute = key.split(":")  # Parse the key name.
        h = r.hgetall(key)  # Its counts.
        out[(gateway, method, int(minute))] = (int(h.get("ok", 0)), int(h.get("fail", 0)))
    return out
