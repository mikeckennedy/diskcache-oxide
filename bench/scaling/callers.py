import re, sys, collections
path, target, depth = sys.argv[1], sys.argv[2], int(sys.argv[3])
lines = open(path).read().split('Call graph:')[1].split('Total number in stack')[0].splitlines()
pat = re.compile(r'^(\s*[+!:| ]*)(\d+)\s+(.*?)\s+\(in ([^)]*)\)')
stack = []
agg = collections.Counter()
def short(name):
    name = re.sub(r'_R[A-Za-z0-9_]+?(\d+[a-z_]+)', lambda m: m.group(0), name)
    for key in ['get_with','read_with_deadline','lease','checkout','checkin','try_enter_shared','try_acquire','enter_until','reserve_slot','Pool','GATE','gate','pool','genlock','slots','StatementCache','lookup','read_row','prepare','pymethod_get','Mutex','lock','unixShmLock','sqlite3','PyO3','pyo3','Python','attach','detach']:
        pass
    return name[:110]
for line in lines:
    m = pat.match(line)
    if not m: continue
    indent = len(m.group(1)); count = int(m.group(2)); name = m.group(3)
    while stack and stack[-1][0] >= indent: stack.pop()
    stack.append((indent, name, count))
    if target in name:
        chain = ' <- '.join(s[1][:90] for s in reversed(stack[-depth-1:-1]))
        agg[chain] += count
for chain, n in agg.most_common(8):
    print(n, '|', chain, '\n')
