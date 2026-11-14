"""FastAPI application: protected pages, JSON API and static assets."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from .cleaner import ProcessCleaner
from .config import AppConfig, load_config
from .host import (
    InterfaceInspector,
    IpCommandInterfaceInspector,
    PortInspector,
    ProcNetPortInspector,
)
from .logging_setup import configure_logging
from .models import (
    DEFAULT_REMOTE_USER,
    AppError,
    CleanupError,
    HostProbeError,
    LISTEN_MODE_DESCRIPTIONS,
    LISTEN_MODE_LABELS,
    ListenMode,
    PolicyError,
    TunnelState,
    ValidationError,
    duration_label,
    iso_utc,
    utcnow,
    compact_remaining,
)
from .rate_limit import LimitDecision, LoginThrottle, SlidingWindowLimiter
from .security import (
    MESSAGE_INVALID_CREDENTIALS,
    SESSION_COOKIE,
    Session,
    SessionManager,
    csrf_tokens_equal,
    source_ip,
)
from .service import TunnelService
from .store import PolicyStateStore

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = PROJECT_ROOT / "templates"
STATIC_DIR = PROJECT_ROOT / "static"


class AuthRequired(Exception):
    def __init__(self, next_path: str):
        self.next_path = next_path


class CsrfFailure(Exception):
    pass


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api/") or "application/json" in request.headers.get(
        "accept", ""
    )


def _error_response(
    request: Request, status_code: int, message: str, code: str = "error"
) -> JSONResponse | HTMLResponse:
    if _wants_json(request):
        return JSONResponse(
            {"error": {"code": code, "message": message}}, status_code=status_code
        )
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "request": request,
            "status_code": status_code,
            "message": message,
            "session": None,
            "csrf_token": "",
            "nav": False,
            "timezone_name": request.app.state.config.server.timezone,
        },
        status_code=status_code,
    )


def _safe_next(value: str | None) -> str:
    if not value:
        return "/apply"
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/apply"
    if any(ord(char) < 32 for char in value):
        return "/apply"
    if value.startswith("/login"):
        return "/apply"
    return value


def _format_local(value: datetime | None, zone: ZoneInfo, fmt: str) -> str:
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(zone).strftime(fmt)


def create_app(
    config: AppConfig | None = None,
    *,
    ports: PortInspector | None = None,
    interfaces: InterfaceInspector | None = None,
    cleaner: Any | None = None,
    service: TunnelService | None = None,
    session_manager: SessionManager | None = None,
    application_limiter: SlidingWindowLimiter | None = None,
    status_limiter: SlidingWindowLimiter | None = None,
    login_throttle: LoginThrottle | None = None,
) -> FastAPI:
    if config is None:
        config = load_config_from_environment()
    configure_logging(config.logging.level, access_log=config.logging.access_log)

    zone = ZoneInfo(config.server.timezone)
    port_inspector = ports or ProcNetPortInspector()
    interface_inspector = interfaces or IpCommandInterfaceInspector()
    if service is None:
        process_cleaner = cleaner or ProcessCleaner(config.cleanup, config.tunnel)
        service = TunnelService(
            config,
            port_inspector,
            interface_inspector,
            process_cleaner,
            PolicyStateStore(config.storage.policy_state_file),
        )
    sm = session_manager or SessionManager(
        config.access.keys,
        config.access.session_ttl_seconds,
        config.access.cookie_secure,
    )
    apply_limiter = application_limiter or SlidingWindowLimiter(config.application_limit)
    state_limiter = status_limiter or SlidingWindowLimiter(config.status_limit)
    throttle = login_throttle or LoginThrottle(config.access.login_rate_limit)

    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    templates.env.filters["localtime"] = lambda value: _format_local(
        value, zone, "%Y-%m-%d %H:%M:%S"
    )
    templates.env.filters["localdate"] = lambda value: _format_local(
        value, zone, "%Y-%m-%d %H:%M"
    )
    templates.env.filters["duration"] = duration_label
    templates.env.filters["compact_remaining"] = compact_remaining
    templates.env.filters["iso_utc"] = iso_utc
    templates.env.filters["state_label"] = lambda value: {
        TunnelState.PENDING: "已分配，待连接",
        TunnelState.ESTABLISHED: "已建立",
        TunnelState.EXPIRED: "已过期",
        TunnelState.UNKNOWN: "未知",
    }.get(value, "未知")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(
        title="一次性反向 SSH 隧道",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.service = service
    app.state.templates = templates
    app.state.ports = port_inspector
    app.state.sessions = sm
    app.state.apply_limiter = apply_limiter
    app.state.status_limiter = state_limiter
    app.state.login_throttle = throttle
    app.state.zone = zone
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; font-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        response.headers.setdefault("Cache-Control", "no-store")
        if config.logging.access_log:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "request method=%s path=%s status=%s duration_ms=%.1f client=%s",
                request.method,
                request.url.path,
                response.status_code,
                elapsed_ms,
                source_ip(request),
            )
        return response

    # ------------------------------------------------------------- exceptions
    @app.exception_handler(AuthRequired)
    async def auth_required_handler(request: Request, exc: AuthRequired):
        if _wants_json(request):
            return JSONResponse(
                {
                    "error": {
                        "code": "unauthorized",
                        "message": "未登录或会话已过期",
                    }
                },
                status_code=401,
            )
        target = request.url.path
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse(
            "/login?" + urlencode({"next": target}), status_code=303
        )

    @app.exception_handler(CsrfFailure)
    async def csrf_handler(request: Request, exc: CsrfFailure):
        return _error_response(
            request, 403, "页面校验已失效，请刷新页面后重试", "csrf_failed"
        )

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        if isinstance(exc, ValidationError):
            payload = {
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "fields": exc.fields,
                }
            }
            if _wants_json(request):
                return JSONResponse(payload, status_code=exc.status_code)
        return _error_response(request, exc.status_code, exc.message, exc.code)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        message = {404: "页面不存在", 405: "请求方法不允许"}.get(
            exc.status_code, str(exc.detail)
        )
        return _error_response(request, exc.status_code, message, "http_error")

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        logger.exception("未处理的请求异常 path=%s", request.url.path)
        return _error_response(request, 500, "服务器内部错误，请查看服务日志", "internal_error")

    # ------------------------------------------------------------------ helpers
    def require_session(request: Request) -> Session:
        session = sm.get(request.cookies.get(SESSION_COOKIE))
        if session is None:
            target = request.url.path
            if request.url.query:
                target += "?" + request.url.query
            raise AuthRequired(target)
        return session

    def require_csrf(session: Session, supplied: str | None) -> None:
        if not csrf_tokens_equal(session.csrf_token, supplied):
            raise CsrfFailure()

    def base_context(request: Request, session: Session | None) -> dict[str, Any]:
        return {
            "request": request,
            "session": session,
            "csrf_token": session.csrf_token if session else "",
            "nav": session is not None,
            "timezone_name": config.server.timezone,
            "poll_interval": config.poll_interval_seconds,
        }

    def apply_context(
        request: Request,
        session: Session,
        *,
        values: dict[str, str] | None = None,
        errors: dict[str, str] | None = None,
        message: str | None = None,
        message_kind: str = "error",
        result: dict[str, Any] | None = None,
        status_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = base_context(request, session)
        values = values or {
            "remote_user": DEFAULT_REMOTE_USER,
            "remote_port": "22",
            "server_user": config.tunnel.user,
            "server_port": str(config.tunnel.server_port),
            "ttl_seconds": str(config.tunnel.default_ttl_seconds),
            "note": "",
        }
        context.update(
            {
                "values": values,
                "errors": errors or {},
                "message": message,
                "message_kind": message_kind,
                "result": result,
                "status_payload": status_payload,
                "policy": service.policy,
                "policy_info": service.policy_info(),
                "ttl_options": [
                    {
                        "seconds": seconds,
                        "label": duration_label(seconds),
                        "selected": str(seconds) == str(values.get("ttl_seconds")),
                    }
                    for seconds in config.tunnel.allowed_ttl_seconds
                ],
                "default_remote_port": 22,
                "default_remote_user": DEFAULT_REMOTE_USER,
                "default_server_user": config.tunnel.user,
                "default_server_port": config.tunnel.server_port,
            }
        )
        return context

    async def render_admin(
        request: Request,
        session: Session,
        *,
        message: str | None = None,
        message_kind: str = "info",
        form_mode: str | None = None,
        form_specific: str | None = None,
        form_error: str | None = None,
        status_code: int = 200,
    ):
        views, probe_error = await service.admin_views()
        policy_info = service.policy_info()
        mode = form_mode or service.policy.mode.value
        specific = (
            form_specific
            if form_specific is not None
            else (service.policy.specific_ip or config.tunnel.specific_listen_ip or "")
        )
        active = [view for view in views if view.record.expired_at is None]
        context = base_context(request, session)
        context.update(
            {
                "views": views,
                "probe_error": probe_error,
                "policy_info": policy_info,
                "policy": service.policy,
                "form_mode": mode,
                "form_specific": specific,
                "form_error": form_error,
                "message": message,
                "message_kind": message_kind,
                "active_count": len(active),
                "pending_count": sum(
                    1 for view in active if view.state is TunnelState.PENDING
                ),
                "established_count": sum(
                    1 for view in active if view.state is TunnelState.ESTABLISHED
                ),
                "expired_count": sum(
                    1 for view in views if view.state is TunnelState.EXPIRED
                ),
                "mode_options": [
                    {
                        "value": mode_value.value,
                        "label": LISTEN_MODE_LABELS[mode_value],
                        "description": LISTEN_MODE_DESCRIPTIONS[mode_value],
                        "risk": mode_value is ListenMode.ALL,
                    }
                    for mode_value in ListenMode
                ],
            }
        )
        return templates.TemplateResponse(
            request, "admin.html", context, status_code=status_code
        )

    # -------------------------------------------------------------------- pages
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        session = sm.get(request.cookies.get(SESSION_COOKIE))
        return RedirectResponse("/apply" if session else "/login", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        session = sm.get(request.cookies.get(SESSION_COOKIE))
        if session is not None:
            return RedirectResponse("/apply", status_code=303)
        context = {
            "request": request,
            "session": None,
            "csrf_token": "",
            "nav": False,
            "timezone_name": config.server.timezone,
            "error": None,
            "next_path": _safe_next(request.query_params.get("next")),
        }
        return templates.TemplateResponse(request, "login.html", context)

    @app.post("/login", response_class=HTMLResponse)
    async def login_submit(request: Request):
        ip = source_ip(request)
        form = await request.form()
        next_path = _safe_next(str(form.get("next") or request.query_params.get("next") or ""))
        access_key = form.get("access_key")
        context = {
            "request": request,
            "session": None,
            "csrf_token": "",
            "nav": False,
            "timezone_name": config.server.timezone,
            "error": None,
            "next_path": next_path,
        }
        if throttle.is_locked(ip):
            context["error"] = MESSAGE_INVALID_CREDENTIALS
            return templates.TemplateResponse(
                request, "login.html", context, status_code=401
            )
        candidate = access_key if isinstance(access_key, str) else ""
        if len(candidate) > 4096:
            candidate = "\x00" * 4096
        authentication = sm.authenticate(candidate)
        if authentication is None:
            throttle.record_failure(ip)
            logger.warning("登录失败 source=%s", ip)
            context["error"] = MESSAGE_INVALID_CREDENTIALS
            return templates.TemplateResponse(
                request, "login.html", context, status_code=401
            )
        throttle.reset(ip)
        token, session = authentication
        logger.info("登录成功 source=%s", ip)
        response = RedirectResponse(next_path, status_code=303)
        sm.set_cookie(response, token)
        return response

    @app.post("/logout")
    async def logout(request: Request):
        session = require_session(request)
        form = await request.form()
        require_csrf(session, str(form.get("csrf_token") or ""))
        sm.revoke(request.cookies.get(SESSION_COOKIE))
        response = RedirectResponse("/login", status_code=303)
        sm.clear_cookie(response)
        logger.info("用户退出登录 source=%s", source_ip(request))
        return response

    @app.get("/apply", response_class=HTMLResponse)
    async def apply_page(request: Request):
        session = require_session(request)
        return templates.TemplateResponse(
            request, "apply.html", apply_context(request, session)
        )

    @app.post("/apply", response_class=HTMLResponse)
    async def apply_submit(request: Request):
        session = require_session(request)
        form = await request.form()
        require_csrf(session, str(form.get("csrf_token") or ""))
        ip = source_ip(request)
        decision = apply_limiter.check(ip)
        server_user_raw = form.get("server_user")
        server_port_raw = str(form.get("server_port") or "").strip()
        values = {
            "remote_user": str(form.get("remote_user") or ""),
            "remote_port": str(form.get("remote_port") or ""),
            "server_user": str(
                config.tunnel.user
                if server_user_raw is None
                else server_user_raw
            ),
            "server_port": server_port_raw or str(config.tunnel.server_port),
            "ttl_seconds": str(form.get("ttl_seconds") or ""),
            "note": str(form.get("note") or ""),
        }
        if not decision.allowed:
            context = apply_context(
                request,
                session,
                values=values,
                message=f"申请过于频繁，请 {decision.retry_after} 秒后再试",
            )
            response = templates.TemplateResponse(
                request, "apply.html", context, status_code=429
            )
            response.headers["Retry-After"] = str(decision.retry_after)
            return response

        errors: dict[str, str] = {}
        server_port_raw = values["server_port"].strip()
        if not server_port_raw:
            server_port = config.tunnel.server_port
        else:
            try:
                server_port = int(server_port_raw)
            except ValueError:
                server_port = 0
                errors["server_port"] = "连接服务器的 SSH 端口必须是整数"
        remote_port_raw = values["remote_port"].strip()
        if not remote_port_raw:
            remote_port = 22
            values["remote_port"] = "22"
        else:
            try:
                remote_port = int(remote_port_raw)
            except ValueError:
                remote_port = 0
                errors["remote_port"] = "远程 SSH 端口必须是整数"
        try:
            ttl_seconds = int(values["ttl_seconds"])
        except ValueError:
            ttl_seconds = 0
            errors["ttl_seconds"] = "请选择有效期"
        if errors:
            return templates.TemplateResponse(
                request,
                "apply.html",
                apply_context(request, session, values=values, errors=errors),
                status_code=400,
            )
        try:
            application = await service.create_tunnel(
                remote_user=values["remote_user"],
                remote_port=remote_port,
                server_user=values["server_user"],
                server_port=server_port,
                ttl_seconds=ttl_seconds,
                note=values["note"],
                source=ip,
            )
            checked = await service.get_status(application.record.id)
        except ValidationError as exc:
            return templates.TemplateResponse(
                request,
                "apply.html",
                apply_context(
                    request,
                    session,
                    values=values,
                    errors=exc.fields,
                    message=exc.message,
                ),
                status_code=400,
            )
        except (HostProbeError, PolicyError, AppError) as exc:
            return templates.TemplateResponse(
                request,
                "apply.html",
                apply_context(
                    request,
                    session,
                    values=values,
                    message=exc.message if hasattr(exc, "message") else str(exc),
                ),
                status_code=getattr(exc, "status_code", 400),
            )
        payload = service.application_payload(application)
        status_payload = service.status_payload(checked)
        context = apply_context(
            request,
            session,
            values=values,
            result=payload,
            status_payload=status_payload,
            message=(
                "检测到同一来源刚刚已有未过期的隧道，已返回该隧道，未重复分配端口。"
                if application.reused
                else None
            ),
            message_kind="info",
        )
        return templates.TemplateResponse(
            request, "apply.html", context, status_code=201 if not application.reused else 200
        )

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_page(request: Request):
        session = require_session(request)
        message = None
        kind = "info"
        if request.query_params.get("deleted"):
            message = "隧道已删除，端口已回收。"
        elif request.query_params.get("saved"):
            message = "监听地址策略已保存，仅对新申请的隧道生效。"
        return await render_admin(request, session, message=message, message_kind=kind)

    @app.post("/admin/policy", response_class=HTMLResponse)
    async def admin_policy(request: Request):
        session = require_session(request)
        form = await request.form()
        require_csrf(session, str(form.get("csrf_token") or ""))
        mode = str(form.get("listen_mode") or "")
        specific = str(form.get("specific_ip") or "")
        try:
            await service.set_policy(mode, specific, source=source_ip(request))
        except (PolicyError, AppError) as exc:
            return await render_admin(
                request,
                session,
                form_mode=mode,
                form_specific=specific,
                form_error=getattr(exc, "message", str(exc)),
                status_code=getattr(exc, "status_code", 400),
            )
        return RedirectResponse("/admin?saved=1", status_code=303)

    @app.post("/admin/tunnels/{tunnel_id}/delete", response_class=HTMLResponse)
    async def admin_delete(request: Request, tunnel_id: str):
        session = require_session(request)
        form = await request.form()
        require_csrf(session, str(form.get("csrf_token") or ""))
        if str(form.get("confirm") or "") != "yes":
            return await render_admin(
                request,
                session,
                message="删除操作需要二次确认，请重新操作。",
                message_kind="error",
                status_code=400,
            )
        try:
            port = await service.delete_tunnel(tunnel_id, source=source_ip(request))
        except (CleanupError, AppError) as exc:
            return await render_admin(
                request,
                session,
                message=getattr(exc, "message", str(exc)),
                message_kind="error",
                status_code=getattr(exc, "status_code", 500),
            )
        logger.info(
            "管理页删除完成：port=%s source=%s", port, source_ip(request)
        )
        return RedirectResponse("/admin?deleted=1", status_code=303)

    # ---------------------------------------------------------------------- API
    def rate_limited(decision: LimitDecision) -> JSONResponse:
        response = JSONResponse(
            {
                "error": {
                    "code": "rate_limited",
                    "message": f"请求过于频繁，请 {decision.retry_after} 秒后再试",
                }
            },
            status_code=429,
        )
        response.headers["Retry-After"] = str(decision.retry_after)
        return response

    @app.post("/api/tunnels")
    async def api_create_tunnel(request: Request):
        session = require_session(request)
        require_csrf(session, request.headers.get("x-csrf-token"))
        decision = apply_limiter.check(source_ip(request))
        if not decision.allowed:
            return rate_limited(decision)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {
                    "error": {
                        "code": "invalid_json",
                        "message": "请求体必须是 JSON 对象",
                    }
                },
                status_code=400,
            )
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": {"code": "invalid_json", "message": "请求体必须是 JSON 对象"}},
                status_code=400,
            )
        fields: dict[str, str] = {}
        remote_user = body.get("remote_user", DEFAULT_REMOTE_USER)
        if not isinstance(remote_user, str):
            fields["remote_user"] = "远程 SSH 用户名必须是字符串"
            remote_user = ""
        server_user = body.get("server_user", config.tunnel.user)
        if not isinstance(server_user, str):
            fields["server_user"] = "连接服务器的 SSH 用户名必须是字符串"
            server_user = ""
        server_port_raw = body.get("server_port", config.tunnel.server_port)
        server_port = config.tunnel.server_port
        if server_port_raw is None or server_port_raw == "":
            server_port = config.tunnel.server_port
        elif isinstance(server_port_raw, int) and not isinstance(server_port_raw, bool):
            server_port = server_port_raw
        else:
            fields["server_port"] = "连接服务器的 SSH 端口必须是整数"
        remote_port_raw = body.get("remote_port", 22)
        remote_port = 22
        if remote_port_raw is None or remote_port_raw == "":
            remote_port = 22
        elif isinstance(remote_port_raw, int) and not isinstance(remote_port_raw, bool):
            remote_port = remote_port_raw
        else:
            fields["remote_port"] = "远程 SSH 端口必须是整数"
        ttl_raw = body.get("ttl_seconds")
        ttl_seconds = 0
        if isinstance(ttl_raw, int) and not isinstance(ttl_raw, bool):
            ttl_seconds = ttl_raw
        else:
            fields["ttl_seconds"] = "ttl_seconds 必须是整数"
        note = body.get("note", "")
        if note is None:
            note = ""
        if not isinstance(note, str):
            fields["note"] = "备注必须是字符串"
            note = ""
        if fields:
            return JSONResponse(
                {
                    "error": {
                        "code": "validation_error",
                        "message": "请检查填写内容",
                        "fields": fields,
                    }
                },
                status_code=400,
            )
        application = await service.create_tunnel(
            remote_user=remote_user,
            remote_port=remote_port,
            server_user=server_user,
            server_port=server_port,
            ttl_seconds=ttl_seconds,
            note=note,
            source=source_ip(request),
        )
        checked = await service.get_status(application.record.id)
        payload = jsonable_encoder(service.application_payload(application))
        payload.update(
            {
                "status": checked.state.value,
                "status_label": service.status_payload(checked)["state_label"],
                "checked_at": iso_utc(checked.checked_at),
                "expires_at": iso_utc(application.record.expires_at),
                "created_at": iso_utc(application.record.created_at),
                "remaining_seconds": checked.remaining_seconds,
            }
        )
        return JSONResponse(payload, status_code=201 if not application.reused else 200)

    @app.get("/api/tunnels/{tunnel_id}/status")
    async def api_status(request: Request, tunnel_id: str):
        require_session(request)
        decision = state_limiter.check(source_ip(request))
        if not decision.allowed:
            return rate_limited(decision)
        checked = await service.get_status(tunnel_id)
        payload = jsonable_encoder(service.status_payload(checked))
        payload["expires_at"] = iso_utc(checked.record.expires_at)
        payload["checked_at"] = iso_utc(checked.checked_at)
        payload["server_time"] = iso_utc(utcnow())
        payload["poll_interval"] = config.poll_interval_seconds
        return JSONResponse(payload)

    @app.delete("/api/tunnels/{tunnel_id}")
    async def api_delete(request: Request, tunnel_id: str):
        session = require_session(request)
        require_csrf(session, request.headers.get("x-csrf-token"))
        if request.headers.get("x-confirm-delete", "").lower() != "yes":
            return JSONResponse(
                {
                    "error": {
                        "code": "confirmation_required",
                        "message": "删除需要二次确认（X-Confirm-Delete: yes）",
                    }
                },
                status_code=400,
            )
        decision = apply_limiter.check(source_ip(request))
        if not decision.allowed:
            return rate_limited(decision)
        port = await service.delete_tunnel(tunnel_id, source=source_ip(request))
        return JSONResponse({"deleted": True, "port": port})

    # ------------------------------------------------------------------- health
    @app.get("/healthz")
    async def healthz(request: Request):
        try:
            await asyncio.to_thread(port_inspector.listening_ports)
        except HostProbeError as exc:
            return JSONResponse(
                {"status": "error", "host_probe": "failed", "message": str(exc)},
                status_code=503,
            )
        return {
            "status": "ok",
            "config": "loaded",
            "host_network": "ok",
            "timezone": config.server.timezone,
        }

    return app


def load_config_from_environment() -> AppConfig:
    import os

    from .config import CONFIG_ENV, DEFAULT_CONFIG_PATH

    return load_config(os.environ.get(CONFIG_ENV, DEFAULT_CONFIG_PATH))


def run() -> None:
    """Entry point used by python -m app and the container command."""
    import os
    import sys

    from .config import CONFIG_ENV, DEFAULT_CONFIG_PATH, ConfigError
    from .config import load_config

    path = os.environ.get(CONFIG_ENV, DEFAULT_CONFIG_PATH)
    try:
        config = load_config(path)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    configure_logging(config.logging.level, access_log=config.logging.access_log)
    if config.server.timezone:
        os.environ["TZ"] = config.server.timezone
        if hasattr(time, "tzset"):
            time.tzset()
    app = create_app(config)
    import uvicorn

    uvicorn.run(
        app,
        host=config.server.listen_host,
        port=config.server.port,
        log_config=None,
        access_log=False,
        server_header=False,
    )
