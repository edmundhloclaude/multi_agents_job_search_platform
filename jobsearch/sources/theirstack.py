"""TheirStack job-postings source for the Crawler (READ_BROWSER, API path).

Maps TheirStack's POST /v1/jobs/search results into the Crawler's posting dicts.
Read-only data retrieval — no browser, no scraping (spec §0.6, "prefer official
API/feed"). The API key is read from an environment variable, never from
config.yaml (same posture as the OpenAI key).

Registered by the orchestrator for any source with ``type: api`` and
``provider: theirstack``. Its function signature matches the Crawler's
``api_fetchers`` contract: ``fetcher(source_config, query) -> list[dict]``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable

_ENDPOINT = "https://api.theirstack.com/v1/jobs/search"
_DEFAULT_KEY_ENV = "THEIRSTACK_API_KEY"


class TheirStackError(Exception):
    pass


def _http_post_json(url: str, token: str, payload: dict, *, timeout: float = 30.0) -> dict:
    """POST JSON with a bearer token; return parsed JSON. Injectable in tests."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        raise TheirStackError(f"HTTP {e.code} from TheirStack: {body}") from e
    except urllib.error.URLError as e:
        raise TheirStackError(f"network error calling TheirStack: {e}") from e


def _comp_text(job: dict) -> str:
    lo, hi = job.get("min_annual_salary_usd"), job.get("max_annual_salary_usd")
    if lo and hi:
        return f"${int(lo):,} - ${int(hi):,} USD"
    if lo:
        return f"from ${int(lo):,} USD"
    if hi:
        return f"up to ${int(hi):,} USD"
    return ""


def _map_job(job: dict) -> dict:
    company = job.get("company") or (job.get("company_object") or {}).get("name") or ""
    location = job.get("location") or ""
    if job.get("remote") and "remote" not in location.lower():
        location = (location + " (Remote)").strip() if location else "Remote"
    # Prefer the direct apply/source URL for the applier; fall back to the listing.
    apply_url = job.get("final_url") or job.get("source_url") or job.get("url") or ""
    # TheirStack gives structured tech + keyword slugs — use them as requirements
    # so the Screener has real signal (dedup, preserve order).
    reqs, seen = [], set()
    for slug in (job.get("technology_slugs") or []) + (job.get("keyword_slugs") or []):
        s = str(slug).replace("-", " ").strip().lower()
        if s and s not in seen:
            seen.add(s)
            reqs.append(s)
    return {
        "company": company,
        "title": job.get("job_title", ""),
        "location": location,
        "url": apply_url,
        "comp_text": _comp_text(job),
        "requirements": reqs[:40],
        "application_method": "indeed_apply" if job.get("easy_apply") else "external_ats",
        "raw": job,
    }


def search_jobs(
    query: str,
    *,
    token: str,
    params: dict[str, Any] | None = None,
    max_pages: int = 1,
    http: Callable[..., dict] | None = None,
) -> list[dict]:
    """Call /v1/jobs/search for one query, paginating up to ``max_pages``."""
    # Resolve at call time so tests can monkeypatch the module-level poster.
    http = http or _http_post_json
    params = dict(params or {})
    limit = int(params.pop("limit", 25))
    # TheirStack requires at least one filter; ensure a recency floor.
    body_base: dict[str, Any] = {"posted_at_max_age_days": 30, "limit": limit}
    body_base.update(params)
    if query:
        body_base.setdefault("job_title_or", [query])

    out: list[dict] = []
    for page in range(max(1, max_pages)):
        body = {**body_base, "page": page}
        resp = http(_ENDPOINT, token, body)
        jobs = resp.get("data") or []
        out.extend(_map_job(j) for j in jobs)
        if len(jobs) < limit:  # last page
            break
    return out


def theirstack_fetcher(source: dict, query: str) -> list[dict]:
    """api_fetchers-compatible entry point. Reads the key from the environment."""
    key_env = source.get("api_key_env", _DEFAULT_KEY_ENV)
    token = os.environ.get(key_env)
    if not token:
        raise TheirStackError(
            f"{key_env} is not set; export your TheirStack API key to enable this source."
        )
    return search_jobs(
        query,
        token=token,
        params=dict(source.get("params", {})),
        max_pages=int(source.get("max_pages", 1)),
    )
