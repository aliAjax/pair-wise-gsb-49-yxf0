"""HTTP 路由与统一错误输出。"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

from .domain import Actor, DomainError, PermissionDenied, ValidationError


RECORD_RE = re.compile(r"^/api/records/(\d+)$")
ACTION_RE = re.compile(r"^/api/records/(\d+)/actions/([a-z_]+)$")
AUDIT_RE = re.compile(r"^/api/records/(\d+)/audit$")

# 可重算台账路由
L_YEAR_SUMMARY_RE = re.compile(r"^/api/ledger/treaty-years/([^/]+)/summary$")
L_YEAR_ID_RE = re.compile(r"^/api/ledger/treaty-years/([^/]+)$")
L_EVENT_ID_RE = re.compile(r"^/api/ledger/events/([^/]+)$")
L_EVENT_WITHDRAW_RE = re.compile(r"^/api/ledger/events/([^/]+)/withdraw$")
L_CLAIM_ID_RE = re.compile(r"^/api/ledger/claims/([^/]+)$")
L_CLAIM_RESERVE_RE = re.compile(r"^/api/ledger/claims/([^/]+)/reserve$")
L_CLAIM_BACKFILL_RE = re.compile(r"^/api/ledger/claims/([^/]+)/backfill$")
L_CLAIM_RELEASE_RE = re.compile(r"^/api/ledger/claims/([^/]+)/release$")
L_CLAIM_SETTLE_RE = re.compile(r"^/api/ledger/claims/([^/]+)/settle$")
L_BATCH_ID_RE = re.compile(r"^/api/ledger/batches/([^/]+)$")
L_YEAR_RECOMPUTE_RE = re.compile(r"^/api/ledger/treaty-years/([^/]+)/recompute$")


def make_handler(service: Any, static_dir: Path, ledger_service: Any = None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "reinsurance-exposure/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _actor(self) -> Actor:
            user_id = self.headers.get("X-User-Id", "").strip()
            role = self.headers.get("X-Role", "").strip()
            if not user_id or not role:
                raise PermissionDenied("缺少X-User-Id或X-Role")
            return Actor(user_id=user_id, role=role, organization=self.headers.get("X-Org", ""))

        def _body(self) -> Dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValidationError("Content-Length无效") from exc
            if length > 1024 * 1024:
                raise ValidationError("请求体过大")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体必须是JSON") from exc
            if not isinstance(data, dict):
                raise ValidationError("JSON顶层必须是对象")
            return data

        def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
            if content_type.startswith("application/json"):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            else:
                body = payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, DomainError):
                self._send(exc.status, {"error": exc.code, "message": str(exc)})
            else:
                self._send(500, {"error": "internal_error", "message": "服务内部错误"})

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    self._send(200, {"status": "ok", "service": "reinsurance-exposure", "database": service.repository.health()})
                    return
                if parsed.path == "/":
                    page = (static_dir / "index.html").read_bytes()
                    self._send(200, page, "text/html; charset=utf-8")
                    return
                if parsed.path == "/api/records":
                    query = parse_qs(parsed.query)
                    records = service.list_records(self._actor(), state=query.get("state", [None])[0], limit=int(query.get("limit", ["100"])[0]))
                    self._send(200, {"items": records})
                    return
                match = RECORD_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_record(self._actor(), int(match.group(1))))
                    return
                match = AUDIT_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": service.timeline(self._actor(), int(match.group(1)))})
                    return
                if parsed.path == "/api/stats":
                    self._send(200, service.stats(self._actor()))
                    return
                if ledger_service is not None and parsed.path.startswith("/api/ledger/"):
                    self._handle_ledger_get(parsed)
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                body = self._body()
                if parsed.path == "/api/records":
                    record = service.create(self._actor(), body.get("reference", ""), body.get("data", {}))
                    self._send(201, record)
                    return
                if ledger_service is not None and parsed.path.startswith("/api/ledger/"):
                    self._handle_ledger_post(parsed, body)
                    return
                match = ACTION_RE.match(parsed.path)
                if match:
                    version = body.get("expected_version")
                    if not isinstance(version, int):
                        raise ValidationError("expected_version必须是整数")
                    record = service.act(self._actor(), int(match.group(1)), version, match.group(2), body.get("data", {}))
                    self._send(200, record)
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

        def _handle_ledger_get(self, parsed) -> None:
            actor = self._actor()
            path = parsed.path
            if path == "/api/ledger/treaty-years":
                self._send(200, {"items": ledger_service.list_treaty_years(actor)})
                return
            match = L_YEAR_SUMMARY_RE.match(path)
            if match:
                self._send(200, ledger_service.year_summary(actor, match.group(1)))
                return
            if path == "/api/ledger/events":
                self._send(200, {"items": ledger_service.list_events(actor)})
                return
            match = L_EVENT_ID_RE.match(path)
            if match and not path.endswith("/withdraw"):
                with ledger_service.repository.tx() as conn:
                    self._send(200, ledger_service._resolve_event_key(conn, match.group(1)))
                return
            if path == "/api/ledger/claims":
                query = parse_qs(parsed.query)
                self._send(200, {"items": ledger_service.list_claims(actor, status=query.get("status", [None])[0])})
                return
            match = L_CLAIM_ID_RE.match(path)
            if match and not any(path.endswith(suffix) for suffix in ("/reserve", "/backfill", "/release", "/settle")):
                self._send(200, ledger_service.get_claim(actor, match.group(1)))
                return
            match = L_BATCH_ID_RE.match(path)
            if match:
                self._send(200, ledger_service.batch_status(actor, match.group(1)))
                return
            if path == "/api/ledger/audit":
                query = parse_qs(parsed.query)
                entity = query.get("entity", [None])[0]
                entity_id = query.get("entity_id", [None])[0]
                self._send(200, {"items": ledger_service.audit_timeline(actor, entity=entity, entity_id=int(entity_id) if entity_id else None)})
                return
            self._send(404, {"error": "not_found", "message": "台账路径不存在"})

        def _handle_ledger_post(self, parsed, body) -> None:
            actor = self._actor()
            path = parsed.path
            data = body.get("data", body) if isinstance(body, dict) else {}

            if path == "/api/ledger/treaty-years":
                self._send(201, ledger_service.setup_treaty_year(actor, data))
                return
            if path == "/api/ledger/events":
                self._send(201, ledger_service.register_event(actor, data))
                return
            if path == "/api/ledger/claims":
                self._send(201, ledger_service.register_claim(actor, data))
                return
            if path == "/api/ledger/claims/batch-reserve" or path == "/api/ledger/batches/reserve":
                self._send(200, ledger_service.batch_reserve(actor, data))
                return
            match = L_YEAR_RECOMPUTE_RE.match(path)
            if match:
                self._send(200, ledger_service.recompute_year(actor, match.group(1)))
                return
            match = L_YEAR_SUMMARY_RE.match(path)
            if match:
                self._send(200, ledger_service.year_summary(actor, match.group(1)))
                return
            match = L_EVENT_WITHDRAW_RE.match(path)
            if match:
                self._send(200, ledger_service.withdraw_event(actor, match.group(1), data))
                return
            match = L_EVENT_ID_RE.match(path)
            if match:
                self._send(200, ledger_service.amend_event(actor, match.group(1), data))
                return
            match = L_CLAIM_RESERVE_RE.match(path)
            if match:
                self._send(200, ledger_service.reserve_claim(actor, match.group(1), data))
                return
            match = L_CLAIM_BACKFILL_RE.match(path)
            if match:
                self._send(200, ledger_service.backfill_claim(actor, match.group(1), data))
                return
            match = L_CLAIM_RELEASE_RE.match(path)
            if match:
                self._send(200, ledger_service.release_claim(actor, match.group(1), data))
                return
            match = L_CLAIM_SETTLE_RE.match(path)
            if match:
                self._send(200, ledger_service.settle_claim(actor, match.group(1), data))
                return
            self._send(404, {"error": "not_found", "message": "台账路径不存在"})

    return Handler


def create_server(host: str, port: int, service: Any, static_dir: Path, ledger_service: Any = None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service, static_dir, ledger_service))
