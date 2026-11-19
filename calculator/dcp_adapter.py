"""Thin adapter between the DCP calculator (notebooks/dcp_estimator.py) and Unity Catalog / Genie.

It does no math. It only:
  * builds the EstimateRequest: a preset ({"preset": "SEND_US01", "preset_args": {...}}) or a plain request JSON,
  * applies what-if overrides: preset keyword arguments (e.g. {"migration_gb": 8000}) and/or dotted paths on the
    request (e.g. {"transfer.daily_gb_high": -1}, {"processing[C02].runs_per_month": 4}),
  * builds the calculator's RegistryPricingProvider from dcp_rate_card (Databricks) and dated aws_pricing_list rows,
  * calls estimate() and returns result.to_dict() reshaped into lists for SQL, plus format_summary().
Validation errors (ValueError) come back as status REJECTED with the calculator's own messages.
"""
import json
from dataclasses import MISSING
from aws_pricing import apply_aws_rates

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


# Baseline quantities are planning assumptions, not observations. Explicit nulls,
# invalid values, model/workload choices and scope are deliberately never filled.
REQUIRED_DEFAULTS = {
    "processing": {"runtime_hours_low": 1, "runtime_hours_expected": 2,
                   "runtime_hours_high": 3, "runs_per_month": 30},
    "sql": {"concurrent_users": 10},
    "transfer": {"daily_gb_low": 5, "daily_gb_expected": 10, "daily_gb_high": 15},
    "storage": {"retained_tb": 2, "files_per_day": 100, "s3_read_requests_per_file": 1},
    "model_endpoints": {"calls_per_day": 100, "input_tokens_low": 500,
                        "input_tokens_expected": 1000, "input_tokens_high": 1500,
                        "output_tokens_low": 100, "output_tokens_expected": 200, "output_tokens_high": 300},
    "one_time_model_jobs": {"input_tokens_low": 500, "input_tokens_high": 1500,
                            "output_tokens_low": 100, "output_tokens_high": 300},
}
IDENTITY_DEFAULTS = {"processing": "Processing", "sql": "SQL",
                     "model_endpoints": "Model", "one_time_model_jobs": "ModelJob"}


def expand_model_planning(m, request):
    """Expand an explicit, versioned reference scenario; never change pricing math.

    Daily API calls are a TOTAL across reference roles, not user requests.
    The model/corpus choices are hypothetical, never measured workload facts.
    """
    d = json.loads(json.dumps(request))
    if not isinstance(d, dict) or "_model_planning" not in d:
        return d, []
    plan = d["_model_planning"]
    if d.get("_first_estimate") is not True or "preset" in d:
        raise ValueError("Model planning requires _first_estimate=true and cannot wrap a preset.")
    if not isinstance(plan, dict) or plan.get("profile") != "RAG_REFERENCE_V1":
        raise ValueError("Unknown model planning profile; use RAG_REFERENCE_V1.")
    if set(plan) - {"profile", "api_calls_per_day", "include_initial_indexing"}:
        raise ValueError("Unsupported model planning fields.")
    calls = plan.get("api_calls_per_day")
    import math
    if isinstance(calls, bool) or not isinstance(calls, (int, float)) or not math.isfinite(calls) or calls < 0:
        raise ValueError("api_calls_per_day must be a finite non-negative number.")
    indexing = plan.get("include_initial_indexing", False)
    if not isinstance(indexing, bool):
        raise ValueError("include_initial_indexing must be true or false.")
    if d.get("model_endpoints") or d.get("one_time_model_jobs"):
        raise ValueError("Do not combine a reference model scenario with supplied model components; use explicit components instead.")
    states = d.get("_areas") or {}
    if not isinstance(states, dict):
        raise ValueError("_areas must be an object.")
    for area in (["model_endpoints", "one_time_model_jobs"] if indexing else ["model_endpoints"]):
        if states.get(area) in ("EXCLUDED", "NOT_APPLICABLE"):
            raise ValueError("Reference planning conflicts with explicit scope for " + area)
    reference = m.preset_ai_rag_model_api().to_dict()
    endpoints = reference["model_endpoints"]
    reference_calls = sum(item["calls_per_day"] for item in endpoints)
    # Normalize the source role allocation to the supplied total model API calls.
    for item in endpoints:
        item["calls_per_day"] = calls * item["calls_per_day"] / reference_calls
    allocation = "reference weights 330:300:215:132 normalized from 977 total API calls; recurring query embeddings/enrichment are separate from initial full-corpus indexing"
    for item in endpoints:
        item.pop("component_id", None)
    d["model_endpoints"] = endpoints
    if indexing:
        d["one_time_model_jobs"] = reference["one_time_model_jobs"]
        for item in d["one_time_model_jobs"]:
            item.pop("component_id", None)
    audit = [{"field": "_model_planning.profile", "value": {
        "profile": plan["profile"], "basis": "hypothetical source-calculator RAG reference, not measured production usage",
        "api_calls_per_day_total": calls,
        "allocation": allocation,
        "models_and_tokens": endpoints,
        "initial_indexing": d.get("one_time_model_jobs", []),
        "indexing_basis": "independent hypothetical full-corpus tokens; never derived from daily calls" if indexing else "not estimated"
    }, "affects_cost": True}]
    return d, audit


def prepare_defaults(m, request):
    """Return a copy and an audit trail. Only omitted fields are defaultable.

    Apply documented quantity baselines to omitted fields only. Scope is never defaulted.
    Generated identifiers avoid every supplied identifier across all engines.
    """
    d, applied = expand_model_planning(m, request)
    if not isinstance(d, dict) or "preset" in d:
        return d, applied
    def put(obj, key, value, path, quantity=True):
        if key not in obj or (not quantity and (obj[key] is None or isinstance(obj[key], str) and not obj[key].strip())):
            obj[key] = value
            applied.append({"field": path + key, "value": value, "affects_cost": quantity})
    if d.get("_first_estimate") is True:
        # Unknown scope is retained, never silently changed to not applicable.
        raw = d.get("_areas")
        if raw is None:
            raw = d["_areas"] = {}
        if isinstance(raw, dict):
            for area in AREAS:
                if area not in raw and not _has(d.get(area)):
                    raw[area] = "UNKNOWN"
    put(d, "use_case_id", "ADHOC", "", False)
    put(d, "use_case_name", "New workload", "", False)
    used = set()
    for area, _, many in SECTIONS:
        values = d.get(area)
        for item in (values if isinstance(values, list) else [values]):
            if isinstance(item, dict) and isinstance(item.get("component_id"), str):
                used.add(item["component_id"])
    for area, cls, many in SECTIONS:
        values = d.get(area)
        if values is None:
            continue
        items = values if many and isinstance(values, list) else [values] if not many and isinstance(values, dict) else []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            path = f"{area}[{index}]." if many else area + "."
            if area in IDENTITY_DEFAULTS:
                prefix = IDENTITY_DEFAULTS[area]
                number = index + 1
                while f"{prefix.upper()}-{number:02d}" in used:
                    number += 1
                put(item, "component_id", f"{prefix.upper()}-{number:02d}", path, False)
                if isinstance(item.get("component_id"), str):
                    used.add(item["component_id"])
                label = "role" if area == "model_endpoints" else "description" if area == "one_time_model_jobs" else "name"
                put(item, label, f"{prefix} {index + 1}", path, False)
            # Read non-null numeric defaults from the source dataclass first.
            # None denotes an unknown in the core, not a usable planning quantity.
            core_fields = {f.name: f for f in m.fields(getattr(m, cls))}
            for field, definition in core_fields.items():
                value = definition.default
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    put(item, field, value, path)
            original = dict(item)
            defaults = REQUIRED_DEFAULTS[area]
            for field, value in defaults.items():
                if area == "processing":
                    workload = item.get("workload_type")
                    if workload not in m.WORKLOAD_TYPES or workload == "CONTINUOUS_STREAMING":
                        continue
                    if field == "runs_per_month" and workload == "EVENT_DRIVEN_BATCH":
                        continue  # preserve the core's existing event-driven scenario defaults
                if area == "model_endpoints" and field.endswith("_expected"):
                    base = field.removesuffix("_expected")
                    if original.get(base + "_low") is not None and original.get(base + "_high") is not None:
                        continue  # retain the core's midpoint-of-supplied-token-range behavior
                definition = core_fields[field]
                if definition.default is not MISSING and definition.default is not None:
                    value = definition.default
                # Explicit unknowns and supplied scenario values remain unchanged.
                put(item, field, value, path)
    if d.get("model_endpoints"):
        put(d, "model_days_per_month", m.EstimateRequest.__dataclass_fields__["model_days_per_month"].default, "")
    # Engine planning defaults stay in PlanningAssumptions, so source upgrades and
    # user assumption overrides continue to govern calculation without duplication.
    assumptions = d.get("assumptions") or {}
    if isinstance(assumptions, dict):
        core = m.PlanningAssumptions()
        keys = set()
        scenarios = d.get("scenarios") or ["LOW", "EXPECTED", "HIGH"]
        for item in d.get("processing") or []:
            if not isinstance(item, dict):
                continue
            streaming = item.get("workload_type") == "CONTINUOUS_STREAMING"
            if streaming:
                keys.add("continuous_hours_per_month")
            fixed = item.get("dbu_per_hour")
            if not isinstance(fixed, dict) or any(sc not in fixed for sc in scenarios):
                keys.add("continuous_dbu_per_hour" if streaming else "batch_dbu_per_hour")
            if item.get("workload_type") == "EVENT_DRIVEN_BATCH" and item.get("runs_per_month") is None:
                keys.add("unconfirmed_event_runs_per_month")
        if d.get("sql") is not None:
            keys.update(("sql_active_hours_per_day", "sql_concurrency_factor", "sql_dbu_per_hour_per_band", "sql_users_per_capacity_band"))
        if d.get("model_endpoints") and "STRESS" in scenarios:
            keys.add("model_stress_call_multiplier")
        for key in sorted(keys):
            if key not in assumptions:
                applied.append({"field": "assumptions." + key, "value": getattr(core, key), "affects_cost": True})
    return d, applied


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
    return m.EstimateRequest.from_dict(prepare_defaults(m, _apply_paths(d, paths))[0])


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


def shape(m, result, rates, areas=None):
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
            "areas": areas or [], "notes": d["notes"], "summary": m.format_summary(result), "disclaimer": DISCLAIMER}


def run(m, request, rates, overrides=None, named=None):
    """Entry point used by the Unity Catalog function. Always returns JSON text.
    named = True / False runs the input check first (named use case / ad-hoc or pasted JSON): if anything required is
    missing or invalid, nothing is priced and status NEEDS_INPUT lists each question. named = None skips the check."""
    if named is not None:
        blocking = [r for r in check(m, request, overrides, named) if r["blocking"]]
        if blocking:
            areas = next((json.loads(r["received"]) for r in blocking if r["issue"] == "AREA_UNCONFIRMED"), None)
            step = ("When the user answers, make the SAME call again with every earlier value plus the answers - never write a request JSON yourself. "
                    "Missing values: add them as dcp_request arguments (or, for a pasted JSON kept verbatim, as p_overrides dotted paths). ")
            if areas:
                try:
                    rq = json.loads(request) if isinstance(request, str) else dict(request)
                    ov = json.loads(overrides) if isinstance(overrides, str) and overrides else dict(overrides or {})
                    ov.update({f"_areas.{x}": "NOT_APPLICABLE" for x in areas.split(",")})
                    lit = lambda o: json.dumps(o, separators=(",", ":")).replace("'", "''")
                    step += ("If the user says the other / remaining areas are not applicable, run exactly: SELECT * FROM <SCHEMA>.dcp_calc_totals("
                             f"p_request => '{lit(rq)}', p_overrides => '{lit(ov)}') - and add any other values the user gave as dotted p_overrides "
                             "keys (e.g. \"processing[ADHOC-C01].runs_per_month\": 30, \"storage.s3_read_requests_per_file\": 2). Otherwise: ")
                except (ValueError, TypeError):
                    pass
                step += (f"Area states for {areas}: dcp_request(... same arguments ..., p_not_applicable_areas => '<areas>', p_excluded_areas => '<areas>', "
                         f"p_unknown_areas => '<areas>') using only the areas the user named; 'the other areas are not applicable' = "
                         f"p_not_applicable_areas => '{areas}'. For a pasted JSON: p_overrides => '{{\"_areas.<area>\": \"NOT_APPLICABLE\"}}'.")
            return json.dumps({"status": "NEEDS_INPUT", "next_step": step, "totals": [], "line_items": [], "compute_decisions": [], "confidence": None,
                               "errors": [f"{r['issue']} {r['field']}" + (f" ({r['component_id']})" if r["component_id"] else "")
                                          + (f", received {r['received']}" if r["received"] is not None else "") + f": {r['question']}"
                                          for r in blocking],
                               "pending": [], "assumptions": [], "warnings": [], "scope": None, "notes": [],
                               "header": {"calculator_version": getattr(m, "CALCULATOR_VERSION", None)}, "summary": None,
                               "disclaimer": DISCLAIMER})
    try:
        req = json.loads(request) if isinstance(request, str) else request
        rt = json.loads(rates) if isinstance(rates, str) and rates else (rates or {})
        rt = apply_aws_rates(req, rt)
        ov = json.loads(overrides) if isinstance(overrides, str) and overrides else (overrides or {})
        if not isinstance(req, dict):
            raise ValueError(f"Request must be a JSON object, got {type(req).__name__}.")
        if not isinstance(ov, dict):
            raise ValueError("Overrides must be a JSON object of field: value pairs.")
        built = build_request(m, req, ov)
        result = m.estimate(built, build_pricing(m, rt))
        _, defaults = prepare_defaults(m, _apply_paths(json.loads(json.dumps(req)), ov)) if "preset" not in req else (req, [])
        if defaults:
            result.assumptions_used.extend("Default applied: " + row["field"] + " = " + json.dumps(row["value"]) for row in defaults)
        cost_defaults = [row for row in defaults if row["affects_cost"]]
        if cost_defaults:
            result.overall_confidence = {**result.overall_confidence, "rating": "LOW",
                "reason": "LOW confidence: omitted inputs used calculator or supplemental planning defaults: " +
                          "; ".join(row["field"] + " = " + json.dumps(row["value"]) for row in cost_defaults) +
                          ". Replace these defaults with measured workload values for a more reliable estimate."}
        prepared, _ = prepare_defaults(m, _apply_paths(json.loads(json.dumps(req)), ov))
        output = shape(m, result, rt, area_states(built, prepared.get("_areas"), named))
        if req.get("_first_estimate") is True:
            unknown = [row["area"] for row in output["areas"] if row["state"] in ("UNKNOWN", "UNCONFIRMED")]
            pending = [{"component_id": area, "reason": "Scope unresolved; not estimated or counted as zero."} for area in unknown]
            metadata = req.get("metadata") or {}
            deferred = metadata.get("deferred_inputs", []) if isinstance(metadata, dict) else []
            if isinstance(deferred, list):
                for index, item in enumerate(deferred):
                    if isinstance(item, dict):
                        pending.append({"component_id": str(item.get("component_id") or item.get("area") or f"deferred-{index+1}"),
                                        "reason": "Deferred; not estimated: " + str(item.get("reason") or "needs clarification")})
            output["notes"].insert(0, "FIRST ESTIMATE: provisional estimate for priced areas; see pending scope and assumptions.")
            if pending:
                reason = "First estimate is incomplete. Not estimated: " + "; ".join(row["component_id"] + ": " + row["reason"] for row in pending)
                output["warnings"].append(reason)
                output["pending"].extend(pending)
                for total in output["totals"]:
                    total["pending_items"] += len(pending)
                    total["status"] = "PARTIAL_PENDING_INPUTS"
                output["confidence"] = {**output["confidence"], "rating": "LOW",
                                        "reason": reason + " " + output["confidence"]["reason"]}
            output["summary"] = "FIRST ESTIMATE — provisional priced-area subtotal.\n" + output["summary"]
            if pending:
                output["summary"] += "\n" + reason
        return json.dumps(output, default=str)
    except (ValueError, TypeError) as e:
        msgs = [x.strip(" -") for x in str(e).replace("Invalid estimate request:", "").split("\n") if x.strip(" -")]
        return json.dumps({"status": "REJECTED", "errors": msgs, "totals": [], "line_items": [], "compute_decisions": [],
                           "confidence": None, "pending": [], "assumptions": [], "warnings": [], "scope": None, "notes": [],
                           "header": {"calculator_version": getattr(m, "CALCULATOR_VERSION", None)}, "summary": None,
                           "disclaimer": DISCLAIMER})


# ---- input check: run BEFORE estimating, so missing or mistyped inputs are asked for instead of priced as PENDING ----
# REQUIRED = no calculator default exists, so the cost would come back PENDING (or the request would be rejected).
# Omitted required quantities receive the explicit planning baselines above before checks.
# Supplied values (including null) and scope are preserved. No pricing math is performed here.

SECTIONS = (("processing", "ProcessingComponent", True), ("sql", "SqlReporting", False),
            ("transfer", "DataTransfer", False), ("storage", "AwsStorage", False),
            ("model_endpoints", "ModelEndpoint", True), ("one_time_model_jobs", "OneTimeModelJob", True))
STREAMING_MODES = ("TRUE_CONTINUOUS_LOW_LATENCY", "INCREMENTAL_AVAILABLE_NOW", "UNCONFIRMED")
LATENCIES = ("SECONDS", "MINUTES_OR_MORE", "UNCONFIRMED")
QUESTIONS = {
    "use_case_id": "What ID should this use case have (for example NEW_WORKLOAD_01)?",
    "use_case_name": "What is the business name of this use case?",
    "workload_type": "Which workload type is it: CONTINUOUS_STREAMING, EVENT_DRIVEN_BATCH, SCHEDULED_BATCH, ML_TRAINING or BATCH_SCORING?",
    "runtime_hours_expected": "How many hours does each run take (optionally a low and high)?",
    "runtime_hours_low": "What is the shortest runtime per run, in hours?",
    "runtime_hours_high": "What is the longest runtime per run, in hours?",
    "runs_per_month": "How many runs per month?",
    "concurrent_users": "How many concurrent SQL users are there?",
    "daily_gb_expected": "How many GB move through DataSync per day (0 if there is only a one-time migration)?",
    "daily_gb_low": "What is the lowest daily DataSync volume, in GB?",
    "daily_gb_high": "What is the highest daily DataSync volume, in GB?",
    "retained_tb": "How many TB are retained in S3?",
    "files_per_day": "How many files are written to S3 per day?",
    "s3_read_requests_per_file": "How many times is each file read?",
    "s3_write_requests_per_file": "How many S3 write requests are made per file?",
    "kms_requests_per_encrypted_file": "How many KMS requests are made per encrypted file?",
    "model": "Which model is called?",
    "calls_per_day": "How many model calls are made per day?",
    "input_tokens_expected": "How many input tokens per call (or a low and high)?",
    "output_tokens_expected": "How many output tokens per call (or a low and high)?",
    "input_tokens_low": "What is the low input token count?",
    "input_tokens_high": "What is the high input token count?",
    "output_tokens_low": "What is the low output token count?",
    "output_tokens_high": "What is the high output token count?",
}
# Guided intake: every field a technician can be asked about, per area (internal fields such as evidence are left out).
# REQUIRED = no default (the cost would be PENDING); DEFAULT = the calculator applies a planning default - show it.
INTAKE_REQUIRED = {"processing": ("workload_type", "runtime_hours_expected", "runs_per_month"), "sql": ("concurrent_users",),
                   "transfer": ("daily_gb_expected",), "storage": ("retained_tb", "files_per_day", "s3_read_requests_per_file"),
                   "model_endpoints": ("model", "calls_per_day", "input_tokens_expected", "output_tokens_expected"),
                   "one_time_model_jobs": ("model", "input_tokens_low", "input_tokens_high", "output_tokens_low", "output_tokens_high")}
INTAKE_SKIP = {"component_id", "name", "evidence", "dbu_per_hour_basis", "dbu_per_hour_evidence", "role", "description"}
QUESTIONS.update({
    "quantity": "How many identical copies of this workload run?", "pattern": "What is the run pattern: CONTINUOUS, SCHEDULED or AS_NEEDED?",
    "dbu_per_hour": "Is the DBU per hour known (for example from a test run)?", "requires_gpu": "Does it need GPUs?",
    "requires_custom_runtime": "Does it need a custom runtime?", "requires_custom_networking": "Does it need custom networking?",
    "declarative_pipeline_preferred": "Should it run as a declarative (Lakeflow) pipeline?",
    "streaming_mode": "Is the streaming truly continuous low-latency, or incremental (available-now)?",
    "latency_requirement": "What latency is needed: seconds, or minutes or more?",
    "working_days_per_month": "How many working days per month are the reports used?", "total_users": "How many users in total?",
    "transfer_days_per_month": "On how many days per month does data move?",
    "initial_transfer_gb": "Is there a one-time migration, and how many GB?",
    "encrypted_files_per_day": "How many files per day are encrypted with KMS?", "kms_keys": "How many customer-managed KMS keys?",
    "days_per_month": "On how many days per month are files written?",
    "request_assumptions_confirmed": "Are the S3 / KMS requests-per-file values confirmed?",
    "serving_mode": "Is the model billed pay-per-token?", "api_calls": "How many API calls does the one-time job make?"})
SCENARIO_BOUND = {"LOW": "low", "EXPECTED": "expected", "HIGH": "high", "STRESS": "high"}
# Every calculator area must be INCLUDED (it has inputs), EXCLUDED, UNKNOWN or NOT_APPLICABLE before an estimate is final.
# The state of an area without inputs is given in the request as "_areas" (keys starting with _ are ignored by the calculator).
AREAS = {"processing": "Databricks processing (streaming or batch jobs)", "sql": "SQL reporting / dashboards",
         "transfer": "AWS DataSync transfer or migration", "storage": "S3 storage, requests and KMS",
         "model_endpoints": "model API endpoints (pay per token)", "one_time_model_jobs": "one-time model jobs (e.g. initial indexing)"}
AREA_STATES = ("INCLUDED", "EXCLUDED", "UNKNOWN", "NOT_APPLICABLE")
AREA_NEEDS = {"processing": "the workload type, runtime per run and runs per month", "sql": "the number of concurrent SQL users",
              "transfer": "the GB per day moved through DataSync", "storage": "the TB retained, files written per day and reads per file",
              "model_endpoints": "the model, calls per day and input / output tokens per call",
              "one_time_model_jobs": "the model and the total input / output token range"}


def _has(section):
    return bool(section) if isinstance(section, list) else section is not None


def area_states(req, raw=None, named=None):
    """State of each calculator area for the result: INCLUDED when it has inputs, else the state given in _areas.
    A named use case keeps the scope it was presented with, so its areas without inputs are NOT_APPLICABLE."""
    raw = {str(k).lower(): str(v).upper() for k, v in raw.items()} if isinstance(raw, dict) else {}
    out = []
    for a in AREAS:
        state = "INCLUDED" if _has(getattr(req, a, None)) else raw.get(a) or ("NOT_APPLICABLE" if named else "UNCONFIRMED")
        out.append({"area": a, "state": state})
    return out


class _Issues:
    def __init__(self):
        self.rows = []

    def add(self, section, cid, field, issue, expected, received=None, blocking=True, question=None):
        self.rows.append({"section": section, "component_id": cid, "field": field, "issue": issue, "blocking": blocking,
                          "expected": expected, "received": None if received is None else json.dumps(received, default=str),
                          "question": question or QUESTIONS.get(field) or f"Please provide {field} for {cid or section}."})

    def has(self, section, cid, field):
        return any(r["section"] == section and r["component_id"] == cid and r["field"] == field for r in self.rows)


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_types(m, typ, d, section, cid, out):
    """Every supplied value must have the declared type: numbers as numbers (30, not "30"), true/false, text."""
    import difflib, math
    allowed = {f.name: f for f in m.fields(typ)}
    for k, v in d.items():
        if k.startswith("_"):
            continue
        if k not in allowed:
            near = difflib.get_close_matches(k, list(allowed), 1)
            out.add(section, cid, k, "UNKNOWN_FIELD", f"one of {sorted(allowed)}", k,
                    question=f"{k!r} is not a {section} field" + (f" - did you mean {near[0]!r}?" if near else "; please remove or rename it."))
            continue
        if v is None:
            continue
        f_, t = allowed[k], m._type_text(allowed[k])
        if m._is_numeric_field(f_):
            whole = t in ("int", "Optional[int]")
            if not _is_num(v):
                out.add(section, cid, k, "WRONG_TYPE", "a whole number" if whole else "a number", v,
                        question=f"{k} must be a number without quotes (got {v!r}). What is the correct value?")
            elif not math.isfinite(v) or v < 0:
                out.add(section, cid, k, "INVALID_VALUE", "a number of 0 or more", v,
                        question=f"{k} cannot be {v!r}. What is the correct value (0 or more)?")
            elif whole and v != int(v):
                out.add(section, cid, k, "WRONG_TYPE", "a whole number", v,
                        question=f"{k} must be a whole number (got {v!r}). What is the correct value?")
        elif m._is_bool_field(f_) and not isinstance(v, bool):
            out.add(section, cid, k, "WRONG_TYPE", "true or false", v, question=f"Is {k} true or false (got {v!r})?")
        elif m._is_str_field(f_) and not isinstance(v, str):
            out.add(section, cid, k, "WRONG_TYPE", "text", v, question=f"{k} must be text (got {v!r}). What is the correct value?")
        elif t.startswith("Optional[Dict") and not (isinstance(v, dict) and all(_is_num(x) for x in v.values())):
            out.add(section, cid, k, "WRONG_TYPE", 'a scenario map such as {"EXPECTED": 1.0, "HIGH": 1.5}', v)
    for k in m._required_fields(typ):
        if d.get(k) is None:
            out.add(section, cid, k, "MISSING", "required", None)


def _check_enum(d, field, values, section, cid, out):
    v = d.get(field)
    if isinstance(v, str) and v not in values:
        out.add(section, cid, field, "INVALID_VALUE", f"one of {list(values)}", v,
                question=f"{field} {v!r} is not recognised. Which one applies: {', '.join(values)}?")


def _need(d, field, section, cid, out):
    if d.get(field) is None and not out.has(section, cid, field):
        out.add(section, cid, field, "MISSING", "required: no default, the cost would be PENDING", None)


def _check_range(d, base, scenarios, section, cid, out, has_expected=True):
    """A low / expected / high value per scenario. The expected value (or, without an expected field, low and high)
    is required; a missing low or high bound only leaves that scenario PENDING, so it is reported but not blocking."""
    lo, ex, hi = (d.get(f"{base}_{b}") for b in ("low", "expected", "high"))
    if has_expected:
        if ex is None and lo is None and hi is None:
            _need(d, f"{base}_expected", section, cid, out)
            return
        if ex is None and "EXPECTED" in scenarios and not (base.endswith("tokens") and lo is not None and hi is not None):
            _need(d, f"{base}_expected", section, cid, out)
    else:
        for b in ("low", "high"):
            _need(d, f"{base}_{b}", section, cid, out)
        return
    for sc in scenarios:
        b = SCENARIO_BOUND.get(sc)
        if b in ("low", "high") and d.get(f"{base}_{b}") is None and not out.has(section, cid, f"{base}_{b}"):
            out.add(section, cid, f"{base}_{b}", "RANGE_BOUND_MISSING", f"optional: without it {sc} is PENDING", None, blocking=False)


def _check_request(m, d, out):
    if not isinstance(d, dict):
        out.add("request", None, "request", "WRONG_TYPE", "a JSON object", d, question="Please send the request as a JSON object.")
        return
    if "_first_estimate" in d and not isinstance(d["_first_estimate"], bool):
        out.add("request", None, "_first_estimate", "WRONG_TYPE", "true or false", d["_first_estimate"])
    top = {k: v for k, v in d.items() if k not in {s for s, _, _ in SECTIONS} and k != "assumptions"}
    _check_types(m, m.EstimateRequest, top, "request", None, out)
    scenarios = d.get("scenarios") or ["LOW", "EXPECTED", "HIGH"]
    if not isinstance(scenarios, list) or [s for s in scenarios if s not in m.SCENARIOS_ALL]:
        out.add("request", None, "scenarios", "INVALID_VALUE", f"a list drawn from {list(m.SCENARIOS_ALL)}", scenarios)
        scenarios = ["LOW", "EXPECTED", "HIGH"]
    present = 0
    convs = {(c.model, c.serving_mode) for c in m.DEFAULT_MODEL_CONVERSIONS}
    for key, cls, is_list in SECTIONS:
        val = d.get(key)
        if val is None:
            continue
        if is_list and not isinstance(val, list) or not is_list and not isinstance(val, dict):
            out.add(key, None, key, "WRONG_TYPE", "a list of objects" if is_list else "a JSON object", val,
                    question=f"{key} must be {'a list of objects' if is_list else 'a JSON object'}. Please resend it.")
            continue
        for n, item in enumerate(val if is_list else [val]):
            label = f"{key}[{n}]" if is_list else key
            if not isinstance(item, dict):
                out.add(key, None, label, "WRONG_TYPE", "a JSON object", item, question=f"{label} must be a JSON object.")
                continue
            present += 1
            cid = item.get("component_id") if isinstance(item.get("component_id"), str) else label
            _check_types(m, getattr(m, cls), item, key, cid, out)
            if key == "processing":
                _check_enum(item, "workload_type", m.WORKLOAD_TYPES, key, cid, out)
                _check_enum(item, "streaming_mode", STREAMING_MODES, key, cid, out)
                _check_enum(item, "latency_requirement", LATENCIES, key, cid, out)
                _check_enum(item, "dbu_per_hour_evidence", m.Evidence.ALL, key, cid, out)
                if _is_num(item.get("quantity")) and item["quantity"] < 1:
                    out.add(key, cid, "quantity", "INVALID_VALUE", "1 or more", item["quantity"],
                            question="How many identical copies of this workload run (1 or more)?")
                if item.get("workload_type") in m.WORKLOAD_TYPES and item["workload_type"] != "CONTINUOUS_STREAMING":
                    _check_range(item, "runtime_hours", scenarios, key, cid, out)
                    if item["workload_type"] != "EVENT_DRIVEN_BATCH":   # event-driven has a planning default
                        _need(item, "runs_per_month", key, cid, out)
            elif key == "sql":
                if _is_num(item.get("concurrent_users")) and item["concurrent_users"] < 1:
                    out.add(key, cid, "concurrent_users", "INVALID_VALUE", "1 or more", item["concurrent_users"])
            elif key == "transfer":
                _check_range(item, "daily_gb", scenarios, key, cid, out)
            elif key == "storage":
                for f_ in ("retained_tb", "files_per_day", "s3_read_requests_per_file"):
                    _need(item, f_, key, cid, out)
                if "s3_write_requests_per_file" in item:
                    _need(item, "s3_write_requests_per_file", key, cid, out)
                if (item.get("encrypted_files_per_day") or 0) and "kms_requests_per_encrypted_file" in item:
                    _need(item, "kms_requests_per_encrypted_file", key, cid, out)
            else:   # model_endpoints, one_time_model_jobs
                mode = item.get("serving_mode") or "PAY_PER_TOKEN"
                known = sorted(mm for mm, sm in convs if sm == mode)
                if mode != "PAY_PER_TOKEN":
                    out.add(key, cid, "serving_mode", "INVALID_VALUE", "PAY_PER_TOKEN", mode,
                            question="Only pay-per-token model serving can be estimated here. Is it PAY_PER_TOKEN?")
                elif isinstance(item.get("model"), str) and item["model"] not in known:
                    out.add(key, cid, "model", "INVALID_VALUE", f"one of {known}", item["model"],
                            question=f"Model {item['model']!r} has no pricing. Which of these is it: {', '.join(known)}?")
                for base in ("input_tokens", "output_tokens"):
                    _check_range(item, base, scenarios, key, cid, out, has_expected=(key == "model_endpoints"))
    raw = d.get("_areas")
    if raw is not None and not isinstance(raw, dict):
        out.add("request", None, "_areas", "WRONG_TYPE", 'an object such as {"transfer": "NOT_APPLICABLE"}', raw)
        raw = {}
    raw = raw or {}
    for k in raw:
        if k not in AREAS:
            out.add("request", None, f"_areas.{k}", "UNKNOWN_FIELD", f"one of {list(AREAS)}", k,
                    question=f"{k!r} is not a calculator area. The areas are: {', '.join(AREAS)}.")
    unconfirmed = []
    for a, label in AREAS.items():
        state = raw.get(a)
        if isinstance(state, str):
            state = state.upper()
        if state is not None and state not in AREA_STATES:
            out.add("request", None, f"_areas.{a}", "INVALID_VALUE", f"one of {list(AREA_STATES)}", raw.get(a),
                    question=f"Is {label} Included, Excluded, Unknown or Not Applicable?")
        elif _has(d.get(a)):
            if state in ("EXCLUDED", "NOT_APPLICABLE"):
                out.add(a, None, f"_areas.{a}", "INVALID_VALUE", "INCLUDED (inputs were supplied)", raw.get(a),
                        question=f"{label} has inputs but is marked {state}. Should it be included, or should its inputs be removed?")
        elif state == "INCLUDED":
            out.add(a, None, a, "MISSING", "required: the area is included but has no inputs", None,
                    question=f"{label} is included: what are {AREA_NEEDS[a]}?")
        elif state is None:
            unconfirmed.append(a)
    if unconfirmed and present:
        out.add("request", None, "_areas", "AREA_UNCONFIRMED", f"each of {unconfirmed} marked {'/'.join(AREA_STATES)}", ",".join(unconfirmed),
                question="Before the estimate is final, every calculator area must be confirmed. For each of these, is it Included "
                         "(then give its inputs), Excluded, Unknown or Not Applicable: "
                         + "; ".join(AREAS[a] for a in unconfirmed) + "?")
    if not present and "INCLUDED" not in {str(v).upper() for v in raw.values()}:
        out.add("request", None, "workload", "NO_WORKLOAD", "at least one section", None,
                question="What should be estimated? Describe the processing workload (type, runtime per run, runs per month), "
                         "SQL users, daily DataSync GB, S3 storage (TB, files per day, reads per file) or model calls and tokens.")


def check(m, request, overrides=None, named=False):
    """List what must be supplied or corrected before estimating. Named use cases were validated when they were loaded,
    so only the request as built (with any what-if overrides) is validated; ad-hoc and pasted JSON requests are also
    checked for every input that has no calculator default. Anything the calculator itself would reject is reported too."""
    out = _Issues()
    try:
        req = json.loads(request) if isinstance(request, str) else request
    except json.JSONDecodeError as e:
        out.add("request", None, "request", "NOT_JSON", "valid JSON", None,
                question=f"The request is not valid JSON ({e.msg} at line {e.lineno}, column {e.colno}). Please correct and resend it.")
        return out.rows
    try:
        ov = json.loads(overrides) if isinstance(overrides, str) and overrides else (overrides or {})
    except json.JSONDecodeError as e:
        out.add("overrides", None, "overrides", "NOT_JSON", "valid JSON", None,
                question=f"The what-if changes are not valid JSON ({e.msg}). Please resend them as field: value pairs.")
        return out.rows
    if not named and isinstance(req, dict) and "preset" not in req:
        try:
            _check_request(m, prepare_defaults(m, _apply_paths(json.loads(json.dumps(req)), ov))[0] if isinstance(ov, dict) else req, out)
        except (ValueError, TypeError) as e:
            out.add("request", None, "_model_planning", "INVALID_VALUE", "valid planning scenario", None, question=str(e))
    if not any(r["blocking"] for r in out.rows):
        try:   # the calculator's own validation, so the check is never looser than the estimate
            if not isinstance(req, dict):
                raise ValueError(f"Request must be a JSON object, got {type(req).__name__}.")
            if not isinstance(ov, dict):
                raise ValueError("Overrides must be a JSON object of field: value pairs.")
            m.validate_request(build_request(m, req, ov))
        except (ValueError, TypeError) as e:
            for msg in [x.strip(" -") for x in str(e).replace("Invalid estimate request:", "").split("\n") if x.strip(" -")]:
                out.add("request", None, "request", "INVALID_VALUE", "a valid request", None, question=f"{msg}. Please correct it.")
    return out.rows


def run_check(m, request, overrides=None, named=False):
    """Entry point used by the Unity Catalog function dcp_check. Always returns JSON text."""
    rows = check(m, request, overrides, named)
    return json.dumps({"status": "NEEDS_INPUT" if any(r["blocking"] for r in rows) else "READY", "issues": rows}, default=str)


def intake_questions(m):
    """Rows (area, field, type, requirement, default_value, question) for the guided intake, from the request schema."""
    from dataclasses import MISSING
    classes = dict((key, cls) for key, cls, _ in SECTIONS)
    rows = []
    for area, cls in classes.items():
        for f_ in m.fields(getattr(m, cls)):
            if f_.name in INTAKE_SKIP:
                continue
            default = None if f_.default is MISSING else f_.default
            req = "REQUIRED" if f_.name in INTAKE_REQUIRED[area] else ("DEFAULT" if default is not None else "OPTIONAL")
            if area == "processing" and f_.name == "runs_per_month":
                req = "REQUIRED for batch types (EVENT_DRIVEN_BATCH: planning default runs); not used for CONTINUOUS_STREAMING"
            elif area == "processing" and f_.name == "runtime_hours_expected":
                req = "REQUIRED for batch types; not used for CONTINUOUS_STREAMING"
            rows.append((area, f_.name, m._type_text(f_), req, None if default is None else json.dumps(default),
                         QUESTIONS.get(f_.name) or f"{f_.name.replace('_', ' ').capitalize()}?"))
    return rows
