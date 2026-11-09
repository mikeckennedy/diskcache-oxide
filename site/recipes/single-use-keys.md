# Single-use keys

At 2 a.m. a tired user double-clicks a password reset link, and two requests arrive on two workers a few milliseconds apart. Both find the token. Both accept it. Some things must happen exactly once however many times they're asked for: a reset link is good for one click, a verification code for one entry, a payment request for one charge even when the client retries. Two cache calls are atomic across every worker on the machine, [pop()](../reference/Cache.pop.md#diskcache_oxide.Cache.pop) and [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add), and between them they cover all of these.


# One-time tokens with [pop()](../reference/Cache.pop.md#diskcache_oxide.Cache.pop)

Store the token when you issue it, with the lifetime the link should have. To redeem it, [pop()](../reference/Cache.pop.md#diskcache_oxide.Cache.pop) it: the value comes back and the entry is gone in one step, so two requests presenting the same token can't both succeed, whichever workers they land on.


``` python
import secrets
import tempfile
import threading

from diskcache_oxide import Cache

tokens = Cache(tempfile.mkdtemp())

def issue_reset_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    tokens.set(f'reset:{token}', user_id, expire=15 * 60)
    return token

def redeem_reset_token(token: str) -> int | None:
    """The user id the token was issued to, or None if it's unknown, expired or already used."""
    return tokens.pop(f'reset:{token}')

token = issue_reset_token(42)

# Eight requests present the same link at the same moment.
results = []
start = threading.Barrier(8)

def click() -> None:
    start.wait()
    results.append(redeem_reset_token(token))

threads = [threading.Thread(target=click) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()

print(f'{results.count(42)} redeemed, {results.count(None)} rejected')
```


    1 redeemed, 7 rejected


A few details that matter:

- **The token is the key, and it's random.** `secrets.token_urlsafe(32)` can't be guessed, so a key that exists is proof the link was issued. Never build the key from the user id alone.
- **Unknown, expired and used all look the same.** [pop()](../reference/Cache.pop.md#diskcache_oxide.Cache.pop) returns `None` for each, which is what the user should see too: one message, with no hint about which it was.
- **Issuing a new link should cancel the old one.** Keep a pointer from the user to their current token, `reset-for:{user_id}`, and [pop()](../reference/Cache.pop.md#diskcache_oxide.Cache.pop) it (then delete the token it names) before issuing a new one.


## Short codes need an attempt limit

A six-digit verification code is guessable, so pair it with a counter, created with an expiry the way the [rate limiting](rate-limiting.md) recipe does, and burn the code after a few wrong guesses:

``` python
def check_code(user_id: int, entered: str) -> bool:
    attempts_key = f'verify-attempts:{user_id}'
    tokens.add(attempts_key, 0, expire=10 * 60)
    if tokens.incr(attempts_key) > 5:
        tokens.delete(f'verify-code:{user_id}')   # too many tries: the code is dead
        return False
    if tokens.get(f'verify-code:{user_id}') != entered:
        return False
    tokens.delete(f'verify-code:{user_id}')       # right, and good for one entry only
    return True
```


# Idempotency keys with [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add)

A client that times out on a `POST` retries it, and the second attempt must not charge the card twice. The client sends the same idempotency key with both, and [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add) makes the first one to arrive the only one that does the work:

``` python
def charge(idempotency_key: str, amount_cents: int) -> dict:
    key = f'idem:{idempotency_key}'
    if not requests_seen.add(key, {'status': 'in-progress'}, expire=24 * 3600):
        previous = requests_seen.get(key)
        if previous is None or previous['status'] == 'in-progress':
            raise Conflict('this request is still being processed; retry in a moment')
        return previous['result']                   # the retry gets the first attempt's answer

    try:
        result = payments.charge(amount_cents)
    except Exception:
        requests_seen.delete(key)                   # let a retry try again
        raise
    requests_seen.set(key, {'status': 'done', 'result': result}, expire=24 * 3600)
    return result
```

The sequence: [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add) claims the key, and exactly one caller gets `True`. A retry that arrives while the first attempt is still running sees `in-progress` and is told to come back; one that arrives after it finished gets the stored result, which should carry everything needed to send the same response again (the status code too). If the work fails, the claim is deleted so that a retry really retries. `previous is None` covers the rare case where the entry expired, or was evicted, between the [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add) and the [get()](../reference/Cache.get.md#diskcache_oxide.Cache.get).

The stored result has to be something the cache can hold: a dict of built-in types, or a type you've [registered](custom-types.md).


# Keep them out of your main cache

Tokens and idempotency keys aren't a copy of something you can recompute, so two things that are fine for cached values are wrong for them:

- **Eviction.** A cache at its [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit) evicts the least recently stored entries, and an unused token is a tiny, old entry, exactly what goes first. An evicted reset link is a dead link; an evicted idempotency key is a double charge.
- **Switching caching off.** A [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) never stores, so [pop()](../reference/Cache.pop.md#diskcache_oxide.Cache.pop) returns `None` and no token can ever be redeemed, and its [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add) always returns `True`, so every retry charges again.

Give them a directory of their own, with a [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit) they'll never reach and no [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) in front of it, as the [job claims](run-once.md#give-claims-their-own-small-cache) recipe does. [`Caches`](../user-guide/caches.md) can register it under its own name so the rest of the app's caches can still be switched off.

`durable=True` is safe here, if you check return values: a [pop()](../reference/Cache.pop.md#diskcache_oxide.Cache.pop) whose delete fails returns `None` rather than risk delivering the token twice (the user clicks again), [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add) fails closed, and a [set()](../reference/Cache.set.md#diskcache_oxide.Cache.set) that was dropped returns `False`, so `issue_reset_token` should raise rather than hand out a link that was never stored. See [errors](../user-guide/errors.md).
