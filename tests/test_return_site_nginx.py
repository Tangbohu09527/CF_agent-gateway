"""Increment-only planning and real Nginx -> Gateway tests, synthetic inputs only.

CI sets CF_RETURN_REQUIRE_NGINX=1 and runs as a non-root user. Local machines
without Nginx skip the real process test; unit tests do not claim Nginx execution.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

import httpx
import pytest
from test_artifact_return_http import artifact_count, headers, path
from test_artifact_return_http import rig as rig  # Reuse real auth/claim/SQLite fixture.
from test_hermes_tls import _test_certificates

from cf_agent_gateway.artifact import ArtifactRepository
from cf_agent_gateway.hermes.return_bridge.enablement import EnablementError
from cf_agent_gateway.hermes.return_bridge.site_nginx import (
    MAP_BEGIN,
    MAP_END,
    ROUTE_BEGIN,
    ROUTE_END,
    plan_nginx,
)
from cf_agent_gateway.hermes.tls import verified_ssl_context

FIXTURE = Path(__file__).parent / "fixtures/return_site/nginx-complete-synthetic.conf"


def _source(tmp_path, text=None):
    target = tmp_path / "existing.conf"
    target.write_bytes(FIXTURE.read_bytes() if text is None else text.encode())
    return target


def test_increment_preserves_entire_original_except_narrow_gate(tmp_path):
    original = FIXTURE.read_text()
    target = _source(tmp_path, original)
    plan = plan_nginx(target)
    assert target.read_text() == original  # planning is read-only
    assert len(plan.changes) == 1
    after = plan.changes[0].after.decode()
    assert "client_max_body_size 16k;" in after
    assert "client_max_body_size 1m;" in after
    assert "proxy_request_buffering off;" in after
    assert "proxy_next_upstream off;" in after
    assert "proxy_set_header Authorization $http_authorization;" in after
    assert "proxy_set_header Content-Length $content_length;" in after
    assert "proxy_pass http://$gateway_upstream;" in after
    assert "~^(GET|POST): 0;" in after
    assert "~^(GET|PUT):/internal/hermes/returns/[1-9][0-9]*/artifacts/[0-7]$ 0;" in after
    # Exact original bytes recovered by removing managed inserts and restoring
    # the sole known gate. This tests comments, ordering, limits and other routes.
    restored = after
    for begin, end in ((ROUTE_BEGIN, ROUTE_END), (MAP_BEGIN, MAP_END)):
        first = restored.rfind("\n", 0, restored.index(begin)) + 1
        last = restored.index(end) + len(end) + 1
        restored = restored[:first] + restored[last:]
    restored = restored.replace(
        "if ($cf_artifact_return_v1_8443_method_denied) { return 405; }",
        "if ($request_method !~ ^(GET|POST)$) { return 405; }",
    )
    assert restored == original
    assert not plan.requirements[-1]["new_writable_business_mount_required"]
    # No private configuration bytes in public plan/repr.
    assert "gateway:8000" not in json.dumps(plan.summary())
    assert "gateway:8000" not in repr(plan)


def test_second_plan_is_byte_identical_and_line_endings_preserved(tmp_path):
    target = _source(tmp_path, FIXTURE.read_text().replace("\n", "\r\n"))
    first = plan_nginx(target)
    target.write_bytes(first.changes[0].after)
    second = plan_nginx(target)
    assert second.changes[0].before == second.changes[0].after
    assert first.changes[0].after.count(b"\n") == first.changes[0].after.count(b"\r\n")


def test_server_fragment_without_method_gate_needs_no_global_map(tmp_path):
    source = FIXTURE.read_text()
    start = source.index("    server {")
    source = source[start : source.rfind("}\n")]
    source = source.replace("        if ($request_method !~ ^(GET|POST)$) { return 405; }\n", "")
    target = _source(tmp_path, source)
    plan = plan_nginx(target)
    assert MAP_BEGIN not in plan.changes[0].after.decode()
    target.write_bytes(plan.changes[0].after)
    assert plan_nginx(target).changes[0].after == target.read_bytes()


def test_quotes_and_comment_braces_do_not_forge_locations(tmp_path):
    source = FIXTURE.read_text().replace(
        "server_name localhost;",
        'server_name "localhost"; # location ^~ /internal/hermes/returns/ {\n'
        '        add_header X-Synthetic "quoted # {} ; value";',
    )
    assert (
        b'add_header X-Synthetic "quoted # {} ; value";'
        in plan_nginx(_source(tmp_path, source)).changes[0].after
    )


@pytest.mark.parametrize(
    "variable",
    [
        "$http_authorization",
        "${http_x_cf_return_authorization}",
        "$http_x_cf_return_capability",
        "$http_proxy_authorization",
        "$http_x_api_key",
        "$request_body",
        "${request_body_file}",
        "$raw_request_headers",
        "$http_cookie",
    ],
)
def test_sensitive_log_format_refused_before_server_method_gate_can_log(tmp_path, variable):
    source = (
        FIXTURE.read_text()
        .replace("http {\n", f"http {{\n    log_format original escape=json '\"{variable}\"';\n")
        .replace("access_log off;", "access_log /synthetic/original.log original;", 1)
    )
    target = _source(tmp_path, source)
    with pytest.raises(EnablementError, match="sensitive_log_format_requires_review"):
        plan_nginx(target)
    assert target.read_bytes() == source.encode()


def test_normal_combined_log_format_and_sensitive_comment_are_preserved(tmp_path):
    formatting = (
        "    # A comment mentioning $http_authorization is not a log format.\n"
        "    log_format original '$remote_addr - $remote_user [$time_local] '\n"
        '        \'"$request" $status $body_bytes_sent "$http_referer" "$http_user_agent"\';\n'
    )
    source = FIXTURE.read_text().replace("http {\n", "http {\n" + formatting)
    result = plan_nginx(_source(tmp_path, source))
    assert formatting in result.changes[0].after.decode()
    screen = next(r for r in result.requirements if r["kind"] == "nginx_log_format_screen")
    assert screen["not_verified"] == "arbitrary module or indirect variable logging"


@pytest.mark.parametrize(
    ("old", "replacement", "code"),
    [
        ("listen 8443 ssl default_server;", "listen 8443;", "existing_tls_listener"),
        (
            "listen 8443 ssl default_server;",
            "listen 8443 ssl; listen 8443;",
            "existing_tls_listener",
        ),
        (
            "listen 8443 ssl default_server;",
            "listen 8443 ssl; listen 8080;",
            "plaintext_alias_listener",
        ),
        ("listen 8443 ssl default_server;", "listen 9443 ssl;", "exactly_one_matching"),
        ("if ($request_method !~ ^(GET|POST)$)", "if ($method_allowed = 0)", "complex_if"),
        ("return 405;", "return 403;", "requires_405_only"),
        ("client_max_body_size 16k;", "include /unread/rules.conf;", "complete_review"),
        ("client_max_body_size 16k;", "rewrite ^ /different break;", "complete_review"),
        ("client_max_body_size 16k;", "error_log /tmp/debug.log debug;", "debug_logging"),
        (
            "^~ /internal/hermes/inbound-bindings/",
            "/internal/hermes/inbound-bindings/",
            "location_required",
        ),
        ("http://$gateway_upstream;", "http://$gateway_upstream/;", "uri_or_scheme"),
        (
            "set $gateway_upstream gateway:8000;",
            "set $gateway_upstream $user_input;",
            "literal_server_set",
        ),
        (
            "Authorization $http_authorization;",
            'Authorization "Bearer synthetic-static";',
            "auth_override",
        ),
        ("proxy_set_header Host $host;", "proxy_set_header X-Unknown $value;", "header_policy"),
        ("proxy_set_header Host $host;", "auth_request /identity;", "source_location_policy"),
        (
            "proxy_set_header Host $host;",
            "set $gateway_upstream alternate:8000;",
            "unknown_source_location",
        ),
        (
            "proxy_set_header Host $host;",
            "access_by_lua_block { verify(); }",
            "unknown_source_location",
        ),
        (
            "location / { return 404; }",
            "location /internal/hermes/returns/ { return 404; }",
            "existing_return_route",
        ),
    ],
)
def test_unknown_or_unsafe_shapes_fail_closed_without_writing(tmp_path, old, replacement, code):
    source = FIXTURE.read_text().replace(old, replacement)
    target = _source(tmp_path, source)
    with pytest.raises(EnablementError, match=code):
        plan_nginx(target)
    assert target.read_text() == source


@pytest.mark.parametrize("needle", ["proxy_cache off;", "default 1;", "return 405;"])
def test_managed_config_tampering_is_rejected(tmp_path, needle):
    target = _source(tmp_path)
    original = plan_nginx(target).changes[0].after.decode()
    target.write_text(original.replace(needle, needle + " # operator modification", 1))
    with pytest.raises(EnablementError):
        plan_nginx(target)


def test_managed_marker_deletion_is_rejected(tmp_path):
    target = _source(tmp_path)
    original = plan_nginx(target).changes[0].after.decode()
    target.write_text(original.replace(ROUTE_END, "# deleted marker"))
    with pytest.raises(EnablementError, match="markers_conflict"):
        plan_nginx(target)


def test_complex_map_method_gate_is_not_replaced_by_permissive_default(tmp_path):
    source = (
        FIXTURE.read_text()
        .replace(
            "http {",
            "http {\n    map $request_method $method_allowed { default 0; GET 1; POST 1; }",
        )
        .replace("if ($request_method !~ ^(GET|POST)$)", "if ($method_allowed = 0)")
    )
    with pytest.raises(EnablementError, match="complex_if"):
        plan_nginx(_source(tmp_path, source))


def test_other_server_and_unrelated_map_remain_identical(tmp_path):
    extra = """    map $http_host $unrelated { default keep; }
    server {
        listen 9443 ssl;
        location / { return 403; }
    }
"""
    source = FIXTURE.read_text().replace("http {\n", "http {\n" + extra)
    assert extra in plan_nginx(_source(tmp_path, source)).changes[0].after.decode()


@contextmanager
def _gateway_peer(app):
    import uvicorn

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = uvicorn.Server(
        uvicorn.Config(app, lifespan="off", access_log=False, log_level="error")
    )
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise AssertionError("isolated Gateway HTTP did not start")
            time.sleep(0.02)
        yield listener.getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive()


def _free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@pytest.fixture
def nginx_executable():
    executable = shutil.which("nginx")
    required = os.environ.get("CF_RETURN_REQUIRE_NGINX") == "1"
    if executable is None:
        if required:
            pytest.fail("CF_RETURN_REQUIRE_NGINX=1 requires an existing nginx executable")
        pytest.skip("Nginx unavailable locally; dedicated CI executes real non-root Nginx")
    if os.name != "posix" or os.geteuid() == 0:
        if required:
            pytest.fail("real Nginx acceptance must run as a non-root POSIX user")
        pytest.skip("real Nginx acceptance requires a non-root POSIX process")
    return executable


def test_real_nonroot_nginx_https_to_gateway_returns(nginx_executable, rig, tmp_path):
    executable = nginx_executable
    ca, certificate, key = _test_certificates(tmp_path)
    port = _free_port()
    with _gateway_peer(rig.app) as gateway_port:
        source = FIXTURE.read_text().replace("/tmp/cf-return-synthetic", str(tmp_path))
        source = source.replace("listen 8443", f"listen {port}")
        source = source.replace("/synthetic/server.pem", str(certificate))
        source = source.replace("/synthetic/server-key.pem", str(key))
        source = source.replace("gateway:8000", f"127.0.0.1:{gateway_port}")
        # Only this isolated fixture selects writable temporary directories.
        # Deployment planning adds no host mount or business storage directory.
        temporary_paths = "".join(
            f"    {name}_temp_path {tmp_path / name};\n"
            for name in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi")
        )
        source = source.replace("http {\n", "http {\n" + temporary_paths)
        target = _source(tmp_path, source)
        candidate = tmp_path / "candidate.conf"
        candidate.write_bytes(plan_nginx(target, listen_port=port).changes[0].after)
        command = [executable, "-p", str(tmp_path) + "/", "-c", str(candidate)]
        check = subprocess.run([*command, "-t"], capture_output=True, timeout=10)
        assert check.returncode == 0, check.stderr.decode(errors="replace")
        process = subprocess.Popen(
            [*command, "-g", "daemon off;"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        try:
            with httpx.Client(
                base_url=f"https://localhost:{port}",
                verify=verified_ssl_context(str(ca)),
                trust_env=False,
                follow_redirects=False,
                timeout=10,
            ) as client:
                deadline = time.monotonic() + 10
                while True:
                    assert process.poll() is None, "isolated nginx exited during startup"
                    try:
                        assert client.get("/wrong-route").status_code == 404
                        break
                    except httpx.ConnectError:
                        assert time.monotonic() < deadline
                        time.sleep(0.02)
                # Synthetic PDF bytes: no approved sample, model or live credential.
                content = b"%PDF-1.7\n" + b"synthetic-content\n" * 4800 + b"%%EOF\n"
                assert 16_384 < len(content) < 1_048_576
                response = client.put(path(rig), content=content, headers=headers(rig, content))
                assert response.status_code == 200, response.text
                receipt = response.json()
                assert receipt["status"] == "ready"
                assert receipt["size"] == len(content)
                assert receipt["sha256"] == hashlib.sha256(content).hexdigest()
                observed = client.get(
                    path(rig), headers={"Authorization": rig.context["authorization"]}
                )
                assert observed.status_code == 200 and observed.json() == receipt
                assert observed.headers["cache-control"] == "no-store"
                with rig.factory() as session:
                    stored = ArtifactRepository(session, rig.settings.artifact.storage_root)
                    assert stored.read(receipt["artifact_id"]) == content
                statuses = {}
                statuses["unauthorized"] = client.put(path(rig, 1), content=content).status_code
                statuses["wrong_capability"] = client.put(
                    path(rig, 1),
                    content=content,
                    headers={**headers(rig, content), "Authorization": "Bearer synthetic-wrong"},
                ).status_code
                oversized = b"x" * (1_048_576 + 1)
                statuses["oversize"] = client.put(
                    path(rig, 1),
                    content=oversized,
                    headers=headers(rig, oversized),
                ).status_code
                statuses["wrong_route"] = client.put(
                    path(rig) + "/extra",
                    content=content,
                    headers=headers(rig, content),
                ).status_code
                statuses["query_rejected"] = client.get(path(rig) + "?x=1").status_code
                statuses["post_rejected"] = client.post(path(rig), content=b"").status_code
                statuses["head_rejected"] = client.head(path(rig)).status_code
                statuses["old_put_rejected"] = client.put(
                    "/inbound-media/old", content=b""
                ).status_code
                statuses["old_16k_limit"] = client.post(
                    "/inbound-media/old", content=content
                ).status_code
                statuses["default_deny"] = client.get("/unchanged-default-deny").status_code
                assert statuses == {
                    "unauthorized": 403,
                    "wrong_capability": 403,
                    "oversize": 413,
                    "wrong_route": 405,
                    "query_rejected": 404,
                    "post_rejected": 405,
                    "head_rejected": 405,
                    "old_put_rejected": 405,
                    "old_16k_limit": 413,
                    "default_deny": 404,
                }
                assert artifact_count(rig) == 1
                assert not any((tmp_path / "client_body").rglob("*"))
                assert not any((tmp_path / "proxy").rglob("*"))
                evidence = {
                    "schema": "cf-return-nginx-isolated/v1",
                    "production": False,
                    "nginx": subprocess.run([executable, "-v"], capture_output=True, timeout=10)
                    .stderr.decode()
                    .strip(),
                    "nonroot": os.geteuid() != 0,
                    "tls_hostname": "localhost",
                    "uploaded_size": len(content),
                    "uploaded_sha256": hashlib.sha256(content).hexdigest(),
                    "ready_receipt": receipt,
                    "negative_statuses": statuses,
                }
                (tmp_path / "nginx-isolated-evidence.json").write_text(
                    json.dumps(evidence, indent=2)
                )
        finally:
            process.terminate()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)
