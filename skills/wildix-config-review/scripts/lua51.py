"""Minimal Lua 5.1 bytecode reader for Wildix per-user feature files (users.tar/<uid>.lua).

The files are luac output for scripts that only build tables of constants, e.g.
`features = { FCF = { status = "on", value = "..." }, ... }`. This module runs just the
table-building subset of the VM (no function calls, arithmetic or jumps beyond LOADBOOL's
skip). Anything else raises LuaDecodeError so the caller can fall back to string constants.
Standard library only.
"""
import struct


class LuaDecodeError(Exception):
    pass


class _Reader:
    def __init__(self, data):
        self.d = data
        self.p = 0

    def take(self, n):
        if self.p + n > len(self.d):
            raise LuaDecodeError('truncated chunk')
        b = self.d[self.p:self.p + n]
        self.p += n
        return b

    def byte(self):
        return self.take(1)[0]


def _header(r):
    if r.take(4) != b'\x1bLua':
        raise LuaDecodeError('not a Lua chunk')
    version, fmt, endian, int_sz, sizet_sz, instr_sz, num_sz, integral = r.take(8)
    if version != 0x51 or fmt != 0:
        raise LuaDecodeError(f'unsupported Lua version 0x{version:02x}')
    if instr_sz != 4 or num_sz != 8 or integral != 0 or int_sz != 4 or sizet_sz not in (4, 8):
        raise LuaDecodeError('unsupported number/size layout')
    e = '<' if endian else '>'
    return {
        'int': lambda: struct.unpack(e + 'i', r.take(4))[0],
        'size_t': lambda: struct.unpack(e + ('Q' if sizet_sz == 8 else 'I'), r.take(sizet_sz))[0],
        'instr': lambda: struct.unpack(e + 'I', r.take(4))[0],
        'number': lambda: struct.unpack(e + 'd', r.take(8))[0],
    }


def _string(r, h):
    n = h['size_t']()
    if n == 0:
        return None
    return r.take(n)[:-1].decode('utf-8', 'replace')  # drop trailing NUL


def _function(r, h):
    f = {'source': _string(r, h)}
    h['int'](); h['int']()  # linedefined, lastlinedefined
    r.take(4)               # nups, numparams, is_vararg, maxstacksize
    f['code'] = [h['instr']() for _ in range(h['int']())]
    consts = []
    for _ in range(h['int']()):
        t = r.byte()
        if t == 0:
            consts.append(None)
        elif t == 1:
            consts.append(bool(r.byte()))
        elif t == 3:
            v = h['number']()
            consts.append(int(v) if v.is_integer() else v)
        elif t == 4:
            consts.append(_string(r, h))
        else:
            raise LuaDecodeError(f'unknown constant type {t}')
    f['consts'] = consts
    f['protos'] = [_function(r, h) for _ in range(h['int']())]
    for _ in range(h['int']()):          # lineinfo
        h['int']()
    for _ in range(h['int']()):          # locvars
        _string(r, h); h['int'](); h['int']()
    for _ in range(h['int']()):          # upvalue names
        _string(r, h)
    return f


def string_constants(data):
    """All string constants in the chunk (fallback when execution isn't possible)."""
    r = _Reader(data)
    h = _header(r)
    out = []

    def walk(f):
        out.extend(c for c in f['consts'] if isinstance(c, str))
        for p in f['protos']:
            walk(p)
    walk(_function(r, h))
    return out


def _lua_table_to_py(t):
    """Lua tables -> dict, or list when keys are exactly 1..n."""
    if isinstance(t, dict):
        conv = {k: _lua_table_to_py(v) for k, v in t.items()}
        if conv and all(isinstance(k, int) for k in conv) and sorted(conv) == list(range(1, len(conv) + 1)):
            return [conv[i] for i in range(1, len(conv) + 1)]
        return {str(k): v for k, v in conv.items()}
    return t


def run_chunk(data):
    """Execute the table-building main function. Returns (globals, return_values)."""
    r = _Reader(data)
    h = _header(r)
    f = _function(r, h)
    K = f['consts']
    R = {}
    G = {}

    def RK(x):
        return K[x & 0xFF] if x & 0x100 else R.get(x)

    code = f['code']
    pc = 0
    while pc < len(code):
        i = code[pc]
        pc += 1
        op = i & 0x3F
        a = (i >> 6) & 0xFF
        c = (i >> 14) & 0x1FF
        b = (i >> 23) & 0x1FF
        bx = i >> 14
        if op == 0:        # MOVE
            R[a] = R.get(b)
        elif op == 1:      # LOADK
            R[a] = K[bx]
        elif op == 2:      # LOADBOOL
            R[a] = bool(b)
            if c:
                pc += 1
        elif op == 3:      # LOADNIL
            for j in range(a, b + 1):
                R[j] = None
        elif op == 5:      # GETGLOBAL
            R[a] = G.get(K[bx])
        elif op == 6:      # GETTABLE
            t = R.get(b)
            R[a] = t.get(RK(c)) if isinstance(t, dict) else None
        elif op == 7:      # SETGLOBAL
            G[K[bx]] = R.get(a)
        elif op == 9:      # SETTABLE
            t = R.get(a)
            if not isinstance(t, dict):
                raise LuaDecodeError('SETTABLE on non-table')
            key = RK(b)
            if key is not None:
                t[key] = RK(c)
        elif op == 10:     # NEWTABLE
            R[a] = {}
        elif op == 34:     # SETLIST
            if c == 0:
                c = code[pc]
                pc += 1
            t = R.get(a)
            if not isinstance(t, dict):
                raise LuaDecodeError('SETLIST on non-table')
            n = b if b else max((k for k in R if k > a), default=a) - a
            for j in range(1, n + 1):
                t[(c - 1) * 50 + j] = R.get(a + j)
        elif op == 30:     # RETURN
            n = (b - 1) if b else max((k for k in R if k >= a), default=a - 1) - a + 1
            return _lua_table_to_py(G), [_lua_table_to_py(R.get(a + j)) for j in range(n)]
        else:
            raise LuaDecodeError(f'unsupported opcode {op} at pc {pc - 1}')
    return _lua_table_to_py(G), []


def decode(data):
    """Best-effort: the table(s) the chunk builds, or None plus a constants list on failure.

    Returns {'ok': bool, 'globals': dict, 'returns': list, 'strings': list, 'error': str|None}.
    """
    try:
        g, ret = run_chunk(data)
        return {'ok': True, 'globals': g, 'returns': ret, 'strings': [], 'error': None}
    except LuaDecodeError as e:
        try:
            strings = string_constants(data)
        except LuaDecodeError:
            strings = []
        return {'ok': False, 'globals': {}, 'returns': [], 'strings': strings, 'error': str(e)}
