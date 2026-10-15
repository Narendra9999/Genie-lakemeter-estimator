"""Thin adapter between the DCP calculator (notebooks/dcp_estimator.py) and Unity Catalog / Genie.

It does no math. It only:
  * builds the EstimateRequest: a preset ({"preset": "SEND_US01", "preset_args": {...}}) or a plain request JSON,
  * applies what-if overrides: preset keyword arguments (e.g. {"migration_gb": 8000}) and/or dotted paths on the
    request (e.g. {"transfer.daily_gb_high": -1}, {"processing[C02].runs_per_month": 4}),
  * builds the calculator's RegistryPricingProvider from the rate card (dcp_rate_card) - nothing else,
  * calls estimate() and returns result.to_dict() reshaped into lists for SQL, plus format_summary().
Validation errors (ValueError) come back as status REJECTED with the calculator's own messages.
"""
import json

DISCLAIMER = "Internal planning estimate, not an invoice or committed run rate."
# "Core monthly" in the DCP output format: Databricks processing + SQL + recurring DataSync (no S3/KMS, no model API)
CORE_ENGINES = ("DATA_PROCESSING", "SQL_REPORTING", "DATA_TRANSFER")


def _apply_paths(d, paths):
    for path, val in paths.items():
        head, _, field = path.rpartition(".")
        if not head:
            d[field] = val
        elif head.startswith("processing[") and head.endswith("]"):
            cid = head[len("processing["):-1]
            hits = [c for c in d.get("processing") or [] if c.get("component_id") == cid or str(c.get("component_id", "")).endswith("-" + cid)]
            if not hits:
                raise ValueError(f"Override {path}: no processing component {cid!r}.")
            for c in hits:
                c[field] = val
        else:
            if d.get(head) is None:
                d[head] = {}
            if not isinstance(d[head], dict):
                raise ValueError(f"Override {path}: {head} is not an object.")
            d[head][field] = val
    return d


def build_request(m, request, overrides=None):
    overrides = dict(overrides or {})
    paths = {k: v for k, v in overrides.items() if "." in k or "[" in k}
    kwargs = {k: v for k, v in overrides.items() if k not in paths}
    if "preset" in request:
        args = {**(request.get("preset_args") or {}), **kwargs}
        req = m.load_preset(request["preset"], **args)
        if not paths:
            return req
        d = req.to_dict()
    else:
        if kwargs:
            paths.update(kwargs)
        d = json.loads(json.dumps(request))
    return m.EstimateRequest.from_dict(_apply_paths(d, paths))


def build_pricing(m, rates):
    """rates = {"as_of": "YYYY-MM-DD", "source": ..., "rows": [RateRecord fields...]}. Only these rows are used."""
    fields = {"provider", "service", "sku", "unit", "region", "rate", "status", "source",
              "effective_start", "effective_end", "observed_rate", "notes"}
    records = []
    for r in (rates or {}).get("rows") or []:
        rec = {k: r.get(k) for k in fields if r.get(k) is not None or k in ("rate", "effective_end", "observed_rate")}
        rec.setdefault("notes", "")
        rec.setdefault("effective_start", "2026-01-01")
        records.append(m.RateRecord(**rec))
    as_of = (rates or {}).get("as_of") or m.DEFAULT_AS_OF
    return m.RegistryPricingProvider(rates=records, conversions=m.DEFAULT_MODEL_CONVERSIONS, as_of=as_of)


def _split(s):
    cid, _, rest = s.partition(": ")
    return {"component_id": cid, "reason": rest or s}


def shape(m, result, rates):
    d = result.to_dict()
    totals = []
    c2 = lambda x: None if x is None else float(f"{x:.2f}")   # same rounding as the calculator's format_summary
    for sc in d["scenarios"]:
        t = d["totals"][sc]
        core = result.cost_by_component(sc, engines=CORE_ENGINES)   # calculator's own slice: processing + SQL + DataSync
        totals.append({"scenario": sc, "label": "STRESS (operating sensitivity, not a forecast)" if sc == "STRESS" else sc,
                       "recurring_monthly_usd": c2(t["recurring_monthly_usd"]), "core_monthly_usd": c2(core),
                       "annual_usd": c2(t["annual_recurring_usd"]), "one_time_usd": c2(t["one_time_usd"]),
                       "first_year_usd": c2(t["first_year_usd"]), "databricks_dbu": c2(t["databricks_dbu_monthly"]),
                       "databricks_usd": c2(t["databricks_monthly_usd"]), "aws_usd": c2(t["aws_monthly_usd"]),
                       "pending_items": t["pending_recurring_items"] + t["pending_one_time_items"], "status": t["status"]})
    lines = [{**li, "warnings": "; ".join(li["warnings"])} for li in d["line_items"]]
    decisions = [{**x, "eligible": ", ".join(x["eligible"])} for x in d["compute_decisions"]]
    used = sorted({f"{li['rate_status']} ${li['rate']:g}/{li['unit']} - {li['rate_source']}" for li in d["line_items"] if li["rate"] is not None})
    return {"status": "OK", "errors": [],
            "header": {"calculator_version": d["calculator_version"], "use_case_id": d["use_case_id"], "use_case_name": d["use_case_name"],
                       "generated_at": d["generated_at"], "rate_card_source": (rates or {}).get("source"),
                       "rate_card_as_of": (rates or {}).get("as_of"), "calibration_used": d["calibration_used"]},
            "totals": totals, "line_items": lines, "compute_decisions": decisions,
            "confidence": d["overall_confidence"],
            "pending": [_split(s) for s in d["missing_inputs"]],
            "assumptions": d["assumptions_used"], "warnings": d["warnings"],
            "scope": {"included": d["scope_included"], "excluded": d["scope_excluded"], "rates_used": used},
            "notes": d["notes"], "summary": m.format_summary(result), "disclaimer": DISCLAIMER}


def run(m, request, rates, overrides=None):
    """Entry point used by the Unity Catalog function. Always returns JSON text."""
    try:
        req = json.loads(request) if isinstance(request, str) else request
        rt = json.loads(rates) if isinstance(rates, str) and rates else (rates or {})
        ov = json.loads(overrides) if isinstance(overrides, str) and overrides else (overrides or {})
        if not isinstance(req, dict):
            raise ValueError(f"Request must be a JSON object, got {type(req).__name__}.")
        if not isinstance(ov, dict):
            raise ValueError("Overrides must be a JSON object of field: value pairs.")
        result = m.estimate(build_request(m, req, ov), build_pricing(m, rt))
        return json.dumps(shape(m, result, rt), default=str)
    except (ValueError, TypeError) as e:
        msgs = [x.strip(" -") for x in str(e).replace("Invalid estimate request:", "").split("\n") if x.strip(" -")]
        return json.dumps({"status": "REJECTED", "errors": msgs, "totals": [], "line_items": [], "compute_decisions": [],
                           "confidence": None, "pending": [], "assumptions": [], "warnings": [], "scope": None, "notes": [],
                           "header": {"calculator_version": getattr(m, "CALCULATOR_VERSION", None)}, "summary": None,
                           "disclaimer": DISCLAIMER})
