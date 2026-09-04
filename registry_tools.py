"""
Registry tools for the Vendor Onboarding Desk.

Two real sources. No API key. No pip install.

    GLEIF   https://api.gleif.org/api/v1/lei-records
            The global register of Legal Entity Identifiers. Regulators created
            it after 2008 so that "who exactly is this counterparty?" has one
            answer worldwide.

    OFAC    https://www.treasury.gov/ofac/downloads/sdn.csv
            The US Treasury sanctions list. Paying anyone on it is a criminal
            offence in most jurisdictions, including via a subsidiary.

-------------------------------------------------------------------------------
ONE OF THESE IS NOT WRITTEN.

`sanctions_screen()` is finished - read it, then leave it alone.
`gleif_lookup()` is YOUR job. Spec is in its docstring.
`verify_tools.py` will tell you the moment it is right.

Read the docs before you start:
    https://www.gleif.org/en/lei-data/gleif-api
-------------------------------------------------------------------------------

Every tool returns a STRING. Never raises, never returns a dict - same rule as
the calculator in Module 3. A tool that raises kills your graph; a tool that
returns "NO_RECORDS" is something your agent can read and react to.

    NO_RECORDS          the registry answered, and has nothing under that name
    NO_SANCTIONS_MATCH  screened, and nothing came close
    LOOKUP_UNAVAILABLE  the call did not complete (network, timeout, 5xx)

NO_RECORDS and LOOKUP_UNAVAILABLE are NOT the same thing.
And - this one matters more - NO_RECORDS does not mean the company is fake.
Work out why before you write any graph code.
"""

from __future__ import annotations

import csv
import difflib
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

NO_RECORDS = "NO_RECORDS"
NO_SANCTIONS_MATCH = "NO_SANCTIONS_MATCH"
LOOKUP_UNAVAILABLE = "LOOKUP_UNAVAILABLE"

_UA = "JHF-Agentic-AI-Bootcamp/1.0 (student project; contact: your-email@example.com)"
_TIMEOUT = 30
_SDN_URL = "https://www.treasury.gov/ofac/downloads/sdn.csv"
_SDN_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".sdn_cache.csv")


# ===========================================================================
# TOOL 1 - SANCTIONS SCREENING.   Finished. Read it, then leave it alone.
# ===========================================================================

def _load_sdn() -> list[tuple[str, str]]:
    """The SDN list is ~5 MB. Download once, then read from disk."""
    if not os.path.exists(_SDN_CACHE):
        req = urllib.request.Request(_SDN_URL, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(_SDN_CACHE, "wb") as f:
                f.write(r.read())

    with open(_SDN_CACHE, "r", encoding="utf-8", errors="replace") as f:
        rows = csv.reader(f)
        # col1 = name, col2 = type, col3 = sanctions programme
        return [(r[1].strip(), r[3].strip())
                for r in rows
                if len(r) > 3 and r[2].strip().lower() == "-0-" and r[1].strip()]


def _normalise(name: str) -> str:
    """Lowercase, drop punctuation, drop the legal-form suffix."""
    s = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    for suffix in (" ltd", " llc", " inc", " plc", " ag", " a s", " pjsc",
                   " fze", " aps", " gmbh", " sa", " nv", " bv"):
        s = s.replace(suffix, " ")
    return " ".join(s.split())


def sanctions_screen(name: str, top: int = 5) -> str:
    """
    Screen a name against the OFAC sanctions list.

    Returns the CLOSEST NAMES and how similar they are - 0.0 to 1.0.

    ---------------------------------------------------------------------------
    READ THIS CAREFULLY. It does NOT tell you whether the company is sanctioned.
    It tells you what the list contains that looks a bit like the name you gave
    it. Deciding whether any of that is a real match is YOUR AGENT'S JOB.

    That is not a limitation of this tool - it is the actual problem. Every
    bank on earth employs people to clear false positives from screens exactly
    like this one. Fuzzy name matching produces near-misses constantly:
    "Trading Company" resembles a thousand other "Trading Companies".

    Set your threshold too low and you refuse to pay honest suppliers.
    Set it too high and you wire money to a sanctioned entity.
    Both are real failures. You will have to pick a number and defend it.
    ---------------------------------------------------------------------------
    """
    try:
        sdn = _load_sdn()
    except Exception:                                     # noqa: BLE001
        return LOOKUP_UNAVAILABLE

    target = _normalise(name)
    if not target:
        return NO_SANCTIONS_MATCH

    scored = []
    for listed, programme in sdn:
        ratio = difflib.SequenceMatcher(None, target, _normalise(listed)).ratio()
        scored.append((ratio, listed, programme))
    scored.sort(reverse=True)

    best = scored[:top]
    if not best or best[0][0] < 0.55:
        return NO_SANCTIONS_MATCH

    lines = [f"closest entries on the OFAC list to '{name}' "
             f"(similarity 0.0-1.0, NOT a verdict):"]
    for ratio, listed, programme in best:
        lines.append(f"  {ratio:.2f}  {listed}   [programme: {programme}]")
    return "\n".join(lines)


# ===========================================================================
# TOOL 2 - GLEIF.   YOU WRITE THIS ONE.
# ===========================================================================

def gleif_lookup(legal_name: str) -> str:
    """Look up a legal name in the live GLEIF LEI register.

    Returns a readable string containing ALL matching candidates.
    Never raises an exception.

    Returns:
        NO_RECORDS if GLEIF answered successfully but found no candidates.
        LOOKUP_UNAVAILABLE if the request could not be completed.
    """
    try:
        legal_name = (legal_name or "").strip()

        if not legal_name:
            return NO_RECORDS

        params = urllib.parse.urlencode({
            "filter[entity.legalName]": legal_name
        })

        url = "https://api.gleif.org/api/v1/lei-records?" + params

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": _UA,
                "Accept": "application/vnd.api+json",
            },
        )

        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            raw = response.read().decode("utf-8")

        payload = json.loads(raw)
        data = payload.get("data", [])

        if not data:
            return NO_RECORDS

        candidates = []

        for item in data:
            attributes = item.get("attributes", {})

            entity = attributes.get("entity", {})
            registration = attributes.get("registration", {})

            lei = item.get("id", "unknown")

            legal_name_data = entity.get("legalName", {})
            candidate_name = (
                legal_name_data.get("name")
                if isinstance(legal_name_data, dict)
                else str(legal_name_data)
            ) or "unknown"

            legal_address = entity.get("legalAddress", {})
            country = legal_address.get("country", "unknown")

            entity_status = entity.get("status", "unknown")
            registration_status = registration.get("status", "unknown")

            candidates.append(
                "\n".join([
                    f"Legal name: {candidate_name}",
                    f"LEI: {lei}",
                    f"Country: {country}",
                    f"Entity status: {entity_status}",
                    f"Registration status: {registration_status}",
                ])
            )

        exact_matches = sum(
            1
            for item in data
            if (
                item.get("attributes", {})
                .get("entity", {})
                .get("legalName", {})
                .get("name", "")
                .strip()
                .casefold()
                == legal_name.casefold()
            )
        )

        header = (
            f"GLEIF lookup for '{legal_name}': "
            f"{len(data)} candidate(s); "
            f"{exact_matches} exact match(es)."
        )

        return header + "\n\n" + "\n\n".join(
            f"Candidate {i}:\n{candidate}"
            for i, candidate in enumerate(candidates, 1)
        )

    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        json.JSONDecodeError,
        OSError,
        ValueError,
    ):
        return LOOKUP_UNAVAILABLE

    except Exception:
        # The assignment requires this function to never raise.
        return LOOKUP_UNAVAILABLE