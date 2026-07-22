"""MCP-backed job source for the Crawler (READ_BROWSER tier).

Makes the Crawler an MCP *client*: it connects to an external MCP server
(stdio or streamable-HTTP), calls a read-only search tool, and maps the result
into ``Posting`` dicts. This is the "prefer official API / feed" path (spec
§0.6) generalized to the MCP ecosystem — no browser, no scraping.

Safety (keeps the READ_BROWSER posture):
* Only a read-only tool may be called. A tool whose NAME looks mutating
  (apply/submit/create/update/delete/…) is refused, and if the server declares
  MCP annotations we honor ``readOnlyHint`` / ``destructiveHint``.
* The adapter never calls apply/submit tools even if the server exposes them.

The MCP SDK is async; this exposes a synchronous ``fetch`` so it drops straight
into the existing sync Crawler.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Optional

# Tool-name heuristics for the read-only guard.
_MUTATING_TOOL = re.compile(
    r"(apply|submit|create|update|delete|remove|write|post|send|pay|checkout|book)",
    re.I,
)
_READONLY_TOOL = re.compile(
    r"(search|list|get|find|query|read|fetch|lookup|browse|jobs?)", re.I
)


class MCPToolNotAllowed(Exception):
    """Raised when a configured MCP tool is not provably read-only."""


def _find_in_group(exc: BaseException, kind: type) -> Optional[BaseException]:
    """Find an exception of ``kind`` inside a (possibly nested) ExceptionGroup."""
    if isinstance(exc, kind):
        return exc
    for sub in getattr(exc, "exceptions", None) or ():
        found = _find_in_group(sub, kind)
        if found is not None:
            return found
    return None


class MCPJobSource:
    """Connect to one MCP server and fetch postings via a read-only tool.

    Config keys (all under a source's ``mcp`` block):
      transport: "stdio" | "http"
      # stdio:
      command, args (list), env (dict)
      # http:
      url, headers (dict)
      tool: tool name to call (must be read-only)
      query_arg: the tool argument that carries the search text (default "query")
      params: static arguments merged into every call (location, limit, …)
      result_path: dot-path to the list of jobs in the tool result (default "jobs")
      field_map: {posting_field: job_key} mapping (sensible Indeed defaults)
      allow_mutating: bool (default False) — override the read-only guard (discouraged)
    """

    _DEFAULT_FIELD_MAP = {
        "company": "company",
        "title": "title",
        "location": "location",
        "url": "url",
        "comp_text": "salary",
        "application_method": "application_method",
    }

    def __init__(self, config: dict[str, Any], *, timeout: float = 30.0):
        self.cfg = dict(config or {})
        self.tool = self.cfg.get("tool", "search_jobs")
        self.query_arg = self.cfg.get("query_arg", "query")
        self.params = dict(self.cfg.get("params", {}))
        self.result_path = self.cfg.get("result_path", "jobs")
        self.field_map = {**self._DEFAULT_FIELD_MAP, **(self.cfg.get("field_map") or {})}
        self.allow_mutating = bool(self.cfg.get("allow_mutating", False))
        self.timeout = timeout

    # ------------------------------------------------------------------ #
    def fetch(self, query: str) -> list[dict]:
        """Synchronously call the MCP tool and return posting dicts."""
        # Synchronous name guard BEFORE opening the async MCP session, so an
        # obviously-mutating tool is refused cleanly (no TaskGroup wrapping).
        self._precheck_tool_name()
        try:
            return asyncio.run(self._afetch(query))
        except BaseException as e:  # noqa: BLE001 - unwrap async ExceptionGroups
            refused = _find_in_group(e, MCPToolNotAllowed)
            if refused is not None:
                raise refused
            raise

    def _precheck_tool_name(self) -> None:
        if self.allow_mutating:
            return
        if _MUTATING_TOOL.search(self.tool):
            raise MCPToolNotAllowed(
                f"Tool {self.tool!r} name looks mutating; refused (READ_BROWSER)."
            )

    async def _afetch(self, query: str) -> list[dict]:
        session_cm, session = await self._connect()
        async with session_cm as (read, write, *_rest):
            from mcp import ClientSession
            async with ClientSession(read, write) as session:
                await session.initialize()
                await self._assert_tool_readonly(session)
                args = {**self.params, self.query_arg: query}
                result = await session.call_tool(self.tool, args)
                return self._map_result(result)

    async def _connect(self):
        """Return an async-context transport for the configured server."""
        transport = self.cfg.get("transport", "stdio")
        if transport == "stdio":
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client
            params = StdioServerParameters(
                command=self.cfg["command"],
                args=self.cfg.get("args", []),
                env=self.cfg.get("env") or None,
            )
            return stdio_client(params), None
        if transport in ("http", "streamable-http", "streamable_http"):
            from mcp.client.streamable_http import streamablehttp_client
            return streamablehttp_client(
                self.cfg["url"], headers=self.cfg.get("headers") or None
            ), None
        raise ValueError(f"Unknown MCP transport: {transport!r}")

    # ------------------------------------------------------------------ #
    async def _assert_tool_readonly(self, session) -> None:
        """Refuse to proceed unless the configured tool is provably read-only."""
        if self.allow_mutating:
            return
        if _MUTATING_TOOL.search(self.tool):
            raise MCPToolNotAllowed(
                f"Tool {self.tool!r} name looks mutating; refused (READ_BROWSER)."
            )
        # Honor MCP annotations when the server provides them.
        try:
            tools = (await session.list_tools()).tools
        except Exception:
            tools = []
        for t in tools:
            if t.name != self.tool:
                continue
            ann = getattr(t, "annotations", None)
            if ann is not None:
                if getattr(ann, "destructiveHint", False):
                    raise MCPToolNotAllowed(f"Tool {self.tool!r} is destructive; refused.")
                if getattr(ann, "readOnlyHint", None) is False:
                    raise MCPToolNotAllowed(f"Tool {self.tool!r} is not read-only; refused.")
            return  # found and allowed
        # Tool not advertised: fall back to the name heuristic (already passed
        # the mutating check); require it to look read-only.
        if not _READONLY_TOOL.search(self.tool):
            raise MCPToolNotAllowed(
                f"Tool {self.tool!r} is not advertised and does not look read-only; refused."
            )

    # ------------------------------------------------------------------ #
    def _map_result(self, result) -> list[dict]:
        if getattr(result, "isError", False):
            return []
        data = self._extract_payload(result)
        jobs = self._navigate(data, self.result_path)
        if not isinstance(jobs, list):
            return []
        out = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            posting = {dst: job.get(src, "") for dst, src in self.field_map.items()}
            posting["requirements"] = job.get("requirements", []) or []
            posting["raw"] = job
            if not posting.get("application_method"):
                posting["application_method"] = "external_ats"
            out.append(posting)
        return out

    @staticmethod
    def _extract_payload(result) -> Any:
        # Prefer structured content, else parse the first JSON text block.
        sc = getattr(result, "structuredContent", None)
        if sc is not None:
            return sc
        for block in getattr(result, "content", []) or []:
            text = getattr(block, "text", None)
            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    continue
        return {}

    @staticmethod
    def _navigate(data: Any, path: str) -> Any:
        if not path:
            return data
        cur = data
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return None
        return cur
