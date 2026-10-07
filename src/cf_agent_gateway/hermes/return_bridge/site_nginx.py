"""Conservative increment for the existing Gateway Nginx server, never a template.

Only complete, directly visible server configuration is accepted. Unknown method
gates, includes or URI rewriting require operator review of the actual source.
Nginx's parser remains authoritative: the deployment entry must run nginx -t on
the complete candidate configuration before applying this plan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from cf_agent_gateway.hermes.return_bridge.enablement import (
    Change,
    EnablementError,
    Plan,
    _path,
    _read,
)

PREFIX = "/internal/hermes/returns/"
SOURCE_SELECTOR = ("^~", "/internal/hermes/inbound-bindings/")
PATH_EXPRESSION = r"^/internal/hermes/returns/[1-9][0-9]*/artifacts/[0-7]$"
ROUTE_BEGIN = "# CF_ARTIFACT_RETURN_ROUTE_V1_BEGIN"
ROUTE_END = "# CF_ARTIFACT_RETURN_ROUTE_V1_END"
MAP_BEGIN = "# CF_ARTIFACT_RETURN_METHOD_V1_BEGIN"
MAP_END = "# CF_ARTIFACT_RETURN_METHOD_V1_END"


@dataclass(frozen=True)
class Token:
    value: str
    start: int
    end: int


@dataclass(frozen=True)
class Directive:
    name: str
    arguments: tuple[str, ...]
    start: int
    end: int
    opening: int | None = None
    closing: int | None = None
    children: tuple[Directive, ...] = ()


def _tokens(source):
    result = []
    cursor = 0
    while cursor < len(source):
        character = source[cursor]
        if character.isspace():
            cursor += 1
            continue
        if character == "#":
            cursor = source.find("\n", cursor)
            if cursor < 0:
                break
            continue
        begin = cursor
        if character in "{};":
            result.append(Token(character, cursor, cursor + 1))
            cursor += 1
            continue
        value = []
        quoted = None
        while cursor < len(source):
            character = source[cursor]
            if quoted:
                if character == quoted:
                    quoted = None
                elif character == "\\":
                    cursor += 1
                    if cursor >= len(source):
                        raise EnablementError("nginx_unterminated_escape")
                    value.append(source[cursor])
                else:
                    value.append(character)
            elif character in "\"'":
                quoted = character
            elif character == "$" and source[cursor : cursor + 2] == "${":
                end = source.find("}", cursor + 2)
                if end < 0:
                    raise EnablementError("nginx_unterminated_variable")
                value.append(source[cursor : end + 1])
                cursor = end
            elif character == "\\":
                cursor += 1
                if cursor >= len(source):
                    raise EnablementError("nginx_unterminated_escape")
                value.append(source[cursor])
            elif character.isspace() or character in "{};#":
                break
            else:
                value.append(character)
            cursor += 1
        if quoted:
            raise EnablementError("nginx_unterminated_quote")
        if cursor == begin:
            raise EnablementError("nginx_invalid_token")
        result.append(Token("".join(value), begin, cursor))
    return result


def _parse(source):
    tokens = _tokens(source)
    cursor = 0

    def sequence(nested=False):
        nonlocal cursor
        result = []
        while cursor < len(tokens):
            if tokens[cursor].value == "}":
                if not nested:
                    raise EnablementError("nginx_unmatched_block")
                closing = tokens[cursor]
                cursor += 1
                return tuple(result), closing
            words = []
            while cursor < len(tokens) and tokens[cursor].value not in {"{", "}", ";"}:
                words.append(tokens[cursor])
                cursor += 1
            if not words or cursor == len(tokens) or tokens[cursor].value == "}":
                raise EnablementError("nginx_incomplete_directive")
            marker = tokens[cursor]
            cursor += 1
            if marker.value == ";":
                result.append(
                    Directive(
                        words[0].value,
                        tuple(w.value for w in words[1:]),
                        words[0].start,
                        marker.end,
                    )
                )
            else:
                children, closing = sequence(True)
                result.append(
                    Directive(
                        words[0].value,
                        tuple(w.value for w in words[1:]),
                        words[0].start,
                        closing.end,
                        marker.start,
                        closing.start,
                        children,
                    )
                )
        if nested:
            raise EnablementError("nginx_unterminated_block")
        return tuple(result), None

    return sequence()[0]


def _line_start(source, offset):
    begin = source.rfind("\n", 0, offset) + 1
    if source[begin:offset].strip():
        raise EnablementError("nginx_server_requires_multiline_boundaries")
    return begin


def _named(nodes, name):
    return [node for node in nodes if node.name == name]


def _listen(node, port):
    matched = False
    listeners = _named(node.children, "listen")
    for listen in listeners:
        if not listen.arguments:
            raise EnablementError("nginx_listen_requires_review")
        endpoint = listen.arguments[0]
        if endpoint == str(port) or endpoint.endswith(":" + str(port)):
            if "ssl" not in listen.arguments:
                raise EnablementError("nginx_existing_tls_listener_required")
            matched = True
    if matched and any("ssl" not in listen.arguments for listen in listeners):
        # A location belongs to the whole server block, not only the selected
        # port. Never expose the newly authenticated route via a plaintext alias.
        raise EnablementError("nginx_plaintext_alias_listener_requires_review")
    return matched


def _simple_gate(source, node):
    condition = source[node.start + len("if") : node.opening].strip()
    found = re.fullmatch(r"\(\s*\$request_method\s+!~\s+\^\(([A-Z|]+)\)\$\s*\)", condition)
    if not found:
        raise EnablementError("nginx_method_map_or_complex_if_requires_review")
    methods = found.group(1).split("|")
    allowed = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}
    if len(set(methods)) != len(methods) or set(methods) - allowed:
        raise EnablementError("nginx_method_gate_requires_review")
    if (
        len(node.children) != 1
        or node.children[0].name != "return"
        or node.children[0].arguments != ("405",)
    ):
        raise EnablementError("nginx_method_gate_requires_405_only")
    return tuple(methods)


def _reject_inherited(nodes):
    for node in nodes:
        if node.name in {
            "include",
            "rewrite",
            "return",
            "error_page",
            "try_files",
            "proxy_method",
            "proxy_set_body",
            "auth_request",
            "satisfy",
        }:
            raise EnablementError("nginx_inherited_directives_require_complete_review")
        if node.name == "error_log" and "debug" in node.arguments:
            raise EnablementError("nginx_debug_logging_may_expose_authorization")


def _reject_sensitive_log_formats(nodes):
    """Screen explicit sensitive variables; this is not a module/alias taint audit.

    A server rewrite may return before location selection, so the new location's
    access_log off cannot protect a method/path rejection in an inherited log.
    Keep the original format untouched and stop planning even if the format is
    currently unused. Arbitrary Lua/njs/custom variable implementations still
    require review of the complete effective deployment configuration.
    """
    raw = {
        "request_body",
        "request_body_file",
        "request_headers",
        "http_headers",
        "raw_request",
        "raw_request_header",
        "raw_request_headers",
        "http_cookie",
    }
    sensitive_fragments = ("authorization", "capability", "api_key", "auth_token", "access_token")
    for node in nodes:
        if node.name == "log_format":
            # Parse variables in decoded quoted/unquoted arguments, including
            # ${name}; comments and the format label are not log payloads.
            values = re.findall(
                r"\$\{([A-Za-z0-9_]+)\}|\$([A-Za-z0-9_]+)", " ".join(node.arguments[1:])
            )
            names = [(braced or plain).lower() for braced, plain in values]
            if any(
                name in raw
                or name.startswith("http_x_cf_return_")
                or any(fragment in name for fragment in sensitive_fragments)
                for name in names
            ):
                raise EnablementError("nginx_sensitive_log_format_requires_review")
        _reject_sensitive_log_formats(node.children)


def _upstream_and_headers(source, server, inherited):
    locations = _named(server.children, "location")
    matches = [loc for loc in locations if loc.arguments == SOURCE_SELECTOR]
    if len(matches) != 1:
        raise EnablementError("nginx_existing_inbound_bindings_location_required")
    selected = matches[0]
    if any(_named(loc.children, "location") for loc in locations):
        raise EnablementError("nginx_nested_locations_require_review")
    if any(
        node.name
        in {
            "include",
            "rewrite",
            "return",
            "try_files",
            "auth_basic",
            "auth_request",
            "satisfy",
            "if",
            "limit_except",
        }
        for node in selected.children
    ):
        raise EnablementError("nginx_source_location_policy_requires_review")
    known_source = {
        "proxy_pass",
        "proxy_set_header",
        "allow",
        "deny",
        "client_max_body_size",
        "client_body_buffer_size",
        "proxy_http_version",
        "proxy_request_buffering",
        "proxy_buffering",
        "proxy_connect_timeout",
        "proxy_read_timeout",
        "proxy_send_timeout",
        "proxy_next_upstream",
        "proxy_cache",
        "proxy_cache_bypass",
        "proxy_no_cache",
        "proxy_max_temp_file_size",
        "proxy_intercept_errors",
        "proxy_redirect",
        "proxy_pass_request_body",
        "proxy_pass_request_headers",
        "access_log",
    }
    if any(node.name not in known_source for node in selected.children):
        # Do not silently drop a per-location auth module, rate limit, variable
        # override or resolver which the return route would otherwise bypass.
        raise EnablementError("nginx_unknown_source_location_directive_requires_review")
    proxies = _named(selected.children, "proxy_pass")
    if len(proxies) != 1 or len(proxies[0].arguments) != 1:
        raise EnablementError("nginx_single_existing_gateway_proxy_required")
    upstream = proxies[0].arguments[0]
    matched = re.fullmatch(
        r"http://(\$[A-Za-z_][A-Za-z0-9_]*|[A-Za-z0-9_.-]+(?::[0-9]+)?)", upstream
    )
    if not matched:
        raise EnablementError("nginx_gateway_proxy_uri_or_scheme_requires_review")
    authority = matched.group(1)
    if authority.startswith("$"):
        definitions = [
            node
            for node in server.children
            if node.name == "set" and node.arguments and node.arguments[0] == authority
        ]
        if (
            len(definitions) != 1
            or len(definitions[0].arguments) != 2
            or not re.fullmatch(r"[A-Za-z0-9_.-]+(?::[0-9]+)?", definitions[0].arguments[1])
        ):
            raise EnablementError("nginx_gateway_upstream_requires_literal_server_set")
    effective = next(
        (
            _named(nodes, "proxy_set_header")
            for nodes in (selected.children, server.children, inherited)
            if _named(nodes, "proxy_set_header")
        ),
        [],
    )
    copied = []
    standard = {
        "host",
        "connection",
        "x-real-ip",
        "x-forwarded-for",
        "x-forwarded-proto",
        "x-request-id",
        "authorization",
        "content-type",
        "content-length",
    }
    for node in effective:
        if len(node.arguments) != 2 or node.arguments[0].lower() not in standard:
            raise EnablementError("nginx_proxy_header_policy_requires_review")
        name, value = node.arguments
        if name.lower() == "authorization" and value != "$http_authorization":
            raise EnablementError("nginx_proxy_auth_override_requires_review")
        if name.lower() not in {"authorization", "content-length", "content-type", "connection"}:
            copied.append(source[node.start : node.end])
    if not any(node.arguments[0].lower() == "host" for node in effective):
        copied.append("proxy_set_header Host $proxy_host;")
    copied.extend(
        source[node.start : node.end]
        for node in selected.children
        if node.name in {"allow", "deny"}
    )
    return upstream, copied


def _route(upstream, copied, indent, newline):
    inner = indent + "    "
    body = [
        f'if ($request_uri !~ "{PATH_EXPRESSION}") {{ return 404; }}',
        "if ($request_method !~ ^(GET|PUT)$) { return 405; }",
        'if ($http_transfer_encoding != "") { return 400; }',
        "client_max_body_size 1m;",
        "client_body_buffer_size 1m;",
        "client_body_in_file_only off;",
        "proxy_http_version 1.1;",
        "proxy_request_buffering off;",
        "proxy_buffering off;",
        "proxy_max_temp_file_size 0;",
        "proxy_cache off;",
        "proxy_cache_bypass 1;",
        "proxy_no_cache 1;",
        "proxy_store off;",
        "proxy_next_upstream off;",
        "proxy_intercept_errors off;",
        "proxy_redirect off;",
        "proxy_pass_request_body on;",
        "proxy_pass_request_headers on;",
        'proxy_set_header Connection "";',
        "proxy_set_header Authorization $http_authorization;",
        "proxy_set_header Content-Length $content_length;",
        "proxy_set_header Content-Type $http_content_type;",
        *copied,
        "access_log off;",
        f"proxy_pass {upstream};",
    ]
    return newline.join(
        [
            indent + ROUTE_BEGIN,
            indent + "location ^~ " + PREFIX + " {",
            *(inner + line for line in body),
            indent + "}",
            indent + ROUTE_END,
        ]
    )


def _method_map(methods, variable, indent, newline):
    return newline.join(
        [
            indent + MAP_BEGIN,
            indent + f'map "$request_method:$request_uri" {variable} {{',
            indent + "    default 1;",
            indent + f"    ~^({'|'.join(methods)}): 0;",
            indent + "    ~^(GET|PUT):/internal/hermes/returns/[1-9][0-9]*/artifacts/[0-7]$ 0;",
            indent + "}",
            indent + MAP_END,
        ]
    )


def _marked(source, beginning, ending):
    if source.count(beginning) != 1 or source.count(ending) != 1:
        raise EnablementError("nginx_managed_markers_conflict")
    start = _line_start(source, source.index(beginning))
    end = source.index(ending) + len(ending)
    return start, end, source[start:end]


def plan_nginx(config_path: str | Path, *, listen_port: int = 8443) -> Plan:
    """Read complete local source and return a private-byte Plan for the shared journal.

    Preserve all other locations, limits, TLS, upstream variables and default deny.
    No production endpoint, process, secret or certificate is accessed here.
    """
    if type(listen_port) is not int or not 1 <= listen_port <= 65535:
        raise EnablementError("nginx_invalid_listener_port")
    path = _path(config_path)
    before = _read(path)
    if before is None:
        raise EnablementError("nginx_complete_existing_configuration_required")
    try:
        source = before.decode("utf-8")
    except UnicodeError:
        raise EnablementError("nginx_utf8_configuration_required") from None
    roots = _parse(source)
    _reject_sensitive_log_formats(roots)
    http = _named(roots, "http")
    if len(http) > 1:
        raise EnablementError("nginx_http_context_ambiguous")
    nodes = http[0].children if http else roots
    inherited = tuple(node for node in nodes if node.name != "server")
    _reject_inherited(inherited)
    if http:
        _reject_inherited(tuple(node for node in roots if node.name != "http"))
    servers = [node for node in _named(nodes, "server") if _listen(node, listen_port)]
    if len(servers) != 1:
        raise EnablementError("nginx_exactly_one_matching_tls_server_required")
    server = servers[0]
    _reject_inherited(tuple(node for node in server.children if node.name != "location"))
    upstream, copied = _upstream_and_headers(source, server, inherited)
    newline = "\r\n" if "\r\n" in source else "\n"
    server_start = _line_start(source, server.start)
    indent = source[server_start : server.start]
    close_start = _line_start(source, server.closing)
    route = _route(upstream, copied, indent + "    ", newline)
    variable = f"$cf_artifact_return_v1_{listen_port}_method_denied"
    gates = _named(server.children, "if")
    if len(gates) > 1:
        raise EnablementError("nginx_multiple_server_gates_require_review")
    mutations = []
    managed = ROUTE_BEGIN in source or ROUTE_END in source
    existing = [
        loc
        for loc in _named(server.children, "location")
        if any(PREFIX.rstrip("/") in part for part in loc.arguments)
    ]
    if managed:
        _, _, rendered = _marked(source, ROUTE_BEGIN, ROUTE_END)
        if len(existing) != 1 or existing[0].arguments != ("^~", PREFIX) or rendered != route:
            raise EnablementError("nginx_managed_return_route_changed")
    else:
        if existing or MAP_BEGIN in source or variable in source:
            raise EnablementError("nginx_existing_return_route_requires_review")
        mutations.append((close_start, close_start, route + newline))
    if gates:
        gate = gates[0]
        condition = source[gate.start : gate.opening].strip()
        if managed and condition == f"if ({variable})":
            maps = [
                node
                for node in _named(nodes, "map")
                if node.arguments == ("$request_method:$request_uri", variable)
            ]
            if len(maps) != 1 or len(maps[0].children) != 3:
                raise EnablementError("nginx_managed_method_gate_changed")
            allowed = re.fullmatch(r"~\^\(([A-Z|]+)\):", maps[0].children[1].name)
            if not allowed:
                raise EnablementError("nginx_managed_method_gate_changed")
            expected = _method_map(tuple(allowed.group(1).split("|")), variable, indent, newline)
            if _marked(source, MAP_BEGIN, MAP_END)[2] != expected:
                raise EnablementError("nginx_managed_method_gate_changed")
            if (
                len(gate.children) != 1
                or gate.children[0].name != "return"
                or gate.children[0].arguments != ("405",)
            ):
                raise EnablementError("nginx_managed_method_gate_changed")
        elif not managed:
            methods = _simple_gate(source, gate)
            replacement = f"if ({variable}) {{ return 405; }}"
            mutations.append((gate.start, gate.end, replacement))
            generated = _method_map(methods, variable, indent, newline)
            mutations.append((server_start, server_start, generated + newline))
        else:
            raise EnablementError("nginx_managed_method_gate_changed")
    elif MAP_BEGIN in source or MAP_END in source:
        raise EnablementError("nginx_managed_method_gate_changed")
    for start, end, replacement in sorted(mutations, reverse=True):
        source = source[:start] + replacement + source[end:]
    after = source.encode("utf-8")
    _parse(source)
    return Plan(
        "nginx",
        (Change(path, ("nginx.return_location", "nginx.narrow_method_gate"), before, after),),
        (
            {
                "kind": "nginx_complete_candidate_validation",
                "required": "nginx -t",
                "instruction": "validate full effective candidate including unchanged TLS/includes",
                "source": "https://nginx.org/en/docs/http/ngx_http_rewrite_module.html",
            },
            {
                "kind": "nginx_artifact_return_route",
                "listen_port": listen_port,
                "route": PREFIX,
                "max_bytes": 1_048_576,
                "methods": ["GET", "PUT"],
                "source_selector": " ".join(SOURCE_SELECTOR),
            },
            {
                "kind": "nginx_log_format_screen",
                "scope": "explicit sensitive authorization/header/body variables only",
                "not_verified": "arbitrary module or indirect variable logging",
            },
            {
                "kind": "non_root_request_buffering",
                "request_buffering": "off",
                "in_memory_body_limit": 1_048_576,
                "new_writable_business_mount_required": False,
                "source": "https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_request_buffering",
            },
        ),
    )
