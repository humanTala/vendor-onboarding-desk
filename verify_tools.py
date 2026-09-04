"""
Check your tools against reality before you build anything on top of them.

    python verify_tools.py

Every case hits the live GLEIF register and the live OFAC list, and checks the
answer against something independently true. If this passes, your plumbing is
sound and any bug after this point is in your agent.

If it fails, fix it first. Debugging a graph on top of a broken tool is the
most expensive hour you can spend.
"""

from __future__ import annotations

import sys

from registry_tools import (
    LOOKUP_UNAVAILABLE,
    NO_RECORDS,
    NO_SANCTIONS_MATCH,
    gleif_lookup,
    sanctions_screen,
)

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(label: str, predicate, why: str) -> None:
    ok = False
    try:
        ok = predicate()
    except NotImplementedError:
        why = "gleif_lookup still raises NotImplementedError - that one is yours"
    except Exception as e:                                # noqa: BLE001
        why = f"{why} (raised {type(e).__name__}: {e})"
    results.append((PASS if ok else FAIL, label, "" if ok else why))


print("contacting GLEIF and OFAC - the sanctions list is ~5 MB on first run\n")

# --------------------------------------------------------------- sanctions ----
r = sanctions_screen("Al Wasel and Babel General Trading LLC")
check("sanctions: a listed entity scores near 1.00",
      lambda: r not in (NO_SANCTIONS_MATCH, LOOKUP_UNAVAILABLE) and "1.00" in r,
      f"this company is on the OFAC list; got: {r[:70]!r}")

check("sanctions: the report names the programme",
      lambda: "programme:" in r,
      "a score with no programme is not actionable")

r2 = sanctions_screen("Maersk A/S")
check("sanctions: a clean name does not score 1.00",
      lambda: "1.00" not in r2,
      "Maersk is not on the list - a 1.00 here means your screening is broken")

# ------------------------------------------------------------------- GLEIF ----
check("gleif: a real company is found",
      lambda: (lambda g: g not in (NO_RECORDS, LOOKUP_UNAVAILABLE)
               and "Maersk" in g)(gleif_lookup("Maersk A/S")),
      "expected candidates for 'Maersk A/S'")

check("gleif: the report includes the LEI code",
      lambda: "LEI" in gleif_lookup("Maersk A/S"),
      "the LEI is the one unambiguous identifier - include it")

check("gleif: the report includes BOTH status fields",
      lambda: (lambda g: "entity status" in g.lower()
               and "registration status" in g.lower())(gleif_lookup("C & V Works ApS")),
      "entity status and registration status are different things; report both")

check("gleif: a lapsed registration is visible",
      lambda: "LAPSED" in gleif_lookup("C & V Works ApS").upper(),
      "'C & V Works ApS' has an ACTIVE entity with a LAPSED registration")

check("gleif: an unknown name returns the sentinel",
      lambda: gleif_lookup("Zorblax Trading FZE") == NO_RECORDS,
      "expected NO_RECORDS exactly")

check("gleif: ALL candidates are returned, not just the first",
      lambda: (lambda g: g.count("LEI:") >= 3)(gleif_lookup("Siemens AG")),
      "'Siemens AG' matches several different legal entities. Returning only "
      "the first hides the most important decision in this project.")

check("gleif: candidates that are not exact matches are flagged as such",
      lambda: (lambda g: "0 exactly" in g or "0 exact" in g)(gleif_lookup("Siemens AG")),
      "none of the 'Siemens AG' candidates is named exactly 'Siemens AG' - "
      "your agent needs to be told that")

# ------------------------------------------------------------------ report ----
print(f"{'':2}{'result':<7}{'check'}")
print("-" * 76)
for status, label, why in results:
    print(f"  {'ok  ' if status == PASS else 'FAIL':<7}{label}")
    if why:
        print(f"         -> {why}")

n_fail = sum(1 for s, _, _ in results if s == FAIL)
print("-" * 76)
if n_fail:
    print(f"{n_fail} of {len(results)} checks failed. Fix these before building the graph.")
    sys.exit(1)
print(f"all {len(results)} checks passed - your tools are sound. Now build the agent.")
