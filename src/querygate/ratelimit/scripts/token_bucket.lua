-- Atomic token-bucket rate limiter.
--
-- A bucket holds up to `capacity` tokens and refills at `rate` tokens/second.
-- A call costs `cost` tokens; it is allowed only if that many are available.
-- Because the whole read-refill-write cycle runs inside one Lua invocation,
-- concurrent callers across every replica serialize on the Redis key, so no
-- read-modify-write race, no double spend.
--
-- Token bucket rather than a fixed window on purpose: a fixed window lets a
-- caller spend a full quota at the end of one window and again at the start of
-- the next (2x the intended rate for an instant). A bucket smooths that out
-- while still permitting a deliberate `capacity`-sized burst.
--
-- KEYS[1] bucket key
-- ARGV[1] capacity   max tokens (burst)
-- ARGV[2] rate       tokens refilled per second
-- ARGV[3] now        client clock (seconds); <= 0 means "use the Redis clock"
-- ARGV[4] cost       tokens this call consumes
-- ARGV[5] ttl        seconds of idleness before the bucket is forgotten
--
-- Returns {allowed, tokens_left, retry_after_seconds}. The two float values
-- come back as strings because Lua->Redis conversion truncates numbers to ints.

local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])

if now <= 0 then
  -- Redis TIME is the single source of truth, so clock skew between app
  -- replicas cannot widen or narrow anyone's window.
  local t = redis.call('TIME')
  now = tonumber(t[1]) + tonumber(t[2]) / 1000000
end

local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])

if tokens == nil or ts == nil then
  -- First call from this caller: start with a full bucket.
  tokens = capacity
  ts = now
end

local elapsed = now - ts
if elapsed < 0 then
  elapsed = 0
end
tokens = math.min(capacity, tokens + elapsed * rate)

local allowed = 0
local retry_after = 0

if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  retry_after = (cost - tokens) / rate
end

redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], ttl)

return { allowed, tostring(tokens), tostring(retry_after) }
