"""Reconcile a presented estimate against the current DCP Smart Estimation Calculator.

What it does
  1. Loads a request JSON built from the team's intake and email answers.
  2. Runs it through the current calculator (estimate()).
  3. Compares every presented figure stored in metadata.presented and prints MATCH or DIFFERS.
  4. Confirms the JSON inputs equal the built-in AI_RAG_MODEL_API preset (input drift check).

Run
  python reconcile_ai_rag.py                                   (uses ai_rag_model_api_request.json)
  python reconcile_ai_rag.py path/to/other_request.json
In Databricks: put this file next to dcp_estimator.py and run
  import reconcile_ai_rag as rc; rc.main("/Workspace/.../ai_rag_model_api_request.json")

Nothing is written anywhere; the script only reads and prints.
"""
import json
import os
import sys

import dcp_estimator as dcp

TOLERANCE_USD = 0.01


def _row(label, presented, calculated):
    if presented == "PENDING" or calculated is None:
        ok = presented == "PENDING" and calculated is None
        status = "MATCH (pending)" if ok else "DIFFERS"
        return ok, f"  {label:<44} presented {str(presented):>10} | calculated {str(calculated):>10} | {status}"
    ok = abs(calculated - presented) < TOLERANCE_USD
    status = "MATCH" if ok else f"DIFFERS BY ${calculated - presented:,.2f}"
    return ok, f"  {label:<44} presented ${presented:>9,.2f} | calculated ${calculated:>9,.2f} | {status}"


IGNORED_FOR_DRIFT = ("evidence",)  # descriptive text only; does not affect cost


def _strip(obj):
    """Drop comment keys (starting with _) and descriptive-only fields at every level."""
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if not k.startswith("_") and k not in IGNORED_FOR_DRIFT}
    if isinstance(obj, list):
        return [_strip(x) for x in obj]
    return obj


def reconcile(path):
    with open(path) as f:
        raw = f.read()
    request = dcp.EstimateRequest.from_json(raw)
    result = dcp.estimate(request)
    ref = request.metadata.get("presented", {})
    rows, all_ok = [], True

    def add(label, presented, calculated):
        nonlocal all_ok
        ok, line = _row(label, presented, calculated)
        all_ok &= ok
        rows.append(line)

    print(f"Calculator v{dcp.CALCULATOR_VERSION} | request {request.use_case_id} | {path}")
    print(f"Presented basis: {ref.get('source', 'not recorded')}\n")

    rows.append("Recurring monthly")
    for sc, v in ref.get("recurring_monthly_usd", {}).items():
        add(f"{sc}", v, result.totals[sc].recurring_monthly_usd)

    rows.append("One-time (full-corpus indexing)")
    for sc, v in ref.get("one_time_usd", {}).items():
        add(f"{sc}", v, result.totals[sc].one_time_usd)

    rows.append("Expected monthly by component")
    for cid, v in ref.get("expected_by_component_usd", {}).items():
        add(cid, v, result.cost_by_component("EXPECTED", component_ids=(cid,)))

    alts = ref.get("final_answer_alternative_totals_usd", {})
    if alts:
        rows.append("Final-answer alternatives (full recurring total; mutually exclusive)")
        final = next(e for e in request.model_endpoints if e.component_id == "MD-AI-C04")
        compare = {r["model"]: r for r in dcp.compare_models(final, list(alts), request.model_days_per_month,
                                                             request.region)}
        for model, presented in alts.items():
            for sc in ("LOW", "EXPECTED", "HIGH"):
                swap = compare[model][f"{sc.lower()}_usd"]
                calc = (None if swap is None else
                        result.totals[sc].recurring_monthly_usd
                        - result.cost_by_component(sc, component_ids=("MD-AI-C04",)) + swap)
                add(f"{model} {sc}", presented if presented == "PENDING" else presented[sc], calc)

    if "overall_confidence" in ref:
        conf = result.overall_confidence["rating"]
        ok = conf == ref["overall_confidence"]
        rows.append(f"Confidence: presented {ref['overall_confidence']} | calculated {conf} | "
                    f"{'MATCH' if ok else 'DIFFERS (see note)'}")

    print("\n".join(rows))

    # Input drift check against the built-in preset.
    if request.use_case_id in dcp.PRESETS:
        preset = dcp.load_preset(request.use_case_id).to_dict()
        mine = _strip(json.loads(raw))
        drift = [k for k in ("model_endpoints", "one_time_model_jobs", "model_days_per_month", "scenarios", "region")
                 if _strip(preset.get(k)) != mine.get(k, preset.get(k))]
        print(f"\nInput check vs built-in preset {request.use_case_id}: "
              f"{'SAME INPUTS' if not drift else 'DIFFERENT: ' + ', '.join(drift)}")

    pending = [m for m in result.missing_inputs]
    if pending:
        print("\nPending (not counted, not zero):")
        for m in pending:
            print(f"  - {m}")
    print(f"\nRESULT: {'ALL PRESENTED FIGURES REPRODUCE' if all_ok else 'ONE OR MORE FIGURES DIFFER - review above'}")
    return all_ok, result


def main(path=None):
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_rag_model_api_request.json")
    ok, _ = reconcile(path)
    return ok


if __name__ == "__main__":
    sys.exit(0 if main(sys.argv[1] if len(sys.argv) > 1 else None) else 1)
