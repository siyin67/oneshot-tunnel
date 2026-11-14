from __future__ import annotations

import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from app.service import TunnelService
from app.store import PolicyStateStore
from app.web import create_app

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
TUNNEL_RE = re.compile(r'data-tunnel-id="([^"]+)"')
PASSWORD_RE = re.compile(r'id="remote-password">([^<]+)<')
SSH_COMMAND_RE = re.compile(r'id="ssh-command"><code>(.*?)</code>', re.DOTALL)
SERVER_PORT_INPUT_RE = re.compile(r'id="server_port".*?value="([^"]+)"', re.DOTALL)


@pytest.fixture
def web(base_config, ports, interfaces, cleaner, clock, id_factory):
    service = TunnelService(
        base_config,
        ports,
        interfaces,
        cleaner,
        PolicyStateStore(None),
        clock=clock,
        id_factory=id_factory,
    )
    app = create_app(
        base_config,
        ports=ports,
        interfaces=interfaces,
        cleaner=cleaner,
        service=service,
    )
    with TestClient(app) as client:
        yield client, service, ports, cleaner


def login(client: TestClient, key: str = "test-access-key") -> None:
    response = client.post(
        "/login",
        data={"access_key": key, "next": "/apply"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/apply"


def csrf_from(client: TestClient, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200
    match = CSRF_RE.search(response.text)
    assert match, f"no csrf token on {path}"
    return match.group(1)


def test_anonymous_pages_and_api_are_blocked(web):
    client, _, _, _ = web
    for path in ("/", "/apply", "/admin"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")
    response = client.get("/api/tunnels/anything/status")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    response = client.post("/api/tunnels", json={})
    assert response.status_code == 401


def test_login_uses_unified_message_and_throttle(web):
    client, _, _, _ = web
    for _ in range(2):
        response = client.post(
            "/login",
            data={"access_key": "wrong", "next": "/apply"},
            follow_redirects=False,
        )
        assert response.status_code == 401
        assert "访问密钥无效，或登录尝试过于频繁" in response.text

    # 达到失败阈值后，即使密钥正确也统一拒绝。
    response = client.post(
        "/login",
        data={"access_key": "test-access-key", "next": "/apply"},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert "访问密钥无效，或登录尝试过于频繁" in response.text


def test_login_sets_cookie_and_apply_returns_command_prompt(web):
    client, service, ports, _ = web
    login(client)
    response = client.get("/apply")
    assert response.status_code == 200
    assert 'value="sshport"' in response.text
    assert 'name="server_user"' in response.text
    assert 'value="tunnel"' in response.text
    assert 'name="server_port"' in response.text
    assert SERVER_PORT_INPUT_RE.search(response.text).group(1) == "22"
    assert "不能填写 root" in response.text
    csrf = CSRF_RE.search(response.text).group(1)

    response = client.post(
        "/apply",
        data={
            "csrf_token": csrf,
            "remote_user": "sshport",
            "remote_port": "2222",
            "server_user": "ops",
            "server_port": "2222",
            "ttl_seconds": "60",
            "note": "web-smoke",
        },
        follow_redirects=False,
    )
    assert response.status_code == 201
    body = response.text
    assert "ssh -N -T" in body
    assert "web-smoke" in body
    assert "data-tunnel-id=" in body
    assert "data-poll-seconds=\"5\"" in body
    assert "服务器端口" in body
    assert "隧道服务器的 2222 端口可达" in body
    assert "服务器账号 ops 的密码" in body
    assert "ops@tunnel.example.com" in body
    assert "-p 2222 " in body
    assert "不探测远程电脑" in body
    assert "第一步：在远程 Windows 创建登录用户" in body
    assert "第二步：在远程 Windows 建立隧道" in body
    assert "New-LocalUser -Name $name" in body
    assert "Set-LocalUser -Name $name -Password $password" in body
    assert "Add-LocalGroupMember" in body
    assert "useradd" not in body
    assert "chpasswd" not in body
    assert "远程电脑登录密码：" in body
    password = PASSWORD_RE.search(body)
    assert password, "result page has no generated remote password"
    assert len(password.group(1)) == 24
    ssh_command = SSH_COMMAND_RE.search(body)
    assert ssh_command, "result page has no ssh command"
    assert password.group(1) not in ssh_command.group(1)

    tunnel_id = TUNNEL_RE.search(body).group(1)
    status = client.get(f"/api/tunnels/{tunnel_id}/status").json()
    port = status["port"]
    assert status["state"] == "pending"

    # 同一来源短时间重复申请返回原隧道，不占新端口。
    response = client.post(
        "/apply",
        data={
            "csrf_token": csrf,
            "remote_user": "sshport",
            "remote_port": "2222",
            "server_user": "ops",
            "server_port": "2222",
            "ttl_seconds": "60",
            "note": "again",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert TUNNEL_RE.search(response.text).group(1) == tunnel_id
    assert "未重复分配端口" in response.text

    ports.set_listening(port)
    established = client.get(f"/api/tunnels/{tunnel_id}/status").json()
    assert established["state"] == "established"

    ports.set_closed(port)
    expired = client.get(f"/api/tunnels/{tunnel_id}/status").json()
    assert expired["state"] == "expired"


def test_apply_validation_and_csrf(web):
    client, _, _, _ = web
    login(client)
    csrf = csrf_from(client, "/apply")

    response = client.post(
        "/apply",
        data={"csrf_token": "", "remote_user": "sshport", "ttl_seconds": "60"},
    )
    assert response.status_code == 403

    response = client.post(
        "/apply",
        data={
            "csrf_token": csrf,
            "remote_user": "",
            "remote_port": "not-a-port",
            "ttl_seconds": "999",
            "note": "",
        },
    )
    assert response.status_code == 400
    assert "远程 SSH 端口必须是整数" in response.text
    assert "远程 SSH 用户名不能为空" not in response.text

    # 端口修好后，其余字段错误一次性返回，便于逐项修正。
    response = client.post(
        "/apply",
        data={
            "csrf_token": csrf,
            "remote_user": "",
            "remote_port": "22",
            "ttl_seconds": "999",
            "note": "",
        },
    )
    assert response.status_code == 400
    assert "远程 SSH 用户名不能为空" in response.text
    assert "所选有效期不在允许的档位中" in response.text

    response = client.post(
        "/apply",
        data={
            "csrf_token": csrf,
            "remote_user": "root",
            "remote_port": "22",
            "ttl_seconds": "60",
            "note": "",
        },
    )
    assert response.status_code == 400
    assert "不能使用 root" in response.text

    response = client.post(
        "/apply",
        data={
            "csrf_token": csrf,
            "remote_user": "sshport",
            "remote_port": "22",
            "server_user": "",
            "ttl_seconds": "60",
            "note": "",
        },
    )
    assert response.status_code == 400
    assert "连接服务器的 SSH 用户名不能为空" in response.text

    response = client.post(
        "/apply",
        data={
            "csrf_token": csrf,
            "remote_user": "sshport",
            "remote_port": "22",
            "server_user": "tunnel",
            "server_port": "not-a-port",
            "ttl_seconds": "60",
            "note": "",
        },
    )
    assert response.status_code == 400
    assert "连接服务器的 SSH 端口必须是整数" in response.text


def test_api_create_status_and_delete(web):
    client, service, ports, cleaner = web
    login(client)
    csrf = csrf_from(client, "/apply")

    response = client.post(
        "/api/tunnels",
        json={
            "remote_user": "sshport",
            "remote_port": 22,
            "server_user": "ops",
            "server_port": 2222,
            "ttl_seconds": 60,
            "note": "api",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201
    payload = response.json()
    tunnel_id = payload["id"]
    port = payload["port"]
    assert payload["status"] == "pending"
    assert payload["server_user"] == "ops"
    assert payload["server_port"] == 2222
    assert "ops@tunnel.example.com" in payload["ssh_command"]
    assert "-p 2222 " in payload["ssh_command"]
    assert payload["listen_address"] == "127.0.0.1"
    assert len(payload["remote_password"]) == 24
    assert "New-LocalUser -Name $name" in payload["remote_setup_command"]
    assert "useradd" not in payload["remote_setup_command"]
    assert payload["remote_password"] not in payload["ssh_command"]
    assert "PreferredAuthentications=password" in payload["ssh_command"]
    assert payload["remote_password"] in payload["agent_prompt"]

    response = client.post(
        "/api/tunnels",
        json={
            "remote_user": "sshport",
            "server_user": "bad user",
            "ttl_seconds": 60,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert "server_user" in response.json()["error"]["fields"]

    response = client.post(
        "/api/tunnels",
        json={
            "remote_user": "sshport",
            "server_port": "2222",
            "ttl_seconds": 60,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert "server_port" in response.json()["error"]["fields"]

    response = client.post(
        "/api/tunnels",
        json={"remote_user": "sshport", "ttl_seconds": 60},
        headers={"X-CSRF-Token": "wrong"},
    )
    assert response.status_code == 403

    ports.set_listening(port)
    response = client.get(f"/api/tunnels/{tunnel_id}/status")
    assert response.status_code == 200
    assert response.json()["state"] == "established"

    response = client.delete(
        f"/api/tunnels/{tunnel_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert port in ports.listening_ports()

    response = client.delete(
        f"/api/tunnels/{tunnel_id}",
        headers={"X-CSRF-Token": csrf, "X-Confirm-Delete": "yes"},
    )
    assert response.status_code == 200
    assert response.json() == {"deleted": True, "port": port}
    assert cleaner.calls[-1][0] == port
    assert port not in ports.listening_ports()
    assert client.get(f"/api/tunnels/{tunnel_id}/status").status_code == 404


def test_admin_policy_and_confirmed_delete(web):
    client, service, ports, cleaner = web
    login(client)
    csrf = csrf_from(client, "/admin")

    # 通过服务层使用不同来源创建旧隧道，避开 Web 同源去重，并保留旧策略。
    original = asyncio.run(
        service.create_tunnel(
            remote_user="sshport",
            remote_port=22,
            ttl_seconds=60,
            note="before-policy",
            source="policy-test-old-source",
        )
    )
    original_payload = service.application_payload(original)
    assert original_payload["listen_address"] == "127.0.0.1"

    response = client.post(
        "/admin/policy",
        data={"csrf_token": csrf, "listen_mode": "all"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert service.policy.mode.value == "all"
    admin = client.get("/admin")
    assert "全部地址" in admin.text
    assert "风险" in admin.text
    assert 'name="confirm" value="yes"' in admin.text
    assert "服务器账号 tunnel · SSH 端口 22" in admin.text

    response = client.post(
        "/admin/policy",
        data={"csrf_token": csrf, "listen_mode": "specific", "specific_ip": "10.9.9.9"},
    )
    assert response.status_code == 400
    assert "不在宿主机任何网卡上" in response.text
    assert service.policy.mode.value == "all"

    response = client.post(
        "/admin/policy",
        data={"csrf_token": csrf, "listen_mode": "specific", "specific_ip": "10.0.0.5"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert service.policy.mode.value == "specific"
    assert service.policy.specific_ip == "10.0.0.5"

    # 新申请使用新策略；旧记录保持原策略。
    created = client.post(
        "/api/tunnels",
        json={"remote_user": "sshport", "ttl_seconds": 60, "note": "after-policy"},
        headers={"X-CSRF-Token": csrf},
    ).json()
    assert created["listen_address"] == "10.0.0.5"
    assert service._records[original.record.id].bind_address == "127.0.0.1"

    tunnel_id = created["id"]
    port = created["port"]
    ports.set_listening(port)
    response = client.post(
        f"/admin/tunnels/{tunnel_id}/delete",
        data={"csrf_token": csrf},
    )
    assert response.status_code == 400
    assert "二次确认" in response.text
    assert port in ports.listening_ports()

    response = client.post(
        f"/admin/tunnels/{tunnel_id}/delete",
        data={"csrf_token": csrf, "confirm": "yes"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin?deleted=1"
    assert port not in ports.listening_ports()
    assert cleaner.calls[-1][0] == port


def test_health_and_static_are_public(web):
    client, _, _, _ = web
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_static_security_headers(web):
    client, _, _, _ = web
    response = client.get("/login")
    headers = response.headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
