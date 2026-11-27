"""H3 Toybox 的令牌门控反向代理。

TizenryA/colab-gpu-studio 有密码登录，Toybox 没有；但云端隧道网址一旦外流，
拿到的人就能白用你的 GPU 和 CU。这个 60 行的代理夹在隧道和 Toybox 之间，
让「手机 + 电脑共用一个网址」这件事不再靠「别外流」这种口头约定。

用法：
    python authproxy.py --listen 127.0.0.1:8191 --upstream 127.0.0.1:8190 --token SECRET

行为：
    ?k=SECRET   写 cookie 后 302 到去掉 k 的同一路径（手机粘贴一次即可）
    POST /__gate 登录表单（没带 k 时打开网页看到的页面）
    X-H3-Token: SECRET  给脚本 / curl 用
    都没有 -> 401，不碰上游

只做 HTTP/1.1 转发：Range（视频拖动进度）、大文件上传、chunked 请求体都原样透传。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import os
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COOKIE = "h3gate"
GATE_PATH = "/__gate"
# 免口令健康端点：只回一行 "ok"，不碰上游、不泄露任何东西。
# 启动探针用它，否则探针打到 / 会拿到 401，而 urllib 把 4xx 当异常 →
# 「门禁已经起来了」被误判成「门禁没启动」。
HEALTH_PATH = "/__health"
HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}
CHUNK = 64 * 1024

LOGIN_HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>H3 Toybox</title>
<style>
 body{font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;background:#111;color:#eee;
      display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
 form{background:#1c1c1e;padding:28px;border-radius:14px;width:min(88vw,340px)}
 h1{font-size:18px;margin:0 0 4px} p{color:#888;font-size:13px;margin:0 0 18px}
 input{width:100%;box-sizing:border-box;padding:12px;border-radius:9px;border:1px solid #333;
       background:#0d0d0d;color:#eee;font-size:16px}
 button{width:100%;margin-top:12px;padding:12px;border:0;border-radius:9px;background:#2f7;color:#041;
        font-size:16px;font-weight:600}
 .err{color:#f66;font-size:13px;margin-top:10px}
</style>
<form method="POST" action="/__gate">
 <h1>🧸 H3 Toybox</h1>
 <p>请输入访问口令</p>
 <input name="token" type="password" autofocus autocomplete="current-password" placeholder="访问口令">
 <button type="submit">进入</button>
 __ERR__
</form>
</html>
"""


def _cookie_value(token: str) -> str:
    return hmac.new(token.encode(), b"h3gate-v1", hashlib.sha256).hexdigest()


class Gate(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "h3gate"
    timeout = 900
    upstream = ("127.0.0.1", 8190)
    token = ""
    cookie_ok = ""
    relogin_after = 30 * 24 * 3600
    connect_timeout = 5

    # ---------- auth ----------

    def _query_token(self, path: str) -> tuple[str, str]:
        if "?" not in path:
            return path, ""
        base, _, qs = path.partition("?")
        pairs = urllib.parse.parse_qsl(qs, keep_blank_values=True)
        got = ""
        kept = []
        for key, value in pairs:
            if key == "k":
                got = got or value
            else:
                kept.append((key, value))
        return base + ("?" + urllib.parse.urlencode(kept) if kept else ""), got

    def _cookie_ok(self) -> bool:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            key, _, value = part.strip().partition("=")
            if key == COOKIE and hmac.compare_digest(value.strip().encode(), self.cookie_ok.encode()):
                return True
        return False

    def _header_ok(self) -> bool:
        got = self.headers.get("X-H3-Token") or ""
        return bool(got) and hmac.compare_digest(got.encode(), self.token.encode())

    def _send(self, status: int, body: bytes, headers: dict | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _deny(self, bad: bool = False) -> None:
        body = LOGIN_HTML.replace("__ERR__", '<div class="err">口令不正确</div>' if bad else "").encode()
        self._send(401, body, {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
        })

    def _grant(self, query: str, token: str) -> None:
        cookie = f"{COOKIE}={self.cookie_ok}; Path=/; HttpOnly; SameSite=Lax; Max-Age={self.relogin_after}"
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            cookie += "; Secure"
        self._send(302, b"", {"Location": query or "/", "Set-Cookie": cookie,
                             "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    # ---------- request body ----------

    def _body_stream(self):
        """返回 (迭代器, 长度或 None, 是否 chunked)。"""
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            def gen():
                while True:
                    line = self.rfile.readline(65537).strip()
                    if not line:
                        return
                    size = int(line.split(b";")[0], 16)
                    if size == 0:
                        while True:
                            trailer = self.rfile.readline(65537)
                            if trailer in (b"\r\n", b"\n", b""):
                                return
                    left = size
                    while left:
                        piece = self.rfile.read(min(CHUNK, left))
                        if not piece:
                            return
                        left -= len(piece)
                        yield piece
                    self.rfile.readline(65537)
            return gen(), None, True
        length = self.headers.get("Content-Length")
        if length is None:
            return iter(()), 0, False
        try:
            total = int(length)
        except ValueError:
            total = 0

        def gen():
            left = total
            while left > 0:
                piece = self.rfile.read(min(CHUNK, left))
                if not piece:
                    return
                left -= len(piece)
                yield piece
        return gen(), total, False

    # ---------- forwarding ----------

    def _forward(self, path: str) -> None:
        body, length, chunked = self._body_stream()
        excluded = HOP | {'cookie', 'x-h3-token'}
        headers = {k: v for k, v in self.headers.items() if k.lower() not in excluded}
        conn = http.client.HTTPConnection(*self.upstream, timeout=self.connect_timeout)
        response_started = False
        try:
            # 先按短超时建立连接（Toybox 没起来就立刻 502，不让浏览器干等），
            # 连上后再放宽到长超时，生成期间的慢响应照常等。
            conn.connect()
            conn.sock.settimeout(self.timeout)
            conn.putrequest(self.command, path, skip_host=True, skip_accept_encoding=True)
            for key, value in headers.items():
                conn.putheader(key, value)
            conn.putheader("Host", "%s:%d" % self.upstream)
            conn.putheader("X-Forwarded-For", self.client_address[0])
            if chunked:
                conn.putheader("Transfer-Encoding", "chunked")
            elif length:
                conn.putheader("Content-Length", str(length))
            conn.endheaders()
            for piece in body:
                if chunked:
                    conn.send(b"%x\r\n" % len(piece) + piece + b"\r\n")
                else:
                    conn.send(piece)
            if chunked:
                conn.send(b"0\r\n\r\n")

            resp = conn.getresponse()
            response_started = True
            self.send_response(resp.status, resp.reason)
            length_out = resp.getheader("Content-Length")
            has_cache = False
            for key, value in resp.getheaders():
                lk = key.lower()
                if lk in HOP:
                    continue
                if lk == "content-length" and length_out:
                    continue
                if lk == "cache-control":
                    has_cache = True
                self.send_header(key, value)
            if not has_cache and self.command == "GET" and resp.status == 200:
                clean_p = urllib.parse.urlsplit(path).path
                if clean_p.startswith(("/static/", "/library/")):
                    self.send_header("Cache-Control", "private, max-age=300")
            if length_out is not None:
                self.send_header("Content-Length", length_out)
                want_close = (self.headers.get("Connection") or "").lower() == "close"
                self.send_header("Connection", "close" if want_close else "keep-alive")
                self.close_connection = want_close
            else:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if self.command == "HEAD":
                return
            while True:
                piece = resp.read(CHUNK)
                if not piece:
                    break
                self.wfile.write(piece)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # 上游没起来 / 隧道抖动
            if response_started:
                self.close_connection = True
                return
            try:
                self._send(502, ("上游不可用：%s" % type(exc).__name__).encode(), {
                    "Content-Type": "text/plain; charset=utf-8"})
            except Exception:
                self.close_connection = True
        finally:
            conn.close()

    # ---------- handlers ----------

    def do_GET(self) -> None:
        path, got = self._query_token(self.path)
        if urllib.parse.urlsplit(self.path).path == HEALTH_PATH:
            return self._send(200, b"ok", {"Content-Type": "text/plain; charset=utf-8"})
        if got:
            if hmac.compare_digest(got.encode(), self.token.encode()):
                return self._grant(path, got)
            return self._deny(bad=True)
        if not (self._cookie_ok() or self._header_ok()):
            return self._deny()
        self._forward(path)

    do_HEAD = do_GET
    do_OPTIONS = do_GET

    def do_POST(self) -> None:
        path, _ = self._query_token(self.path)
        if urllib.parse.urlsplit(path).path == GATE_PATH:
            raw = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 8192)).decode(
                "utf-8", "replace")
            fields = dict(urllib.parse.parse_qsl(raw, keep_blank_values=True))
            if hmac.compare_digest(fields.get("token", "").encode(), self.token.encode()):
                return self._grant("/", fields.get("token", ""))
            return self._deny(bad=True)
        if not (self._cookie_ok() or self._header_ok()):
            return self._deny()
        self._forward(path)

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

    def log_request(self, code='-', size='-') -> None:
        # 只记请求路径，避免带 k 的登录网址进入日志。
        self.log_message('%s %s %s %s', self.command, ascii(self.path.partition('?')[0]), code, size)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[%s] %s %s\n" % (time.strftime("%H:%M:%S"), self.address_string(), fmt % args))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="127.0.0.1:8191")
    ap.add_argument("--upstream", default="127.0.0.1:8190")
    ap.add_argument("--token", default="")
    args = ap.parse_args()

    host, _, port = args.listen.rpartition(":")
    up_host, _, up_port = args.upstream.rpartition(":")
    token = (args.token or os.environ.get("H3_GATE_TOKEN", "")).strip()
    if not token:
        print("需要 --token", file=sys.stderr)
        return 2

    Gate.upstream = (up_host, int(up_port))
    Gate.token = token
    Gate.cookie_ok = _cookie_value(token)
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer((host or "127.0.0.1", int(port)), Gate)
    httpd.daemon_threads = True
    print("gate on %s:%s -> %s:%s" % (host, port, up_host, up_port), flush=True)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())