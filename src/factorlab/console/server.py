"""Loopback-only HTTP transport; no command, mutation, or arbitrary-path routes."""

from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
import json
from urllib.parse import parse_qs, urlsplit

from factorlab.console.data import Catalog

CSP = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"


def one(query, key, default=""):
    values = query.pop(key, [default])
    if len(values) != 1:
        raise ValueError("Repeated parameter")
    return values[0]


def page(query):
    offset, limit = int(one(query, "offset", "0")), int(one(query, "limit", "50"))
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError("Invalid page")
    return offset, limit


def filters(query, names):
    result = {name: one(query, name) for name in names if name in query}
    if query:
        raise ValueError("Unknown parameter")
    return result


class ConsoleServer(HTTPServer):
    """Bind IPv4 loopback only, including when embedded by another application."""

    def __init__(self, catalog: Catalog, port=8765):
        self.catalog = catalog
        super().__init__(("127.0.0.1", port), Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "FactorLabConsole"
    sys_version = ""

    def log_message(self, *_):
        # Request strings can contain secrets; do not copy them to terminal logs.
        pass

    def reply(self, content, mime, code=200):
        self.send_response(code)
        for name, value in {
            "Content-Type": mime, "Content-Length": str(len(content)),
            "Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY", "Connection": "close",
        }.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(content)

    def json_reply(self, value, code=200):
        self.reply(json.dumps(value, ensure_ascii=False, allow_nan=False).encode(),
                   "application/json; charset=utf-8", code)

    def allowed_request(self):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            hosts.update({"127.0.0.1", "localhost"})
        if self.headers.get("Host") not in hosts:
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {"http://" + host for host in hosts}:
            return False
        return self.headers.get("Sec-Fetch-Site") not in {"cross-site", "same-site"}

    def do_GET(self):
        if not self.allowed_request():
            self.json_reply({"error": "Local same-origin access only"}, 403)
            return
        try:
            url = urlsplit(self.path)
            if url.scheme or url.netloc:
                raise ValueError("Absolute request target")
            query = parse_qs(url.query, keep_blank_values=True, max_num_fields=20)
            catalog = self.server.catalog
            if url.path in {"/", "/app.js", "/style.css"}:
                if query:
                    raise ValueError("Unexpected parameter")
                name, mime = {"/": ("index.html", "text/html; charset=utf-8"),
                              "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                              "/style.css": ("style.css", "text/css; charset=utf-8")}[url.path]
                self.reply(files("factorlab.console").joinpath("static", name).read_bytes(), mime)
                return
            if url.path == "/api/overview":
                filters(query, ())
                result = catalog.overview()
            elif url.path == "/api/evidence":
                project = one(query, "project")
                filters(query, ())
                result = catalog.evidence(project)
            elif url.path == "/api/table":
                key = one(query, "id")
                offset, limit = page(query)
                selected = filters(query, ("strategy", "year", "symbol", "industry", "omitted_symbol"))
                result = catalog.table(key, selected, offset, limit)
            elif url.path == "/api/experiments":
                offset, limit = page(query)
                selected = filters(query, ("project", "date", "model", "strategy", "status", "execution_status"))
                result = catalog.experiments(selected, offset, limit)
            elif url.path == "/api/experiment":
                store, identifier = one(query, "store"), one(query, "id")
                filters(query, ())
                result = catalog.detail(store, identifier)
            elif url.path == "/api/compare":
                store, ids = one(query, "store"), one(query, "ids").split(",")
                if not 2 <= len(ids) <= 10:
                    raise ValueError("Select 2–10 runs in one store")
                filters(query, ())
                try:
                    result = catalog.compare(store, ids)
                except ValueError as error:
                    self.json_reply({"error": "不可比较：" + str(error)}, 422)
                    return
            elif url.path == "/report":
                key = one(query, "id")
                filters(query, ())
                content, mime = catalog.report(key)
                self.reply(content, mime)
                return
            else:
                self.json_reply({"error": "Unknown route"}, 404)
                return
            self.json_reply(result)
        except (KeyError, FileNotFoundError):
            self.json_reply({"error": "Evidence not available / UNKNOWN"}, 404)
        except (ValueError, UnicodeError, OSError):
            self.json_reply({"error": "Invalid request or unreadable evidence"}, 400)

    def mutation(self):
        self.json_reply({"error": "Read-only console"}, 405)

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_CONNECT = mutation
