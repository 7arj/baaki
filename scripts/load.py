"""Tiny load generator (stdlib only): N concurrent clients hitting a URL for D seconds.

    python3 scripts/load.py https://baaki.localhost/login --clients 40 --seconds 180
"""

import argparse
import collections
import ssl
import threading
import time
import urllib.request

p = argparse.ArgumentParser()
p.add_argument("url")
p.add_argument("--clients", type=int, default=40)
p.add_argument("--seconds", type=int, default=180)
a = p.parse_args()

ctx = ssl._create_unverified_context()  # the local ingress uses a self-signed certificate
codes: collections.Counter = collections.Counter()
lock = threading.Lock()
deadline = time.time() + a.seconds


def worker():
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(a.url, context=ctx, timeout=10) as r:  # nosec B310
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception:
            code = "error"
        with lock:
            codes[code] += 1


threads = [threading.Thread(target=worker, daemon=True) for _ in range(a.clients)]
for t in threads:
    t.start()
for t in threads:
    t.join()
total = sum(codes.values())
print(f"{total} requests in {a.seconds}s ({total / a.seconds:.0f} req/s): {dict(codes)}")
