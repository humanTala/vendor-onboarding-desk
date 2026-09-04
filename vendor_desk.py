from __future__ import annotations

import os
import re
from typing import Literal, Optional, TypedDict

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from registry_tools import (
    LOOKUP_UNAVAILABLE,
    NO_RECORDS,
    NO_SANCTIONS_MATCH,
    gleif_lookup,
    sanctions_screen,
)
from search_tools import web_search

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))
load_dotenv()

MAX_LOOKUPS = 9
MAX_SCREENING_STEPS = 2
SANCTIONS_REJECT_THRESHOLD = 0.90

BUDGET = {"used": 0}


def spend(what: str) -> bool:
    """Call before every lookup. Returns False when the budget is gone."""
    if BUDGET["used"] >= MAX_LOOKUPS:
        return False
    BUDGET["used"] += 1
    print(f"  [{BUDGET['used']:2}/{MAX_LOOKUPS}] {what}")
    return True


def get_llm() -> ChatOpenAI:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is missing. Add it to bridge-project/.env.")
    return ChatOpenAI(
        model="openai/gpt-4o-mini",
        temperature=0,
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )


def _best_sanctions_score(report: str) -> float:
    if report in (NO_SANCTIONS_MATCH, LOOKUP_UNAVAILABLE):
        return 0.0
    scores = []
    for line in report.splitlines():
        match = re.match(r"\s*([0-9]+\.[0-9]{2})\s+", line)
        if match:
            scores.append(float(match.group(1)))
    return max(scores, default=0.0)


def _default_search_query(supplier: dict) -> str:
    return (
        f'"{supplier["name"]}" {supplier["jurisdiction"]} '
        f'{supplier["category"]} company'
    )


def _read_request() -> str:
    with open(os.path.join(_PROJECT_ROOT, "REQUEST.md"), "r", encoding="utf-8") as handle:
        return handle.read()


def _invoke_structured(schema, prompt: str, fallback_factory):
    """Retry once on structured-output failure, then return a safe fallback."""
    llm = get_llm().with_structured_output(schema)
    for _ in range(2):
        try:
            return llm.invoke(prompt)
        except Exception:
            continue
    return fallback_factory()


def _fallback_plan_from_request(request: str):
    lines = request.splitlines()
    suppliers = []
    risk_rank = {"UAE": 3, "Saudi Arabia": 2, "Germany": 1, "Denmark": 0}

    for raw in lines:
        if "—" not in raw and " - " not in raw:
            continue
        if "supplier" in raw.lower() or "From:" in raw or "To:" in raw:
            continue

        if "—" in raw:
            left, right = raw.split("—", 1)
        elif " - " in raw:
            left, right = raw.split(" - ", 1)
        else:
            continue

        name = left.strip()
        detail = right.strip()
        if not name or "," not in detail:
            continue

        category, jurisdiction = [part.strip() for part in detail.split(",", 1)]
        suppliers.append(
            {
                "name": name,
                "category": category,
                "jurisdiction": jurisdiction,
                "priority_reason": "Higher identity ambiguity and jurisdictional risk compared with remaining queue.",
                "_risk": risk_rank.get(jurisdiction, 1),
            }
        )

    suppliers.sort(key=lambda s: (s["_risk"], len(s["name"])), reverse=True)

    out = [
        Supplier(
            name=item["name"],
            category=item["category"],
            jurisdiction=item["jurisdiction"],
            priority_reason=item["priority_reason"],
        )
        for item in suppliers
    ]

    return Plan(
        suppliers=out,
        budget_strategy="Fallback ordering by likely ambiguity and jurisdictional risk when model output is unavailable.",
    )


class Supplier(BaseModel):
    name: str
    category: str
    jurisdiction: str
    priority_reason: str = Field(
        description="Why this supplier should be checked in this position under the lookup budget."
    )


class Plan(BaseModel):
    suppliers: list[Supplier] = Field(
        description="All suppliers from the email, ordered from highest to lowest lookup priority."
    )
    budget_strategy: str = Field(
        description="One short sentence describing how to spend the limited lookup budget."
    )


class Verdict(BaseModel):
    supplier: str
    verdict: Literal["APPROVE", "CONDITIONS", "REJECT", "INSUFFICIENT"]
    reason: str = Field(description="Why. In language Rana can repeat to Procurement.")
    action_required: Optional[str] = Field(
        default=None,
        description="Concrete next step required for CONDITIONS or INSUFFICIENT. Optional otherwise.",
    )


class ScreenAction(BaseModel):
    tool: Literal["gleif_lookup", "web_search", "finish"]
    query: Optional[str] = Field(
        default=None,
        description="Supplier name for GLEIF or a targeted query for web search.",
    )
    note: str = Field(description="Why this is the next best action.")


class State(TypedDict):
    request: str
    queue: list
    evidence: list
    verdicts: list
    skipped: list
    sanctions: dict
    all_suppliers: list
    current_supplier: Optional[dict]
    current_evidence: list
    memo: Optional[str]
    plan_summary: str


class ScreenState(TypedDict):
    supplier: dict
    sanctions_report: str
    observations: list[str]
    action: Optional[dict]
    done: bool
    tool_steps: int


def _screen_reason(state: ScreenState) -> ScreenState:
    if state["done"] or state["tool_steps"] >= MAX_SCREENING_STEPS:
        state["done"] = True
        return state

    supplier = state["supplier"]
    observations = "\n\n".join(state["observations"]) or "(nothing yet)"
    prompt = (
        "You are screening one supplier for vendor onboarding.\n"
        "Sanctions screening is already complete and does not consume budget.\n"
        "GLEIF and web search DO consume the budget, so conserve them.\n"
        "Prefer gleif_lookup first when identity can be established from the legal name.\n"
        "Use web_search when GLEIF returns NO_RECORDS or when you need independent evidence the company exists.\n"
        "Use finish when you have enough evidence for a finance decision or when more lookup spend is not justified.\n\n"
        f"SUPPLIER: {supplier['name']}\n"
        f"CATEGORY: {supplier['category']}\n"
        f"JURISDICTION: {supplier['jurisdiction']}\n"
        f"SANCTIONS REPORT:\n{state['sanctions_report']}\n\n"
        f"OBSERVATIONS SO FAR:\n{observations}"
    )

    def fallback_action() -> ScreenAction:
        if not state["observations"]:
            return ScreenAction(tool="gleif_lookup", query=supplier["name"], note="Fallback first check")
        if any(NO_RECORDS in obs for obs in state["observations"]) and not any(
            obs.startswith("web_search(") for obs in state["observations"]
        ):
            return ScreenAction(tool="web_search", query=_default_search_query(supplier), note="Fallback existence check")
        return ScreenAction(tool="finish", note="Fallback stop")

    action = _invoke_structured(ScreenAction, prompt, fallback_action)
    state["action"] = action.model_dump()
    return state


def _screen_act(state: ScreenState) -> ScreenState:
    action = state.get("action") or {}
    tool = action.get("tool")

    if tool == "finish":
        state["done"] = True
        return state

    supplier = state["supplier"]
    if not spend(f"{tool} for {supplier['name']}"):
        state["observations"].append(
            f"budget exhausted before {tool} could run for {supplier['name']}"
        )
        state["done"] = True
        return state

    if tool == "gleif_lookup":
        query = supplier["name"]
        result = gleif_lookup(query)
    elif tool == "web_search":
        query = action.get("query") or _default_search_query(supplier)
        result = web_search(query)
    else:
        state["observations"].append("unknown tool requested during screening")
        state["done"] = True
        return state

    state["tool_steps"] += 1
    state["observations"].append(f"{tool}({query})\n{result}")
    return state


def _screen_done(state: ScreenState) -> str:
    if state["done"] or state["tool_steps"] >= MAX_SCREENING_STEPS:
        return "end"
    return "loop"


_SUPPLIER_SCREEN_GRAPH = None


def _get_supplier_screen_graph():
    global _SUPPLIER_SCREEN_GRAPH
    if _SUPPLIER_SCREEN_GRAPH is None:
        graph = StateGraph(ScreenState)
        graph.add_node("reason", _screen_reason)
        graph.add_node("act", _screen_act)
        graph.set_entry_point("reason")
        graph.add_edge("reason", "act")
        graph.add_conditional_edges("act", _screen_done, {"loop": "reason", "end": END})
        _SUPPLIER_SCREEN_GRAPH = graph.compile()
    return _SUPPLIER_SCREEN_GRAPH


def triage(state: State) -> State:
    prompt = (
        "Read Rana's email and extract every supplier.\n"
        "Return all suppliers in the order the limited lookup budget should be spent.\n"
        "Prioritise suppliers where the typed name is likely ambiguous, suppliers in higher-risk jurisdictions, "
        "and suppliers likely to need more than one lookup.\n"
        "Remember: sanctions screening happens for everyone regardless of budget, so this order is only for identity-establishing work.\n\n"
        f"EMAIL:\n{state['request']}"
    )

    plan = _invoke_structured(Plan, prompt, lambda: _fallback_plan_from_request(state["request"]))

    queue = []
    evidence = []
    verdicts = []
    sanctions = {}

    for supplier_model in plan.suppliers:
        supplier = supplier_model.model_dump()
        sanctions_report = sanctions_screen(supplier["name"])
        sanctions[supplier["name"]] = sanctions_report

        if _best_sanctions_score(sanctions_report) >= SANCTIONS_REJECT_THRESHOLD:
            verdicts.append(
                Verdict(
                    supplier=supplier["name"],
                    verdict="REJECT",
                    reason=(
                        f"Sanctions screening found a closest OFAC match at or above "
                        f"{SANCTIONS_REJECT_THRESHOLD:.2f} similarity for this exact supplier name. "
                        "Paying it should be treated as prohibited unless Legal clears the match."
                    ),
                    action_required="Do not release payment. Escalate to Legal and do not contact the supplier directly.",
                ).model_dump()
            )
            evidence.append(
                {
                    "supplier": supplier["name"],
                    "items": [f"sanctions_screen({supplier['name']})\n{sanctions_report}"],
                }
            )
            continue

        queue.append(supplier)

    state["queue"] = queue
    state["evidence"] = evidence
    state["verdicts"] = verdicts
    state["skipped"] = []
    state["sanctions"] = sanctions
    state["all_suppliers"] = [supplier.model_dump() for supplier in plan.suppliers]
    state["current_supplier"] = None
    state["current_evidence"] = []
    state["memo"] = None
    state["plan_summary"] = plan.budget_strategy
    return state


def screen(state: State) -> State:
    supplier = state["queue"].pop(0)
    sanctions_report = state["sanctions"].get(supplier["name"], NO_SANCTIONS_MATCH)
    screen_state: ScreenState = {
        "supplier": supplier,
        "sanctions_report": sanctions_report,
        "observations": [],
        "action": None,
        "done": False,
        "tool_steps": 0,
    }
    result = _get_supplier_screen_graph().invoke(screen_state)

    current_evidence = [f"sanctions_screen({supplier['name']})\n{sanctions_report}"]
    current_evidence.extend(result["observations"])

    state["current_supplier"] = supplier
    state["current_evidence"] = current_evidence
    state["evidence"].append({"supplier": supplier["name"], "items": current_evidence})
    return state


def _fallback_verdict(supplier: dict, evidence: str) -> Verdict:
    if _best_sanctions_score(evidence) >= SANCTIONS_REJECT_THRESHOLD:
        return Verdict(
            supplier=supplier["name"],
            verdict="REJECT",
            reason="High-confidence sanctions similarity at or above the configured threshold.",
            action_required="Do not release payment. Escalate to Legal.",
        )
    if LOOKUP_UNAVAILABLE in evidence:
        return Verdict(
            supplier=supplier["name"],
            verdict="INSUFFICIENT",
            reason="A required evidence source was unavailable after retry.",
            action_required="Re-run unavailable lookup and collect current company registration proof.",
        )
    return Verdict(
        supplier=supplier["name"],
        verdict="INSUFFICIENT",
        reason="Model output failed twice, so this supplier was not auto-classified safely.",
        action_required="Manually review evidence and confirm LEI or registration number before payment.",
    )


def decide(state: State) -> State:
    supplier = state["current_supplier"]
    if not supplier:
        return state

    evidence_text = "\n\n".join(state["current_evidence"]) or "(no evidence)"
    prompt = (
        "Write a finance-facing supplier decision.\n"
        "You must return exactly one of APPROVE, CONDITIONS, REJECT, or INSUFFICIENT.\n"
        "Rules you must follow:\n"
        f"1. Treat sanctions similarity below {SANCTIONS_REJECT_THRESHOLD:.2f} as a fuzzy lead, not a sanctions verdict, unless other evidence confirms it.\n"
        f"2. Treat sanctions similarity at or above {SANCTIONS_REJECT_THRESHOLD:.2f} as REJECT.\n"
        "3. NO_RECORDS from GLEIF means no LEI under that typed name, not that the company is fake.\n"
        "4. If GLEIF returns several candidates and zero exact matches, use CONDITIONS and ask for the LEI or registration number from Procurement.\n"
        "5. If GLEIF returns one exact match with an active entity and ISSUED registration, APPROVE is usually appropriate.\n"
        "6. If the entity exists but registration is LAPSED, RETIRED, or otherwise not current, use CONDITIONS and ask for current registration evidence before payment.\n"
        "7. If GLEIF has no record but web search shows a real company, use CONDITIONS and ask for documentary proof of the legal entity.\n"
        "8. If GLEIF has no record and web search finds no credible trace of a company, REJECT.\n"
        "9. If evidence is conflicting or a tool was unavailable and that blocks a safe decision, use INSUFFICIENT and name the exact document or check needed.\n"
        "10. CONDITIONS and INSUFFICIENT must include a concrete action_required.\n\n"
        f"SUPPLIER: {supplier['name']}\n"
        f"CATEGORY: {supplier['category']}\n"
        f"JURISDICTION: {supplier['jurisdiction']}\n\n"
        f"EVIDENCE:\n{evidence_text}"
    )

    verdict = _invoke_structured(
        Verdict,
        prompt,
        lambda: _fallback_verdict(supplier, evidence_text),
    )

    state["verdicts"].append(verdict.model_dump())
    state["current_supplier"] = None
    state["current_evidence"] = []
    return state


def budget_left(state: State) -> str:
    if state["queue"] and BUDGET["used"] < MAX_LOOKUPS:
        return "next"
    return "memo"


def write_memo(state: State) -> State:
    order = {"REJECT": 0, "CONDITIONS": 1, "APPROVE": 2, "INSUFFICIENT": 3}
    verdicts = sorted(state["verdicts"], key=lambda item: (order[item["verdict"]], item["supplier"]))
    state["skipped"] = list(state["queue"])

    lines = [
        f"SUPPLIER REVIEW - Thursday payment run - {len(state['all_suppliers'])} suppliers - {BUDGET['used']} lookups used",
        "",
        f"Budget strategy: {state['plan_summary']}",
        "",
    ]

    for verdict in verdicts:
        lines.append(f"  {verdict['verdict']:<12}{verdict['supplier']}")
        lines.append(f"                {verdict['reason']}")
        if verdict.get("action_required"):
            lines.append(f"                -> {verdict['action_required']}")
        lines.append("")

    if state["skipped"]:
        lines.append("  NOT CHECKED (budget)")
        for supplier in state["skipped"]:
            lines.append(
                f"                {supplier['name']} - {supplier['jurisdiction']}; {supplier['priority_reason']}"
            )
            lines.append(
                "                Residual risk: sanctions screened only; legal-entity evidence not established."
            )
        lines.append("")

    state["memo"] = "\n".join(lines).rstrip()
    return state


def build_graph():
    graph = StateGraph(State)
    graph.add_node("triage", triage)
    graph.add_node("screen", screen)
    graph.add_node("decide", decide)
    graph.add_node("memo", write_memo)

    graph.set_entry_point("triage")
    graph.add_conditional_edges("triage", budget_left, {"next": "screen", "memo": "memo"})
    graph.add_edge("screen", "decide")
    graph.add_conditional_edges("decide", budget_left, {"next": "screen", "memo": "memo"})
    graph.add_edge("memo", END)
    return graph.compile()


if __name__ == "__main__":
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise SystemExit("OPENROUTER_API_KEY missing - add it to bridge-project/.env")

    if not os.path.exists(os.path.join(_PROJECT_ROOT, "REQUEST.md")):
        raise SystemExit("REQUEST.md not found in bridge-project/")

    BUDGET["used"] = 0
    app = build_graph()
    result = app.invoke(
        {
            "request": _read_request(),
            "queue": [],
            "evidence": [],
            "verdicts": [],
            "skipped": [],
            "sanctions": {},
            "all_suppliers": [],
            "current_supplier": None,
            "current_evidence": [],
            "memo": None,
            "plan_summary": "",
        }
    )
    print(result["memo"])
