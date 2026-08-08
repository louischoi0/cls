"""Resolving a GitHub link to a repository's README.

The link is operator-supplied, so it is **parsed into owner/repo and the fetch
URL is rebuilt from scratch** — never fetched as given. That is the whole
security posture of this module: a link can only ever cause a request to
`raw.githubusercontent.com` for a path this file constructed, so it cannot be
pointed at an internal address, a metadata endpoint, or a different port.
"""

from __future__ import annotations

import logging
import re

import httpx

log = logging.getLogger("cc_automation.github")

RAW_HOST = "raw.githubusercontent.com"
ALLOWED_HOSTS = frozenset({"github.com", "www.github.com"})

#: Tried in order; the first that exists wins.
README_NAMES = ("README.md", "readme.md", "README.markdown", "README.rst", "README.txt", "README")

_OWNER_REPO = re.compile(r"^[A-Za-z0-9._-]+$")

# https://github.com/o/r(.git)(/anything)  |  git@github.com:o/r(.git)  |  o/r
_HTTP = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/([^/]+)/([^/#?]+)", re.I)
_SSH = re.compile(r"^git@github\.com:([^/]+)/([^/#?]+)", re.I)
_BARE = re.compile(r"^([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)$")


class GithubError(Exception):
    """The link is not a usable GitHub repository, or its README is missing."""


def parse_repo(link: str) -> tuple[str, str]:
    """`https://github.com/owner/repo.git` -> `("owner", "repo")`.

    Anything that is not a GitHub repository reference is refused, including a
    URL on another host — this is what keeps the fetch below unreachable from a
    crafted link.
    """
    value = (link or "").strip()
    if not value:
        raise GithubError("no GitHub link given")

    for pattern in (_SSH, _HTTP, _BARE):
        match = pattern.match(value)
        if match:
            owner, repo = match.group(1), match.group(2)
            break
    else:
        raise GithubError(
            f"not a GitHub repository link: {link!r} "
            "(expected github.com/owner/repo)"
        )

    repo = repo[:-4] if repo.lower().endswith(".git") else repo
    if not _OWNER_REPO.match(owner) or not _OWNER_REPO.match(repo):
        raise GithubError(f"not a GitHub repository link: {link!r}")
    return owner, repo


def canonical_url(link: str) -> str:
    """The stored form of a link, so two spellings of one repo compare equal."""
    owner, repo = parse_repo(link)
    return f"https://github.com/{owner}/{repo}"


async def fetch_readme(link: str, timeout: float = 15.0) -> tuple[str, str]:
    """Return `(name, text)` for the repo's README, or raise GithubError.

    `HEAD` as the ref lets GitHub pick the default branch, so this works whether
    it is `main`, `master`, or anything else.
    """
    owner, repo = parse_repo(link)
    tried: list[str] = []
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for name in README_NAMES:
            url = f"https://{RAW_HOST}/{owner}/{repo}/HEAD/{name}"
            try:
                response = await client.get(url)
            except httpx.HTTPError as exc:
                raise GithubError(f"could not reach GitHub: {exc}") from exc
            if response.status_code == 404:
                tried.append(name)
                continue
            if response.is_success:
                log.info("fetched %s from %s/%s", name, owner, repo)
                return name, response.text
            raise GithubError(
                f"GitHub answered {response.status_code} for {owner}/{repo}/{name}"
            )
    raise GithubError(
        f"{owner}/{repo} has no README (tried {', '.join(tried)}). "
        "A private repository also looks like this — there is no token here."
    )
