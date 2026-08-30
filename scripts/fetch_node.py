# scripts/fetch_node.py
"""并行分块下载 Node.js 官方 tar 包（构建镜像用）。

本机网络对单个 TCP 连接限速约 250KB/s，多连接并行可跑满带宽：
将目标文件按 Range 切为多段并发下载，合并后校验总字节数。
用法：python fetch_node.py [URL] [目标路径]
"""
import concurrent.futures
import os
import sys
import urllib.request

DEFAULT_URL = ("https://cdn.npmmirror.com/binaries/node/v22.17.1/"
               "node-v22.17.1-linux-x64.tar.xz")
CHUNKS = 8
MIN_CHUNK = 1 << 20  # 1MB


def _head(url: str, timeout: int = 30) -> int:
    """HEAD 请求获取 Content-Length。"""
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return int(r.headers["Content-Length"])


def _fetch_range(url: str, start: int, end: int, out: str) -> int:
    """下载 [start, end] 字节段到 out 文件，返回字节数。"""
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        data = r.read()
    with open(out, "wb") as f:
        f.write(data)
    return len(data)


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    dest = sys.argv[2] if len(sys.argv) > 2 else "/node.tar.xz"
    total = _head(url)
    chunk = max(MIN_CHUNK, total // CHUNKS)
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < total:
        end = min(start + chunk - 1, total - 1)
        ranges.append((start, end))
        start = end + 1
    parts = [f"/tmp/node-part-{i}.bin" for i in range(len(ranges))]
    got = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(ranges)) as ex:
        futs = [ex.submit(_fetch_range, url, s, e, parts[i])
                for i, (s, e) in enumerate(ranges)]
        for f in concurrent.futures.as_completed(futs):
            got += f.result()
    if got != total:
        print(f"下载不完整: {got}/{total}", file=sys.stderr)
        return 1
    with open(dest, "wb") as out:
        for p in parts:
            with open(p, "rb") as f:
                out.write(f.read())
            os.remove(p)
    print(f"node tarball downloaded: {total} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
