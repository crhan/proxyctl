"""Filtering helpers for ``proxyctl connections``.

The command gathers sockets in ``connections.py``; this module owns the naming
tables and matching rules for app, host, preset, agent, and free-text filters.
Keeping this separate prevents the collector from becoming a pile of special
cases.
"""

from __future__ import annotations

import re
from typing import Any

APP_CONTEXTS = [
    "codex_app",
    "codex_cli",
    "claude_app",
    "claude_cli",
    "chatgpt_app",
]
APP_CONTEXT_LABELS = {
    "codex_app": "Codex App",
    "codex_cli": "Codex CLI",
    "claude_app": "Claude App",
    "claude_cli": "Claude CLI",
    "chatgpt_app": "ChatGPT App",
}
DEFAULT_APP_FILTERS = [APP_CONTEXT_LABELS[name] for name in APP_CONTEXTS]
APP_FILTER_ALIASES = {
    "codex": ["codex_app", "codex_cli"],
    "codex app": ["codex_app"],
    "codex cli": ["codex_cli"],
    "claude": ["claude_app", "claude_cli"],
    "claude app": ["claude_app"],
    "claude cli": ["claude_cli"],
    "chatgpt": ["chatgpt_app"],
    "chatgpt app": ["chatgpt_app"],
}
APP_REMOTE_PATTERNS = {
    "codex_app": re.compile(r"openai|chatgpt|oaistatic|oaiusercontent", re.I),
    "codex_cli": re.compile(r"openai|chatgpt|oaistatic|oaiusercontent", re.I),
    "chatgpt_app": re.compile(r"openai|chatgpt|oaistatic|oaiusercontent", re.I),
    "claude_app": re.compile(r"anthropic|claude|mcp-proxy", re.I),
    "claude_cli": re.compile(r"anthropic|claude|mcp-proxy", re.I),
}
AGENT_PROFILES = {
    "codex": {
        "contexts": ["codex_app", "codex_cli"],
        "hosts": ["openai", "chatgpt", "oaistatic", "oaiusercontent"],
        "process": ["codex"],
    },
    "chatgpt": {
        "contexts": ["chatgpt_app"],
        "hosts": ["openai", "chatgpt", "oaistatic", "oaiusercontent"],
        "process": ["chatgpt"],
    },
    "openai": {
        "contexts": ["codex_app", "codex_cli", "chatgpt_app"],
        "hosts": ["openai", "chatgpt", "oaistatic", "oaiusercontent"],
        "process": ["codex", "chatgpt"],
    },
    "claude": {
        "contexts": ["claude_app", "claude_cli"],
        "hosts": ["anthropic", "claude", "mcp-proxy"],
        "process": ["claude"],
    },
    "anthropic": {
        "contexts": ["claude_app", "claude_cli"],
        "hosts": ["anthropic", "claude", "mcp-proxy"],
        "process": ["claude"],
    },
    "openclaw": {
        "contexts": [],
        "hosts": ["openclaw"],
        "process": ["openclaw"],
    },
}
PRESET_AGENTS = {
    "ai": ["openai", "anthropic"],
    "llm": ["openai", "anthropic"],
    "agent": ["codex", "claude", "openclaw"],
    "agents": ["codex", "claude", "openclaw"],
    "coding": ["codex", "claude", "openclaw"],
    "openai": ["openai"],
    "anthropic": ["anthropic"],
    "claude": ["claude"],
    "codex": ["codex"],
    "chatgpt": ["chatgpt"],
}
ROUTE_FILTER_ALIASES = {
    "proxy": "proxy",
    "direct": "direct",
    "reject": "reject",
    "unknown": "unknown",
    "代理": "proxy",
    "直连": "direct",
    "拒绝": "reject",
    "未知": "unknown",
}


def _normalize_app_filter(value: str) -> str:
    """Normalize a user-facing app filter for alias lookup."""
    return re.sub(r"[\s_-]+", " ", value.strip().lower())


def _normalize_filter_value(value: str) -> str:
    """Normalize a user-facing filter value for lookup and matching."""
    return re.sub(r"[\s_-]+", " ", value.strip().lower())


def _normalize_route_filter(value: str) -> str:
    """Normalize route filter aliases to Mihomo route_kind values."""
    return ROUTE_FILTER_ALIASES.get(_normalize_filter_value(value), "")


def _contexts_for_filter(value: str) -> list[str]:
    """Return canonical app contexts selected by one ``--app`` filter."""
    return APP_FILTER_ALIASES.get(_normalize_app_filter(value), [])


def _effective_contexts(filters: list[str]) -> list[str]:
    """Expand user filters to canonical app contexts, preserving priority."""
    contexts: list[str] = []
    for app_context in APP_CONTEXTS:
        if any(app_context in _contexts_for_filter(item) for item in filters):
            contexts.append(app_context)
    return contexts


def _detect_app_contexts(text: str) -> list[str]:
    """Detect known AI app/CLI contexts from process text."""
    lowered = text.lower()
    contexts: list[str] = []
    if "/applications/codex.app/" in lowered or "codex helper" in lowered:
        contexts.append("codex_app")
    elif re.search(r"(^|[/\s])codex(-aarch64|$|\s)", lowered):
        contexts.append("codex_cli")
    if "/applications/claude.app/" in lowered or "claude helper" in lowered:
        contexts.append("claude_app")
    elif re.search(r"(^|[/\s])claude($|[\s-])", lowered):
        contexts.append("claude_cli")
    if "/applications/chatgpt.app/" in lowered or "chatgpthelper" in lowered:
        contexts.append("chatgpt_app")
    return contexts


def _matches_app_text(text: str, filters: list[str]) -> bool:
    """Return whether process text matches requested app filters."""
    if not filters:
        return True
    detected = set(_detect_app_contexts(text))
    requested = set(_effective_contexts(filters))
    if requested and detected & requested:
        return True
    lowered = text.lower()
    return any(
        app_filter.lower() in lowered
        for app_filter in filters
        if not _contexts_for_filter(app_filter)
    )


def _remote_target_text(conn: dict[str, Any]) -> str:
    """Return searchable destination text for one Mihomo connection."""
    metadata = conn.get("metadata") or {}
    return " ".join([
        str(metadata.get("host") or ""),
        str(metadata.get("destinationIP") or ""),
    ])


def _remote_candidate_contexts(conn: dict[str, Any],
                               filters: list[str]) -> list[str]:
    """Infer possible app contexts from a remote destination."""
    requested = _effective_contexts(filters)
    candidates = requested or APP_CONTEXTS
    target = _remote_target_text(conn)
    return [
        app_context for app_context in candidates
        if APP_REMOTE_PATTERNS[app_context].search(target)
    ]


def _remote_candidate_contexts_for_args(conn: dict[str, Any],
                                        args: Any) -> list[str]:
    """Infer possible app contexts from a remote destination and filters."""
    requested = _requested_contexts(args)
    candidates = requested or APP_CONTEXTS
    target = _remote_target_text(conn)
    return [
        app_context for app_context in candidates
        if APP_REMOTE_PATTERNS[app_context].search(target)
    ]


def _merge_contexts(items: list[dict[str, Any]], field: str) -> list[str]:
    """Merge canonical app context lists while preserving display order."""
    seen = {
        app_context
        for item in items
        for app_context in item.get(field, [])
    }
    return [app_context for app_context in APP_CONTEXTS if app_context in seen]


def _agent_names(args: Any) -> list[str]:
    """Return agent names selected by --agent, --preset and known --app."""
    names: list[str] = []
    for preset in args.preset_filters:
        names.extend(PRESET_AGENTS.get(_normalize_filter_value(preset), []))
    names.extend(args.agent_filters)
    for app_filter in args.app_filters:
        normalized = _normalize_filter_value(app_filter)
        if normalized in AGENT_PROFILES or _contexts_for_filter(app_filter):
            names.append(app_filter)
    return names


def _requested_contexts(args: Any) -> list[str]:
    """Return app contexts requested by agent-like filters."""
    seen: set[str] = set()
    for app_filter in args.app_filters:
        seen.update(_contexts_for_filter(app_filter))
    for agent in _agent_names(args):
        profile = AGENT_PROFILES.get(_normalize_filter_value(agent), {})
        seen.update(profile.get("contexts", []))
    return [app_context for app_context in APP_CONTEXTS if app_context in seen]


def _agent_host_terms(args: Any) -> list[str]:
    """Return host substrings selected by agent-like filters."""
    terms: list[str] = []
    for agent in _agent_names(args):
        normalized = _normalize_filter_value(agent)
        profile = AGENT_PROFILES.get(normalized)
        terms.extend(profile.get("hosts", []) if profile else [normalized])
    return terms


def _agent_process_terms(args: Any) -> list[str]:
    """Return process substrings selected by agent-like filters."""
    terms: list[str] = list(args.app_filters)
    for agent in _agent_names(args):
        normalized = _normalize_filter_value(agent)
        profile = AGENT_PROFILES.get(normalized)
        terms.extend(profile.get("process", []) if profile else [agent])
    return terms


def _contains_any(text: str, terms: list[str]) -> bool:
    """Return whether ``text`` contains any non-empty term."""
    lowered = text.lower()
    return any(term.strip().lower() in lowered for term in terms if term.strip())


def _detail_text(detail: dict[str, Any] | None) -> str:
    """Return searchable text for one Mihomo detail block."""
    m = detail or {}
    return " ".join([
        str(m.get("host") or ""),
        str(m.get("destination_ip") or ""),
        str(m.get("rule") or ""),
        str(m.get("rule_payload") or ""),
        str(m.get("route_kind") or ""),
        " ".join(str(item) for item in (m.get("chains") or [])),
    ])


def _chain_text(detail: dict[str, Any] | None) -> str:
    """Return searchable chain text for one Mihomo detail block."""
    return " ".join(str(item) for item in ((detail or {}).get("chains") or []))


def _route_kind_text(detail: dict[str, Any] | None) -> str:
    """Return Mihomo route kind text for one detail block."""
    return str((detail or {}).get("route_kind") or "")


def _matches_filter_dimensions(process_text: str, target_text: str,
                               contexts: list[str], args: Any,
                               chain_text: str = "",
                               route_kind: str = "") -> bool:
    """Return whether a row satisfies all active filter dimensions."""
    if not args.has_filters():
        return True
    if args.host_filters and not _contains_any(target_text, args.host_filters):
        return False
    if args.chain_filters and not _contains_any(chain_text, args.chain_filters):
        return False
    if args.route_filters and not _matches_route_kind(route_kind, args.route_filters):
        return False
    if args.query_filters:
        query_text = " ".join([process_text, target_text, " ".join(contexts)])
        if not _contains_any(query_text, args.query_filters):
            return False
    return _matches_agent_dimension(process_text, target_text, contexts, args)


def _matches_route_kind(route_kind: str, filters: list[str]) -> bool:
    """Return whether a Mihomo route kind matches route filters."""
    normalized = _normalize_route_filter(route_kind)
    requested = {_normalize_route_filter(item) for item in filters}
    return normalized in requested


def _matches_agent_dimension(process_text: str, target_text: str,
                             contexts: list[str], args: Any) -> bool:
    """Return whether process/host/context matches app, agent and preset filters."""
    if not (args.app_filters or args.agent_filters or args.preset_filters):
        return True
    requested = set(_requested_contexts(args))
    if requested and requested.intersection(contexts):
        return True
    if _contains_any(process_text, _agent_process_terms(args)):
        return True
    return _contains_any(target_text, _agent_host_terms(args))
