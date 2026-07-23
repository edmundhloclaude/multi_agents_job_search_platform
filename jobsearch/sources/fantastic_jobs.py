"""Fantastic.jobs (Active Jobs DB) source for the Crawler (READ_BROWSER, API path).

Maps the Active Jobs DB response into the Crawler's posting dicts. Read-only
data retrieval (spec §0.6, "prefer official API/feed"). The key is read from an
environment variable, never config.yaml.

Supports two access modes (source `params.auth`):
  * "bearer"   (default) — the direct API: GET https://data.fantastic.jobs/v1/active-ats
                           with `Authorization: Bearer <key>`.
  * "rapidapi"           — via RapidAPI: X-RapidAPI-Key / X-RapidAPI-Host headers.

Registered for sources with ``type: api`` and ``provider: fantastic_jobs``.
Signature matches the Crawler's api_fetchers contract: (source, query) -> list[dict].
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

_DIRECT_URL = "https://data.fantastic.jobs/v1/active-ats"
_RAPIDAPI_URL = "https://active-jobs-db.p.rapidapi.com/active-ats-24h"
_RAPIDAPI_HOST = "active-jobs-db.p.rapidapi.com"
_DEFAULT_KEY_ENV = "FANTASTIC_JOBS_API_KEY"


class FantasticJobsError(Exception):
    pass


def _http_get_json(url: str, headers: dict, params: dict, *, timeout: float = 30.0) -> Any:
    """GET with query params + headers; return parsed JSON. Injectable in tests."""
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        raise FantasticJobsError(f"HTTP {e.code} from Fantastic.jobs: {body}") from e
    except urllib.error.URLError as e:
        raise FantasticJobsError(f"network error calling Fantastic.jobs: {e}") from e


def _location(job: dict) -> str:
    loc = ""
    ld = job.get("locations_derived")
    if isinstance(ld, list) and ld:
        loc = str(ld[0])
    elif isinstance(job.get("cities_derived"), list) and job["cities_derived"]:
        parts = [str(job["cities_derived"][0])]
        if isinstance(job.get("countries_derived"), list) and job["countries_derived"]:
            parts.append(str(job["countries_derived"][0]))
        loc = ", ".join(parts)
    # Remote signal lives in location_type / ai_work_arrangement (no remote flag).
    arr = " ".join(str(job.get(k, "") or "") for k in
                   ("location_type", "ai_work_arrangement")).lower()
    if "remote" in arr:
        loc = f"{loc} (Remote)".strip() if loc else "Remote"
    return loc


def _num(x):
    return f"{x:,}" if isinstance(x, (int, float)) else str(x)


def _comp_text(job: dict) -> str:
    lo, hi = job.get("ai_salary_min_value"), job.get("ai_salary_max_value")
    cur = job.get("ai_salary_currency") or ""
    unit = job.get("ai_salary_unit_text") or ""
    if lo and hi:
        return f"{cur} {_num(lo)}-{_num(hi)} {unit}".strip()
    if job.get("ai_salary_value"):
        return f"{cur} {_num(job['ai_salary_value'])} {unit}".strip()
    sal = job.get("salary")
    return str(sal) if sal else ""


def _map_job(job: dict) -> dict:
    # ai_key_skills is a parsed skills list — use it as requirements (screening signal).
    skills = job.get("ai_key_skills")
    reqs = [str(s).strip().lower() for s in skills][:40] if isinstance(skills, list) else []
    return {
        "company": job.get("organization", "") or "",
        "title": job.get("title", "") or "",
        "location": _location(job),
        "url": job.get("url", "") or "",
        "comp_text": _comp_text(job),
        "requirements": reqs,
        "application_method": "external_ats",
        # Expose the description under "description" so the Screener/dashboard
        # pick it up uniformly (they read raw["description"]).
        "raw": {**job, "description": job.get("description_text", "") or ""},
    }


def _extract_jobs(resp: Any) -> list:
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for key in ("data", "jobs", "results"):
            if isinstance(resp.get(key), list):
                return resp[key]
    return []


def search_jobs(
    query: str,
    *,
    token: str,
    params: dict[str, Any] | None = None,
    max_pages: int = 1,
    http: Callable[..., Any] | None = None,
) -> list[dict]:
    """Call the Active Jobs DB for one query, paginating up to ``max_pages``."""
    http = http or _http_get_json
    params = dict(params or {})
    auth = str(params.pop("auth", "bearer")).lower()
    base_url = params.pop("base_url", None) or (_RAPIDAPI_URL if auth == "rapidapi" else _DIRECT_URL)
    limit = int(params.pop("limit", 10))

    if auth == "rapidapi":
        host = params.pop("rapidapi_host", _RAPIDAPI_HOST)
        headers = {"X-RapidAPI-Key": token, "X-RapidAPI-Host": host, "Accept": "application/json"}
    else:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    base_params: dict[str, Any] = {"time_frame": "24h", "description_format": "text"}
    base_params.update(params)
    base_params["limit"] = limit
    if query:
        base_params.setdefault("title", f'"{query}"')

    out: list[dict] = []
    for page in range(max(1, max_pages)):
        q = {**base_params, "offset": page * limit}
        jobs = _extract_jobs(http(base_url, headers, q))
        out.extend(_map_job(j) for j in jobs if isinstance(j, dict))
        if len(jobs) < limit:
            break
    return out


def fantastic_jobs_fetcher(source: dict, query: str) -> list[dict]:
    """api_fetchers-compatible entry point. Reads the key from the environment."""
    key_env = source.get("api_key_env", _DEFAULT_KEY_ENV)
    token = os.environ.get(key_env)
    if not token:
        raise FantasticJobsError(
            f"{key_env} is not set; export your Fantastic.jobs API key to enable this source."
        )
    return search_jobs(
        query,
        token=token,
        params=dict(source.get("params", {})),
        max_pages=int(source.get("max_pages", 1)),
    )
