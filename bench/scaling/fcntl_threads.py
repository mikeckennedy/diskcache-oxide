import fcntl
import os
import shutil
import sys
import tempfile
import threading
import time


def run(paths, seconds=1.5):
    counts = [0] * len(paths)
    stop = threading.Event()
    bar = threading.Barrier(len(paths) + 1)

    def w(n):
        fd = os.open(paths[n], os.O_RDWR)
        lockf = fcntl.lockf
        SH = fcntl.LOCK_SH | fcntl.LOCK_NB
        UN = fcntl.LOCK_UN
        bar.wait()
        c = 0
        while not stop.is_set():
            for _ in range(1000):
                lockf(fd, SH, 1, 124)
                lockf(fd, UN, 1, 124)
            c += 1000
        counts[n] = c
        os.close(fd)

    ts = [threading.Thread(target=w, args=(n,)) for n in range(len(paths))]
    [t.start() for t in ts]
    bar.wait()
    t0 = time.perf_counter()
    time.sleep(seconds)
    stop.set()
    [t.join() for t in ts]
    return sum(counts) / (time.perf_counter() - t0) / 1000


root = tempfile.mkdtemp()
files = []
for i in range(16):
    p = f'{root}/f{i}'
    open(p, 'wb').write(b'\0' * 4096)
    files.append(p)
print(sys.version.split()[0], 'GIL', sys._is_gil_enabled())
for c in (1, 2, 4, 6, 8, 12):
    print(
        f'{c:2} threads, own file each: {run(files[:c]):7,.0f}k pairs/s   one shared file: {run([files[0]] * c):7,.0f}k'
    )
shutil.rmtree(root)
