"""读 protobuf 编码的数据（Antigravity 的对话库里存的是这种）。只按字段号取值，不需要 .proto 定义，也不依赖 protobuf 库。

字段号由调用方自己写明出处。格式不对（截断、不认识的编码）时抛 ValueError。
"""


def _varint(data, i):
    n = shift = 0
    while True:
        if i >= len(data) or shift > 63:
            raise ValueError("protobuf 数据不完整")
        c = data[i]
        i += 1
        n |= (c & 0x7F) << shift
        if c < 0x80:
            return n, i
        shift += 7


def fields(data):
    """[(字段号, 值)]：数字是 int，长度前缀的（字符串、子消息、打包的数组）是 bytes，定长的 4 / 8 字节也是 bytes。"""
    out = []
    i = 0
    while i < len(data):
        key, i = _varint(data, i)
        num, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(data, i)
        elif wire == 2:
            n, i = _varint(data, i)
            value, i = data[i:i + n], i + n
        elif wire in (1, 5):
            n = 8 if wire == 1 else 4
            value, i = data[i:i + n], i + n
        else:
            raise ValueError(f"不认识的 protobuf 编码 {wire}")
        if i > len(data):
            raise ValueError("protobuf 数据不完整")
        out.append((num, value))
    return out


class Message:
    """一条消息。同一个字段出现几次时 get() 取最后一个（protobuf 的规矩），all() 取全部；没有这个字段时返回默认值。"""

    def __init__(self, data=b""):
        self._fields = fields(data or b"")

    def all(self, num):
        return [v for n, v in self._fields if n == num]

    def get(self, num, default=None):
        values = self.all(num)
        return values[-1] if values else default

    def int(self, num):
        value = self.get(num)
        return value if isinstance(value, int) else None

    def text(self, num):
        value = self.get(num)
        return value.decode("utf-8", "replace") if isinstance(value, bytes) else None

    def message(self, num):
        value = self.get(num)
        return Message(value if isinstance(value, bytes) else b"")

    def messages(self, num):
        return [Message(v) for v in self.all(num) if isinstance(v, bytes)]

    def time(self, num):
        """google.protobuf.Timestamp（1 秒、2 纳秒）转成毫秒，没有时返回 None。"""
        ts = self.message(num)
        seconds = ts.int(1)
        return seconds * 1000 + (ts.int(2) or 0) / 1e6 if seconds else None
