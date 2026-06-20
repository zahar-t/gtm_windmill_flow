"""Unipile LinkedIn client — warm engagement signals + (optional) profile email.

Unipile fronts a *connected* LinkedIn account: it returns only what that account
could itself see in a LinkedIn session. We use it for two inbound-intent signals:

  • profile viewers   — "who viewed your LinkedIn page". Not a typed endpoint;
                        rides the raw passthrough (POST /api/v1/linkedin). The
                        exact voyager path is account/version-specific, so it is
                        config-driven (UNIPILE_PROFILE_VIEWS_PATH) and skipped
                        when unset.
  • post engagers     — people who reacted to / commented on our recent posts.
                        Typed, confirmed endpoints:
                          GET /api/v1/users/{id}/posts        (own posts)
                          GET /api/v1/posts/{social_id}/reactions
                          GET /api/v1/posts/{social_id}/comments

Auth: header `X-API-KEY`, query param `account_id`. Base URL is the per-tenant
DSN (host:port) from the Unipile dashboard, e.g. https://api1.unipile.com:13111.

Every function returns [] when DSN / key / account are missing or on ANY error
(uses common/http, which never raises). Field access is defensive — Unipile
response shapes vary by LinkedIn version, so unknown shapes degrade to [] rather
than crash. No raw httpx here.
"""
from __future__ import annotations

from typing import Any

from scripts.common import config
from scripts.common.http import get_json, post_json


def _ready() -> bool:
    return bool(config.UNIPILE_DSN and config.UNIPILE_API_KEY and config.UNIPILE_ACCOUNT_ID)


def _base() -> str:
    """https://{DSN}/api/v1 — DSN already carries host:port, no scheme."""
    dsn = config.UNIPILE_DSN.replace("https://", "").replace("http://", "").rstrip("/")
    return f"https://{dsn}/api/v1"


def _headers() -> dict[str, str]:
    return {
        "X-API-KEY": config.UNIPILE_API_KEY,
        "accept": "application/json",
        "Content-Type": "application/json",
    }


def _acct() -> dict[str, str]:
    return {"account_id": config.UNIPILE_ACCOUNT_ID}


def _items(data: Any) -> list[dict]:
    """Pull the list out of a Unipile response (it paginates as {items: [...]})."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("items", "data", "elements", "results"):
            v = data.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def _public_id(raw: dict) -> str | None:
    """Best-effort LinkedIn public identifier across the field aliases Unipile uses."""
    return (
        raw.get("public_identifier")
        or raw.get("public_id")
        or raw.get("provider_id")
        or raw.get("id")
        or None
    )


def _linkedin_url(public_id: str | None) -> str | None:
    return f"https://www.linkedin.com/in/{public_id}" if public_id else None


def _person(raw: dict, *, kind: str, signal: str | None) -> dict[str, Any]:
    """Map a Unipile actor (viewer / reactor / commenter) to a partial lead.

    No email here — LinkedIn engagement gives identity + a 'why now', not an
    address. The email waterfall (or a future Unipile profile lookup) fills that.
    """
    pid = _public_id(raw)
    name = (
        raw.get("name")
        or raw.get("full_name")
        or f"{raw.get('first_name', '')} {raw.get('last_name', '')}".strip()
        or None
    )
    return {
        "name": name or None,
        "title": raw.get("headline") or raw.get("occupation") or raw.get("title") or None,
        "company": raw.get("company") or raw.get("company_name") or None,
        "linkedin_url": raw.get("profile_url") or raw.get("public_profile_url") or _linkedin_url(pid),
        "provider_id": pid,
        "country": raw.get("location") or raw.get("country") or None,
        "_kind": kind,        # viewer | reaction | comment — for logging/debug
        "_signal": signal,    # human-readable "why now" for personalization
    }


def get_profile_viewers(limit: int = 50) -> list[dict[str, Any]]:
    """Who viewed our LinkedIn profile (raw passthrough). [] unless configured."""
    if not _ready() or not config.UNIPILE_PROFILE_VIEWS_PATH:
        return []
    # Raw route: POST /api/v1/linkedin proxies an arbitrary LinkedIn endpoint.
    # The exact body schema is account-specific; we pass the configured path and
    # parse defensively, so a shape mismatch degrades to [] rather than breaking.
    data = post_json(
        f"{_base()}/linkedin",
        headers=_headers(),
        params=_acct(),
        json={"method": "GET", "request_url": config.UNIPILE_PROFILE_VIEWS_PATH},
        timeout=30.0,
        retries=1,
    )
    viewers = _items(data)[:limit]
    return [_person(v, kind="viewer", signal="viewed your LinkedIn profile") for v in viewers]


def _own_user_id() -> str | None:
    """Our own LinkedIn provider id, needed to list our own posts."""
    data = get_json(f"{_base()}/users/me", headers=_headers(), params=_acct(), retries=1)
    return _public_id(data) if isinstance(data, dict) else None


def get_post_engagers(post_lookback: int | None = None, per_post: int = 50) -> list[dict[str, Any]]:
    """People who reacted to or commented on our recent posts. [] when unconfigured."""
    if not _ready():
        return []
    n_posts = config.UNIPILE_POST_LOOKBACK if post_lookback is None else post_lookback
    uid = _own_user_id()
    if not uid:
        return []

    posts = _items(
        get_json(f"{_base()}/users/{uid}/posts", headers=_headers(), params=_acct(), retries=1)
    )[:max(0, n_posts)]

    engagers: list[dict[str, Any]] = []
    for post in posts:
        social_id = post.get("social_id") or post.get("id")
        if not social_id:
            continue
        snippet = (post.get("text") or "").strip().replace("\n", " ")[:60]
        post_ref = f'"{snippet}…"' if snippet else "your post"

        reactions = _items(
            get_json(f"{_base()}/posts/{social_id}/reactions",
                     headers=_headers(), params=_acct(), retries=1)
        )[:per_post]
        for r in reactions:
            actor = r.get("author") or r.get("user") or r  # reactor may be nested
            engagers.append(_person(actor, kind="reaction", signal=f"reacted to {post_ref}"))

        comments = _items(
            get_json(f"{_base()}/posts/{social_id}/comments",
                     headers=_headers(), params=_acct(), retries=1)
        )[:per_post]
        for c in comments:
            actor = c.get("author") or c.get("user") or c
            engagers.append(_person(actor, kind="comment", signal=f"commented on {post_ref}"))

    return engagers


if __name__ == "__main__":
    import json

    print(f"=== unipile.py smoke (keyless; ready={_ready()}) ===")
    viewers = get_profile_viewers()
    engagers = get_post_engagers()
    print(json.dumps({"viewers": viewers, "engagers": engagers}, default=str)[:400])
    assert viewers == [] and engagers == [], "no creds → no calls, empty lists"
    print("PASS: no DSN/key/account → returned [] without raising")
