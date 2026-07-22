"""A minimal fake 'Indeed' MCP server for tests (stdio transport).

Exposes a read-only `search_jobs` tool (what the Crawler should use) and a
mutating `apply_to_job` tool (which the read-only guard must refuse). Run by the
test harness as a subprocess via the MCP stdio client.
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-indeed")

_JOBS = [
    {
        "jobkey": "aa1",
        "company": "Nimbus Systems",
        "title": "Staff Software Engineer",
        "location": "Remote",
        "salary": "$200,000 - $240,000",
        "url": "https://indeed.test/viewjob?jk=aa1",
        "application_method": "indeed_apply",
        "requirements": ["python", "distributed systems", "kubernetes"],
        "snippet": "Build distributed systems in Python...",
    },
    {
        "jobkey": "bb2",
        "company": "Vertex Labs",
        "title": "Senior Backend Engineer",
        "location": "San Francisco, CA",
        "salary": "$190,000",
        "url": "https://indeed.test/viewjob?jk=bb2",
        "application_method": "external_ats",
        "requirements": ["python", "postgres", "go"],
        "snippet": "Own the backend platform...",
    },
]


@mcp.tool()
def search_jobs(query: str, location: str = "", limit: int = 25) -> dict:
    """Search Indeed job postings (read-only)."""
    q = (query or "").lower()
    hits = [j for j in _JOBS if q in j["title"].lower() or q in " ".join(j["requirements"]).lower()] or _JOBS
    return {"jobs": hits[:limit]}


@mcp.tool()
def apply_to_job(jobkey: str, resume: str = "") -> dict:
    """Submit an application (MUTATING — the crawler must never call this)."""
    return {"status": "submitted", "jobkey": jobkey}


if __name__ == "__main__":
    mcp.run()
