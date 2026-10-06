"""极简二维码编码器（字节模式，版本 1–10，纠错 L/M/Q/H）。仅标准库。

用来把「文件码网址」变成二维码：手机扫一下就能在浏览器里取件。窗口和命令行（send --qr）共用。
算法按 ISO/IEC 18004，结构参考 Nayuki 的 QR Code generator（MIT）。

    m = encode("http://192.168.1.5:41234/Xk3…")   # -> list[list[bool]]，True 为深色
"""
from __future__ import annotations

_ECL = {"L": (0, 1), "M": (1, 0), "Q": (2, 3), "H": (3, 2)}     # 表下标, 格式位
# 每块纠错码字数 / 块数，下标为版本号（0 占位）
_ECC_PER_BLOCK = (
    (0, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18),
    (0, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26),
    (0, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24),
    (0, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28),
)
_NUM_BLOCKS = (
    (0, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4),
    (0, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5),
    (0, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8),
    (0, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8),
)
MAX_VERSION = 10


def _raw_modules(ver: int) -> int:
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n = ver // 7 + 2
        result -= (25 * n - 10) * n - 55
        if ver >= 7:
            result -= 36
    return result


def _data_codewords(ver: int, e: int) -> int:
    return _raw_modules(ver) // 8 - _ECC_PER_BLOCK[e][ver] * _NUM_BLOCKS[e][ver]


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


class _Matrix:
    def __init__(self, ver: int):
        self.ver = ver
        self.size = ver * 4 + 17
        self.mod = [[False] * self.size for _ in range(self.size)]
        self.fn = [[False] * self.size for _ in range(self.size)]

    def set_fn(self, x, y, dark):
        self.mod[y][x] = dark
        self.fn[y][x] = True

    def alignment_positions(self) -> list[int]:
        if self.ver == 1:
            return []
        n = self.ver // 7 + 2
        step = (self.ver * 8 + n * 3 + 5) // (n * 4 - 4) * 2
        return list(reversed([self.size - 7 - i * step for i in range(n - 1)] + [6]))

    def draw_function_patterns(self, fmt_bits: int):
        s = self.size
        for i in range(s):
            self.set_fn(6, i, i % 2 == 0)
            self.set_fn(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (s - 4, 3), (3, s - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < s and 0 <= y < s:
                        self.set_fn(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = self.alignment_positions()
        n = len(pos)
        for i in range(n):
            for j in range(n):
                if (i, j) in ((0, 0), (0, n - 1), (n - 1, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set_fn(pos[i] + dx, pos[j] + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(fmt_bits, 0)
        if self.ver >= 7:
            rem = self.ver
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.ver << 12 | rem
            for i in range(18):
                bit = (bits >> i) & 1 == 1
                a, b = s - 11 + i % 3, i // 3
                self.set_fn(a, b, bit)
                self.set_fn(b, a, bit)

    def draw_format(self, fmt_bits: int, mask: int):
        data = fmt_bits << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: (bits >> i) & 1 == 1  # noqa: E731
        s = self.size
        for i in range(6):
            self.set_fn(8, i, bit(i))
        self.set_fn(8, 7, bit(6))
        self.set_fn(8, 8, bit(7))
        self.set_fn(7, 8, bit(8))
        for i in range(9, 15):
            self.set_fn(14 - i, 8, bit(i))
        for i in range(8):
            self.set_fn(s - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set_fn(8, s - 15 + i, bit(i))
        self.set_fn(8, s - 8, True)

    def draw_codewords(self, data: list[int]):
        s = self.size
        i = 0
        right = s - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(s):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = (s - 1 - vert) if upward else vert
                    if not self.fn[y][x] and i < len(data) * 8:
                        self.mod[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask: int):
        f = _MASKS[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.fn[y][x] and f(x, y) == 0:
                    self.mod[y][x] = not self.mod[y][x]

    def penalty(self) -> int:
        """简化的惩罚分（行/列长串、2×2 同色块、深浅比例），用于挑选掩码。"""
        s, m, score = self.size, self.mod, 0
        for lines in (m, [list(col) for col in zip(*m)]):
            for row in lines:
                run, prev = 0, None
                for v in row:
                    if v == prev:
                        run += 1
                        if run == 5:
                            score += 3
                        elif run > 5:
                            score += 1
                    else:
                        run, prev = 1, v
        for y in range(s - 1):
            for x in range(s - 1):
                v = m[y][x]
                if v == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(row) for row in m)
        total = s * s
        k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        return score + max(0, k) * 10


_MASKS = (
    lambda x, y: (x + y) % 2,
    lambda x, y: y % 2,
    lambda x, y: x % 3,
    lambda x, y: (x + y) % 3,
    lambda x, y: (x // 3 + y // 2) % 2,
    lambda x, y: x * y % 2 + x * y % 3,
    lambda x, y: (x * y % 2 + x * y % 3) % 2,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2,
)


def encode(text: str, ecl: str = "M") -> list[list[bool]]:
    data = text.encode("utf-8")
    e, fmt_bits = _ECL[ecl]
    for ver in range(1, MAX_VERSION + 1):
        count_bits = 8 if ver <= 9 else 16
        if 4 + count_bits + len(data) * 8 <= _data_codewords(ver, e) * 8:
            break
    else:
        raise ValueError("内容太长，无法生成二维码")
    capacity = _data_codewords(ver, e) * 8
    bits: list[int] = []

    def put(value, n):
        bits.extend((value >> i) & 1 for i in reversed(range(n)))

    put(0b0100, 4)
    put(len(data), count_bits)
    for b in data:
        put(b, 8)
    put(0, min(4, capacity - len(bits)))
    put(0, (-len(bits)) % 8)
    pad = 0xEC
    while len(bits) < capacity:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]

    # 分块加纠错码并交织
    nblocks, ecclen = _NUM_BLOCKS[e][ver], _ECC_PER_BLOCK[e][ver]
    raw = _raw_modules(ver) // 8
    nshort = nblocks - raw % nblocks
    shortlen = raw // nblocks
    div = _rs_divisor(ecclen)
    blocks, k = [], 0
    for i in range(nblocks):
        dat = codewords[k:k + shortlen - ecclen + (0 if i < nshort else 1)]
        k += len(dat)
        ecc = _rs_remainder(dat, div)
        if i < nshort:
            dat = dat + [0]
        blocks.append(dat + ecc)
    final = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != shortlen - ecclen or j >= nshort:
                final.append(blk[i])

    best, best_score = None, None
    for mask in range(8):
        m = _Matrix(ver)
        m.draw_function_patterns(fmt_bits)
        m.draw_codewords(final)
        m.apply_mask(mask)
        m.draw_format(fmt_bits, mask)
        score = m.penalty()
        if best_score is None or score < best_score:
            best, best_score = m, score
    return best.mod


def to_terminal(matrix: list[list[bool]], border: int = 2) -> str:
    """用半格字符画二维码（两行合一行）：深色模块画成空白，浅色画成方块，适合深色终端也能扫。"""
    size = len(matrix)
    get = lambda x, y: 0 <= x < size and 0 <= y < size and matrix[y][x]  # noqa: E731
    lines = []
    for y in range(-border, size + border, 2):
        row = []
        for x in range(-border, size + border):
            top, bottom = not get(x, y), not get(x, y + 1)   # 反色：浅色 = 画出来
            row.append("█" if top and bottom else "▀" if top else "▄" if bottom else " ")
        lines.append("".join(row))
    return "\n".join(lines)
