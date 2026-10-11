"""Loopback JSON/SSE API for the local workbench, using only the standard library."""

from __future__ import annotations

import hmac
import json
import logging
import re
import threading
import time
from dataclasses import asdict, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from src.workbench.models import ConflictError, NotFoundError
from src.workbench.service import UnsupportedModeError, WorkbenchService

_LOG = logging.getLogger(__name__)
_TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
_ID = re.compile(r"^[a-z]+-[a-f0-9]{32}$")


def _encode(value):
    return json.dumps(
        value,
        default=lambda obj: asdict(obj) if is_dataclass(obj) else str(obj),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


class WorkbenchHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = True

    def __init__(self, address, service: WorkbenchService, *, token: str):
        if address[0] not in {"127.0.0.1", "localhost"}:
            raise ValueError("workbench HTTP must bind to loopback")
        if not isinstance(token, str) or len(token) < 24 or not token.isascii():
            raise ValueError("provide an ASCII bearer token of at least 24 characters")
        self.service = service
        self.token = token
        self.stream_slots = threading.BoundedSemaphore(16)
        self.request_slots = threading.BoundedSemaphore(32)
        self.stopping = threading.Event()
        super().__init__(address, WorkbenchHandler)

    def process_request(self, request, client_address):
        if not self.request_slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()

    def shutdown(self):
        self.stopping.set()
        super().shutdown()


class WorkbenchHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FixLoopWorkbench/2"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *_):
        # Do not log query strings, message bodies, or authorization headers.
        pass

    def _json(self, status, value):
        body = _encode(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _authenticate(self):
        expected_hosts = {
            f"127.0.0.1:{self.server.server_port}",
            f"localhost:{self.server.server_port}",
        }
        if self.headers.get("Host") not in expected_hosts:
            self._json(403, {"error": "invalid_host"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + host for host in expected_hosts}:
            self._json(403, {"error": "invalid_origin"})
            return False
        expected = "Bearer " + self.server.token
        auth = self.headers.get("Authorization", "")
        if not hmac.compare_digest(auth.encode("utf-8"), expected.encode("ascii")):
            self._json(401, {"error": "unauthorized"})
            return False
        return True

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("chunked request bodies are unsupported")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isdigit():
            raise ValueError("a single Content-Length is required")
        length = int(lengths[0])
        if not 0 < length <= 1_000_000:
            raise ValueError("JSON request size must be between 1 and 1000000 bytes")
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Content-Type must be application/json")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("incomplete request body")
        value = json.loads(
            raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid JSON number"))
        )
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    @staticmethod
    def _fields(body, *, allowed, required=()):
        if set(body) - set(allowed) or set(required) - set(body):
            raise ValueError("unknown or missing request fields")
        revision = body.get("expected_revision")
        if revision is not None and (type(revision) is not int or revision < 0):
            raise ValueError("expected_revision must be a nonnegative integer")
        for key in {"node_id", "branch_id"} & body.keys():
            value = body[key]
            if value is not None and (not isinstance(value, str) or not _ID.fullmatch(value)):
                raise ValueError(key + " must be a workbench ID or null")

    def do_GET(self):  # noqa: N802
        self._handle("GET")

    def do_POST(self):  # noqa: N802
        self._handle("POST")

    def do_PATCH(self):  # noqa: N802
        self._handle("PATCH")

    def _handle(self, method):
        try:
            if not self._authenticate():
                return
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc:
                raise ValueError("only relative API paths are accepted")
            query = parse_qs(parsed.query, keep_blank_values=True)
            if any(len(values) != 1 for values in query.values()):
                raise ValueError("duplicate query parameters")
            parts = parsed.path.strip("/").split("/")
            if parts[:1] != ["api"]:
                raise NotFoundError("route")
            body = self._body() if method in {"POST", "PATCH"} else {}
            self._dispatch(method, parts[1:], query, body)
        except NotFoundError:
            self._json(404, {"error": "not_found"})
        except ConflictError as exc:
            self._json(409, {"error": "conflict", "message": str(exc)})
        except UnsupportedModeError as exc:
            self._json(422, {"error": "unsupported_mode", "message": str(exc)})
        except (ValueError, TypeError, UnicodeError):
            self._json(400, {"error": "invalid_request"})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            _LOG.exception("workbench HTTP operation failed")
            self._json(500, {"error": "internal_error"})

    def _dispatch(self, method, parts, query, body):
        service = self.server.service
        store = service.store
        if parts == ["health"] and method == "GET":
            self._json(
                200, {"status": "ok", "stage": "S3", "modes": ["chat"], "file_restore": True}
            )
            return
        if parts == ["conversations"]:
            if method == "GET":
                self._json(200, {"conversations": store.list_conversations()})
                return
            if method == "POST":
                self._fields(body, allowed={"title", "project_path"})
                self._json(201, service.create_conversation(**body))
                return
        if len(parts) < 2 or parts[0] != "conversations" or not _ID.fullmatch(parts[1]):
            raise NotFoundError("route")
        cid = parts[1]
        if len(parts) == 2:
            if method == "GET":
                self._json(200, store.get_conversation(cid))
                return
            if method == "PATCH":
                self._fields(body, allowed={"title", "expected_revision"}, required={"title"})
                self._json(200, service.rename_conversation(cid, **body))
                return
        tail = parts[2:]
        revision = {"expected_revision": body.get("expected_revision")}
        if method == "GET" and tail == ["tree"]:
            self._json(200, store.get_tree(cid))
        elif method == "GET" and tail == ["workspace"]:
            self._json(200, service.project_status(cid))
        elif method == "POST" and tail == ["checkpoints"]:
            self._fields(body, allowed={"label", "expected_revision"})
            self._json(201, service.save_checkpoint(cid, **body))
        elif method == "POST" and tail == ["restores", "preview"]:
            self._fields(
                body,
                allowed={"action", "node_id", "branch_id", "name", "expected_revision"},
                required={"action"},
            )
            self._json(201, service.preview_restore(cid, **body))
        elif method == "POST" and tail == ["restores", "apply"]:
            self._fields(body, allowed={"plan_id"}, required={"plan_id"})
            self._json(200, service.apply_restore(cid, **body))
        elif method == "POST" and tail == ["restores", "recover"]:
            self._fields(
                body, allowed={"operation_id", "decision"}, required={"operation_id", "decision"}
            )
            self._json(200, service.recover_restore(cid, **body))
        elif method == "GET" and tail == ["messages"]:
            if set(query) - {"node_id"}:
                raise ValueError("unknown message query")
            if "node_id" in query:
                node_id = query["node_id"][0] or None
            else:
                tree = store.get_tree(cid)
                node_id = next(
                    b.head_node_id
                    for b in tree.branches
                    if b.id == tree.conversation.active_branch_id
                )
            self._json(200, {"messages": store.get_messages(cid, node_id)})
        elif method == "POST" and tail == ["turns"]:
            self._fields(body, allowed={"text", "mode", "expected_revision"}, required={"text"})
            self._json(202, service.send(cid, **body))
        elif method == "POST" and len(tail) == 3 and tail[0] == "nodes":
            if tail[2] == "edit":
                self._fields(body, allowed={"text", "expected_revision"}, required={"text"})
                self._json(202, service.edit(cid, tail[1], **body))
            elif tail[2] == "regenerate":
                self._fields(body, allowed={"expected_revision"})
                self._json(202, service.regenerate(cid, tail[1], **revision))
            else:
                raise NotFoundError("route")
        elif method == "POST" and tail == ["view"]:
            self._fields(body, allowed={"node_id", "expected_revision"}, required={"node_id"})
            self._json(200, service.view(cid, **body))
        elif method == "POST" and tail == ["fork"]:
            self._fields(
                body, allowed={"node_id", "name", "expected_revision"}, required={"node_id"}
            )
            self._json(201, service.fork(cid, **body))
        elif method == "POST" and tail == ["switch"]:
            self._fields(body, allowed={"branch_id", "expected_revision"}, required={"branch_id"})
            self._json(200, service.switch_branch(cid, **body))
        elif len(tail) >= 2 and tail[0] == "runs":
            rid = tail[1]
            run = store.get_run(cid, rid)
            if method == "GET" and len(tail) == 2:
                self._json(
                    200,
                    {
                        "run": run,
                        "partial_answer": service.journal.partial_answer(rid),
                        "event_cursor": service.journal.latest(rid),
                    },
                )
            elif method == "POST" and tail[2:] == ["stop"]:
                self._fields(body, allowed=set())
                self._json(202, service.stop(cid, rid))
            elif method == "GET" and tail[2:] == ["events"]:
                if set(query) - {"after"}:
                    raise ValueError("unknown event query")
                after = int(query.get("after", ["0"])[0])
                last_id = self.headers.get("Last-Event-ID")
                if last_id is not None:
                    prefix, separator, sequence = last_id.rpartition(":")
                    if not separator or prefix != rid:
                        raise ValueError("event cursor belongs to a different run")
                    after = int(sequence)
                events = service.events(cid, rid, after=after)
                self._stream(cid, rid, after, events)
            else:
                raise NotFoundError("route")
        else:
            raise NotFoundError("route")

    def _stream(self, cid, rid, after, initial):
        if not self.server.stream_slots.acquire(blocking=False):
            self._json(429, {"error": "stream_capacity"})
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            service = self.server.service
            events = initial
            heartbeat = time.monotonic()
            while not self.server.stopping.is_set():
                for event in events:
                    frame = (
                        (f"id: {rid}:{event['sequence']}\nevent: {event['kind']}\ndata: ").encode()
                        + _encode(event)
                        + b"\n\n"
                    )
                    self.wfile.write(frame)
                    after = event["sequence"]
                self.wfile.flush()
                events = service.journal.read(rid, after)
                if events:
                    continue
                run = service.store.get_run(cid, rid)
                if run.status in _TERMINAL:
                    # DB commit may precede its journal notification by a few ms.
                    # A final snapshot is authoritative and carries no replay ID.
                    self.wfile.write(
                        b"event: run.snapshot\ndata: " + _encode(asdict(run)) + b"\n\n"
                    )
                    self.wfile.flush()
                    return
                if time.monotonic() - heartbeat >= 5:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    heartbeat = time.monotonic()
                service.wait_for_change()
        except (OSError, TimeoutError):
            # Disconnects detach a viewer; they never cancel model execution.
            pass
        finally:
            self.server.stream_slots.release()
