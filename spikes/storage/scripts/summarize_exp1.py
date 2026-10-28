"""Summarize runs/exp1.log (one line per race run) by configuration."""

import collections
import re
import sys

agg: dict = collections.OrderedDict()
for line in open(sys.argv[1]):
    if not line.startswith('threshold'):
        continue
    kv = dict(re.findall(r'(\w+)=([\w.]+)', line))
    naive_delete = kv['naive'] == 'true' and int(kv['cas_deleted']) > 0
    cfg = (kv['threshold'], kv['readers'], kv['deletes'], kv['naive'] + ('+delete' if naive_delete else ''), kv['gap_us'])
    a = agg.setdefault(cfg, collections.Counter())
    a['runs'] += 1
    for k in ['hits', 'misses', 'true_misses', 'cas_deleted', 'torn', 'retries', 'errors', 'locked_fallbacks', 'orphan_val', 'tmp']:
        a[k] += int(kv[k])
    a['max_retry'] = max(a['max_retry'], int(kv['max_retry']))
    a['final_bad'] += kv['final_ok'] != 'true' or kv['rows'] != '1'
    a['files_not1'] += kv['files'] != '1'
    a['runs_with_miss'] += int(kv['misses']) > 0
    m = re.search(r'writer sets=(\d+) deletes=(\d+)', line)
    a['sets'] += int(m.group(1))
    a['dels'] += int(m.group(2))

cols = ['runs', 'sets', 'dels', 'hits', 'misses', 'runs_with_miss', 'true_misses', 'cas_deleted', 'torn', 'errors',
        'retries', 'max_retry', 'locked_fallbacks', 'final_bad', 'files_not1', 'orphan_val', 'tmp']
print('| threshold | readers | deletes | naive | gap µs | ' + ' | '.join(cols) + ' |')
print('|' + '---|' * (5 + len(cols)))
for c, a in agg.items():
    print('| ' + ' | '.join(c) + ' | ' + ' | '.join(str(a[k]) for k in cols) + ' |')
