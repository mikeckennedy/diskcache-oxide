# Server-side sessions

Every request arrives with a cookie and a question: who is this? A session is the one piece of per-user state almost every web app keeps server side to answer it: who's logged in, since when, with which roles. Keeping it in a cache directory means every worker sees the same session the instant it's written, a login on one worker is a login on all of them, and there's no database row per request and no Redis to run. The cookie holds only a random id.


# Start, load, end


``` python
import secrets
import tempfile

from diskcache_oxide import Cache

now = [1_000_000.0]                                   # a fake clock; in a real app use time.time()
sessions = Cache(tempfile.mkdtemp(), clock=lambda: now[0], eviction_policy='none')

IDLE = 30 * 60          # a session ends after 30 minutes without a request
ABSOLUTE = 12 * 3600    # and 12 hours after login, however active

def start_session(user_id: int, roles: list[str]) -> str:
    sid = secrets.token_urlsafe(32)
    sessions.set(f'session:{sid}', {'user_id': user_id, 'roles': roles, 'started': now[0]}, expire=IDLE)
    sessions.set(f'user-sessions:{user_id}:{sid}', True, expire=ABSOLUTE)
    return sid

def load_session(sid: str) -> dict | None:
    data, expires_at = sessions.get(f'session:{sid}', expire_time=True)
    if data is None or now[0] - data['started'] > ABSOLUTE:
        return None
    if expires_at - now[0] < IDLE / 2:                # slide the idle timeout, but not on every request
        sessions.touch(f'session:{sid}', expire=IDLE)
    return data

def end_session(sid: str) -> None:
    data = sessions.pop(f'session:{sid}')
    if data is not None:
        sessions.delete(f'user-sessions:{data["user_id"]}:{sid}')

sid = start_session(42, ['member'])
now[0] += 20 * 60
print('after 20 min:', load_session(sid) is not None)   # active, and the timeout slides
now[0] += 20 * 60
print('after 40 min:', load_session(sid) is not None)   # still active: the last request was 20 min ago
now[0] += 31 * 60
print('after 71 min:', load_session(sid) is not None)   # idle too long
```


    after 20 min: True
    after 40 min: True
    after 71 min: False


- **[touch()](../reference/Cache.touch.md#diskcache_oxide.Cache.touch) slides the idle timeout** without rewriting the session. It's a small write, so it's done only when less than half the idle time is left, which `get(expire_time=True)` tells you in the same read as the data. A busy user costs one write every 15 minutes instead of one per request.
- **The absolute lifetime is inside the value.** [expire](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) can only say when the session dies if it's left alone; the login time says when it dies regardless.
- **The id is the secret.** `secrets.token_urlsafe(32)` in an `HttpOnly`, `Secure`, `SameSite` cookie, and nothing else in the cookie. The session dict holds ids and roles, not the whole user object, which belongs in the database.


# Log out everywhere

A password change or a stolen laptop should end every session a user has. The second key written at login, `user-sessions:{user_id}:{sid}`, is an index for exactly this: a prefix scan finds them all.


``` python
other = start_session(42, ['member'])
sid = start_session(42, ['member'])

def end_all_sessions(user_id: int) -> int:
    ended = 0
    for key in sessions.iterkeys(prefix=f'user-sessions:{user_id}:'):
        sid = key.rsplit(':', 1)[1]
        ended += sessions.delete(f'session:{sid}')
        sessions.delete(key)
    return ended

print(end_all_sessions(42), 'sessions ended;', 'still valid:', load_session(other) is not None)
```


    2 sessions ended; still valid: False


Index entries for sessions that idled out stay until their own `ABSOLUTE` expiry, which is why they have one. To show a user their active sessions, read each `session:` key the index names and skip the ones that are gone.


# Sessions are not cache entries

A lost session is a forced logout, so sessions get a cache of their own, configured unlike the one for page fragments:

- **Nothing evicted for size.** Eviction removes the least recently stored entries, and a long-lived session is exactly that. Open the session cache with `eviction_policy='none'`, so a session goes only when it expires or ends. Sessions are small (a few hundred bytes each), so 1 GiB holds millions; watch [volume()](../reference/Cache.volume.md#diskcache_oxide.Cache.volume) if you store more per session, since under `'none'` the cache can grow past [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit).
- **Never a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache).** A [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) stores nothing, so nobody could log in. If the app's other caches can be switched off through [`Caches`](../user-guide/caches.md), register the session cache by name and leave it enabled.
- **Not `durable=True`.** A dropped write there means a login that silently didn't happen. Let [set()](../reference/Cache.set.md#diskcache_oxide.Cache.set) raise and show the user an error.
- **Run [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) from a scheduled job**, [claimed by one worker](run-once.md), so ended sessions free their space. Expired sessions are already invisible to [get()](../reference/Cache.get.md#diskcache_oxide.Cache.get); this is housekeeping.
- **One machine.** Every worker on the host shares the sessions. Behind a load balancer across several machines, use sticky sessions or a session store every machine can reach.
