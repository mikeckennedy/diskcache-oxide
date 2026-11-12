# Caching responses from other services

Your dashboard endpoint takes 1.2 seconds, and 1.1 of them are spent waiting for a partner API to return the same JSON it returned to the previous request. An app that calls other services (a payments API, a headless CMS, your own microservices) spends most of its latency waiting on them, and most of those answers don't change between calls. Caching them in a directory every worker shares means one request to the upstream per machine instead of one per worker, and a last known good answer to serve when the upstream is slow, or down, or has started returning the wrong thing with a 500.


# A cached fetch

The entry holds the response body, its `ETag` and a freshness deadline taken from the upstream's `Cache-Control: max-age`, with a default when there isn't one. A fresh entry is returned without a request. A stale one is revalidated with `If-None-Match`, so an unchanged resource costs a `304` with no body. And if the request fails while there's a stale entry, the stale entry is the answer.


``` python
import json
import re
import tempfile
from urllib.parse import urlsplit

import httpx

from diskcache_oxide import Cache

now = [1_000_000.0]                                   # a fake clock; in a real app use time.time()
cache = Cache(tempfile.mkdtemp(), clock=lambda: now[0])

def max_age(response: httpx.Response) -> float | None:
    m = re.search(r'max-age=(\d+)', response.headers.get('cache-control', ''))
    return float(m.group(1)) if m else None

def fetch_json(client: httpx.Client, url: str, *, default_ttl: float = 300, keep: float = 24 * 3600) -> object:
    key = ('http', 'GET', url)
    host = urlsplit(url).netloc                       # the tag, to drop one service's responses at once
    entry = cache.get(key)
    if entry is not None and now[0] < entry['fresh_until']:
        return json.loads(entry['body'])              # fresh: no request at all

    headers = {'If-None-Match': entry['etag']} if entry and entry['etag'] else {}
    try:
        response = client.get(url, headers=headers)
        if response.status_code != 304:
            response.raise_for_status()
    except httpx.HTTPError:
        if entry is not None:
            return json.loads(entry['body'])          # upstream trouble: the last good answer
        raise

    if response.status_code == 304:                   # unchanged: keep the body, extend its freshness
        entry['fresh_until'] = now[0] + (max_age(response) or default_ttl)
    else:
        entry = {
            'body': response.content,
            'etag': response.headers.get('etag'),
            'fresh_until': now[0] + (max_age(response) or default_ttl),
        }
    cache.set(key, entry, expire=keep, tag=host)
    return json.loads(entry['body'])
```


Here it runs against a mock upstream that counts its requests:


``` python
seen: list[str | None] = []

def upstream(request: httpx.Request) -> httpx.Response:
    seen.append(request.headers.get('If-None-Match'))
    headers = {'ETag': '"v1"', 'Cache-Control': 'max-age=60'}
    if request.headers.get('If-None-Match') == '"v1"':
        return httpx.Response(304, headers=headers)
    return httpx.Response(200, json={'episodes': [512, 513]}, headers=headers)

client = httpx.Client(transport=httpx.MockTransport(upstream))
url = 'https://api.example.com/episodes'

fetch_json(client, url)
fetch_json(client, url)                               # fresh: served from the cache
now[0] += 61
fetch_json(client, url)                               # stale: one conditional request, answered 304
print(f'{len(seen)} upstream requests; If-None-Match headers sent: {seen}')
```


    2 upstream requests; If-None-Match headers sent: [None, '"v1"']


Why it's shaped this way:

- **Freshness lives in the value, not in [expire](../reference/Cache.expire.md#diskcache_oxide.Cache.expire).** The entry's [expire](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) (`keep`) is how long a stale copy is worth holding on to for revalidation and outages. If freshness were the expiry, a stale entry would vanish just when it's useful.
- **`304` costs one small write.** The body is kept and only `fresh_until` changes. A [set()](../reference/Cache.set.md#diskcache_oxide.Cache.set) is needed because the deadline is inside the value; [touch()](../reference/Cache.touch.md#diskcache_oxide.Cache.touch) only moves the entry's expiry.
- **The key names the request.** It's a tuple of method and URL, so no string formatting is needed and a URL can't run into the next field. If the response depends on who's asking (an `Authorization` header, a tenant), add that identity to the tuple, `('http', 'GET', url, tenant_id)`, or one user's data will be served to another.
- **Every [set](../reference/Cache.set.md#diskcache_oxide.Cache.set) passes the tag.** An overwrite without `tag=` leaves the entry untagged, so the `304` path tags its write too.


# Dropping one service's responses

Each entry is tagged with its upstream's host, so when a service tells you its data changed (a CMS publish webhook, a deploy of your own microservice), [evict](../reference/Cache.evict.md#diskcache_oxide.Cache.evict) drops everything cached from it in one call, whatever the URLs were:


``` python
fetch_json(client, 'https://api.example.com/episodes/512')
print(cache.evict('api.example.com'), 'responses removed')
```


    2 responses removed


A tuple key has no string prefix, so `iterkeys(prefix=...)` can't find these entries; the tag is how you reach them as a group. See [Invalidating groups of keys](invalidating-groups.md).


# Variations

- **Serving the bytes straight through.** When your endpoint relays the upstream's body, store `response.content` and the `content-type`, and return them as they are, without parsing. Bodies of 64 KiB or more are stored in their own file, so a large payload costs no more than a small one.
- **Negative caching.** A `404` that costs a round trip every time is worth caching for a short while: store `{'status': 404, 'fresh_until': ...}` and return your own not-found from it. Don't cache `5xx` as answers; the stale-on-error path above already covers an upstream outage.
- **Headers not to keep.** Drop `Set-Cookie` and anything else tied to the one connection before storing a response's headers.
- **Only one worker should refetch.** When many requests find the same stale entry at once, they all revalidate. `304`s are cheap, so that's usually fine. If the upstream charges per call, wrap the refetch in a claim as the [stale-while-revalidate](stale-while-revalidate.md) recipe does.
