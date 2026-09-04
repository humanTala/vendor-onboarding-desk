"""
Web search tool for the Module 2 bridge project.

PRE-WRITTEN FOR YOU. You do not need to change anything in this file.
Read it if you are curious, then import web_search() and get on with the
interesting part - deciding what your agent does with the result.

No API key. No extra install. Uses `requests`, which you already have.

    from search_tools import web_search
    evidence = web_search("who founded Anthropic")

It returns a plain-text block of numbered findings with source URLs, or one
of two markers you must handle:

    NO_RESULTS          the search worked, but found nothing useful
    SEARCH_UNAVAILABLE  the network or the service failed

Those two are NOT the same thing, and your agent should not treat them the
same way. "I found nothing" is a real finding. "I could not look" is not.
"""

from __future__ import annotations

import time

import requests

NO_RESULTS = "NO_RESULTS"
SEARCH_UNAVAILABLE = "SEARCH_UNAVAILABLE"

# Wikimedia blocks browser-mimicking User-Agents ("Mozilla/5.0 ...") with
# HTTP 403 under its robot policy. It wants a real client name and a way to
# contact whoever is running it. Sending a fake browser string is what gets
# you rate-limited, not the volume of requests.
# https://meta.wikimedia.org/wiki/User-Agent_policy
_UA = {
    "User-Agent": (
        "AgenticBootcamp/1.0 "
        "(https://github.com/Alaaldin97/Agentic-AI-Bootcamp-Students) "
        "python-requests"
    )
}
_TIMEOUT = 12


class Unreachable(Exception):
    """The service could not be reached at all - network, DNS, proxy, timeout.

    This is deliberately NOT the same as 'the service answered and had
    nothing'. Telling those two apart is the whole point of the two markers
    below, so the failure has to survive the trip back up.
    """


def _get(url: str, params: dict) -> dict | None:
    """One GET with two retries.

    Returns parsed JSON, or None when the service answered but the answer was
    not usable. Raises Unreachable when we never got an answer at all.
    """
    last_error = "unknown"
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=_UA, timeout=_TIMEOUT)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 503):     # rate limited - back off
                last_error = f"HTTP {r.status_code}"
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 403:
                # Blocked, not empty. Returning None here would surface as
                # NO_RESULTS and tell you the claim wasn't found, when in fact
                # the question was never asked. That lie is worse than an error.
                raise Unreachable(f"HTTP 403 (blocked) from {url}")
            return None                          # answered, just not usefully
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(1.0 * (attempt + 1))
    raise Unreachable(last_error)


def _duckduckgo(query: str) -> list[tuple[str, str, str]]:
    """DuckDuckGo Instant Answer. Good for well-known entities."""
    data = _get("https://api.duckduckgo.com/",
                {"q": query, "format": "json", "no_html": 1, "skip_disambig": 1})
    if not data:
        return []

    found: list[tuple[str, str, str]] = []
    if data.get("AbstractText"):
        found.append((
            data.get("Heading") or query,
            data["AbstractText"],
            data.get("AbstractURL", ""),
        ))
    for topic in data.get("RelatedTopics", [])[:4]:
        if isinstance(topic, dict) and topic.get("Text"):
            found.append((
                topic.get("Text", "")[:60],
                topic["Text"],
                topic.get("FirstURL", ""),
            ))
    return found


def _wikipedia(query: str) -> list[tuple[str, str, str]]:
    """Wikipedia search + intro extracts. Broader coverage than the above."""
    hits = _get("https://en.wikipedia.org/w/api.php",
                {"action": "query", "list": "search", "srsearch": query,
                 "format": "json", "srlimit": 3})
    if not hits:
        return []
    titles = [h["title"] for h in hits.get("query", {}).get("search", [])]
    if not titles:
        return []

    ex = _get("https://en.wikipedia.org/w/api.php",
              {"action": "query", "prop": "extracts", "exintro": 1,
               "explaintext": 1, "titles": "|".join(titles), "format": "json"})
    if not ex:
        return []

    found: list[tuple[str, str, str]] = []
    for page in ex.get("query", {}).get("pages", {}).values():
        text = (page.get("extract") or "").strip()
        if text:
            title = page.get("title", "")
            url = "https://en.wikipedia.org/wiki/" + title.replace(" ", "_")
            found.append((title, text, url))
    return found


def web_search(query: str, max_chars: int = 1400) -> str:
    """Search the web and return plain text with sources.

    Returns NO_RESULTS if the search ran but found nothing useful, or
    SEARCH_UNAVAILABLE if the search could not run at all.
    """
    query = (query or "").strip()
    if not query:
        return NO_RESULTS

    reachable = False
    results: list[tuple[str, str, str]] = []

    for backend in (_duckduckgo, _wikipedia):
        try:
            got = backend(query)
        except Unreachable:
            continue              # never answered - do NOT count as reachable
        reachable = True          # it answered, even if with nothing
        results.extend(got)
        if len(results) >= 3:
            break

    if not reachable:
        return SEARCH_UNAVAILABLE
    if not results:
        return NO_RESULTS

    out, used = [], 0
    for i, (title, text, url) in enumerate(results[:5], 1):
        snippet = " ".join(text.split())
        if used + len(snippet) > max_chars:
            snippet = snippet[: max(0, max_chars - used)]
        if not snippet:
            break
        out.append(f"[{i}] {title}\n    {snippet}\n    source: {url or 'n/a'}")
        used += len(snippet)
        if used >= max_chars:
            break

    return "\n\n".join(out) if out else NO_RESULTS


def selftest() -> int:
    """Check the two backends can actually be reached from THIS machine.

    Run this first if you get NO_RESULTS on a question you know the answer to:

        python search_tools.py --selftest
    """
    print("checking search backends from this machine")
    print("=" * 60)
    ok = 0
    total = 0
    for name, backend in (("duckduckgo", _duckduckgo), ("wikipedia", _wikipedia)):
        total += 1
        try:
            hits = backend("Anthropic")
        except Unreachable as exc:
            print(f"  {name:<12} UNREACHABLE   {exc}")
            continue
        ok += 1
        print(f"  {name:<12} reachable     ({len(hits)} hits)")

    print("=" * 60)
    if ok == 0:
        print("Neither backend is reachable. Your network, proxy, VPN or DNS is")
        print("blocking them - it is NOT your agent code. Try a different")
        print("network or a VPN, then run this again.")
        return 1
    if ok < total:
        # Partial reachability is the dangerous case. Wikipedia is the backend
        # that actually carries most claims; DuckDuckGo answers only for very
        # well-known entities. If Wikipedia is blocked you will still get
        # NO_RESULTS on true claims, and nothing will look broken.
        print(f"WARNING: only {ok} of {total} backends reachable.")
        print("A NO_RESULTS here may mean 'blocked', not 'found nothing'.")
        print("Fix the unreachable backend above before trusting any verdict.")
        return 2
    print("Search works here. A NO_RESULTS is a real 'found nothing'.")
    return 0


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        raise SystemExit(selftest())

    q = " ".join(sys.argv[1:]) or "who founded Anthropic"
    print(f"QUERY: {q}\n" + "=" * 60)
    print(web_search(q))
