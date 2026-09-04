# NOTES

## a) Where did the budget force a real trade-off?
The budget constraint forced the biggest trade-off on identity establishment depth versus total coverage. I chose to spend lookups first on suppliers with ambiguity or higher risk signals (for example, Siemens AG with multiple GLEIF candidates, and UAE trading entities where false positives and identity ambiguity are more likely).

The practical trade-off was that lower-ambiguity names received only one identity lookup when possible, while ambiguous names received two-step checks (GLEIF plus web search when needed). This keeps all seven suppliers covered while accepting shallower evidence for lower-risk cases.

## b) What sanctions threshold did you set, and why that number?
Threshold used: 0.90 similarity from sanctions_screen.

Why 0.90:
- In verify_tools and live runs, known noisy non-sanctioned names can produce high but sub-0.90 matches due to shared tokens like "Trading" and "Company".
- The listed supplier case produces a near-exact match around 1.00.
- 0.90 is a conservative line that still catches near-exact hits while reducing false rejects from fuzzy token overlap.

Cost if wrong:
- Too low: honest suppliers get blocked, payment operations are disrupted, and Procurement escalations increase.
- Too high: a sanctioned counterparty might slip through, creating legal and regulatory exposure.

## c) Where did the agent nearly get it wrong?
The riskiest failure mode was treating a GLEIF hit as automatic approval, especially for Siemens AG where multiple candidates exist and none is an exact match. Early iterations can drift toward "found in registry, therefore approve".

What was changed:
- Added explicit decision rules in the decision prompt: multiple candidates with zero exact matches must be CONDITIONS and request LEI or registration number.
- Added structured-output retry-once wrappers and safe fallbacks so one malformed model response does not terminate the full run after budget has already been spent.

Run-to-run consistency:
- I ran the agent twice. Verdict classes and supplier outcomes were stable across runs.
- Narrative wording and lookup ordering varied slightly between runs, which is expected model non-determinism even at temperature 0 under tool-calling flows.
