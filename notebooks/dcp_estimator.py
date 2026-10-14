"""
DCP Smart Estimation Calculator - reusable core module
======================================================

Version: 4.0.2 (input-hardening update of 4.0.1; no change to any calculated figure)

PURPOSE
-------
Produce a repeatable, explainable *planning* estimate for a proposed workload from
structured inputs. This is an internal planning estimate, not an invoice or a
committed run rate.

HOW AN ESTIMATE FLOWS THROUGH THE CALCULATOR
--------------------------------------------
    Inputs -> Validation -> Rates -> Compute selection -> Engines -> Totals -> Confidence -> Output

FILE LAYOUT
-----------
    Part 1  Vocabulary          Status, treatment, evidence, and scenario constants
    Part 2  Rates               Rate records, model conversions, pricing providers
    Part 3  Inputs              EstimateRequest and its building blocks
    Part 4  Validation          Reject bad inputs before any math runs
    Part 5  Compute selection   Which Databricks compute option fits a workload
    Part 6  Engines             Five engines that turn inputs into priced line items
    Part 7  Orchestration       estimate(): runs the engines, totals, and confidence
    Part 8  Output              EstimateResult, tables, plain-language summary
    Part 9  Presets             SEND_US01, AI_RAG_MODEL_API, and the new-workload template
    Part 10 Databricks helpers  Optional live rates and historical calibration (Spark)

CORE vs ADD-ONS
---------------
Parts 1-8 are the core. They are pure Python, have no Spark or widget dependency,
and contain no customer-specific values. Part 9 holds presets: each preset only
*builds an EstimateRequest*; it never calculates. Part 10 is optional and only runs
inside Databricks.

USAGE
-----
    from dcp_estimator import estimate, load_preset, EstimateRequest, format_summary

    request = load_preset("SEND_US01")              # a known workload
    request = EstimateRequest.from_json(text)       # or a brand-new workload
    result = estimate(request)
    print(format_summary(result))

GUARDRAILS (apply everywhere)
-----------------------------
* Missing values stay PENDING. They are never silently treated as zero.
* A confirmed zero is different from a missing value.
* Rates must match exactly (provider, SKU, unit, region, effective date). No substitution.
* Recurring monthly costs and one-time costs are always kept separate.
* Every priced line carries its quantity, unit, rate, rate source, and formula.
* Planning assumptions are labeled as assumptions and lower the confidence rating.
* Same request + same rates + same calibration always gives the same result.
* Bad input fails with a clear ValueError that names the field, never a crash in the math.

CHANGE HISTORY
--------------
    4.0.2  Input hardening. Type checks on every numeric, true/false, and text field;
           null list sections mean none; clear errors for missing fields and invalid
           JSON; assumption overrides checked for shape and value; live account rates
           now match region, effective date, cloud, currency, and unit, read one named
           price column, and flag differences from the approved list. No calculated
           figure changed. Tests: 42 -> 51.
    4.0.1  Clean refactor of v3.9.1.14 BETA: one engine, one entry point, presets as inputs only.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, Tuple

CALCULATOR_VERSION = "4.0.2"
DEFAULT_AS_OF = "2026-10-05"


# =============================================================================
# PART 1  VOCABULARY
# =============================================================================

SCENARIOS_ALL = ("LOW", "EXPECTED", "HIGH", "STRESS")
"""LOW / EXPECTED / HIGH form the planning range. STRESS is an operating sensitivity,
not a forecast."""

# Historical calibration returns 25th, 50th, and 75th percentiles. This maps each
# scenario to the percentile it uses.
SCENARIO_PERCENTILE_INDEX = {"LOW": 0, "EXPECTED": 1, "HIGH": 2, "STRESS": 2}


class RateStatus:
    GOVERNED = "GOVERNED"                      # confirmed account rate
    VALIDATED_OBSERVED = "VALIDATED_OBSERVED"  # validated from billing exports
    REFERENCE_ONLY = "REFERENCE_ONLY"          # planning fallback, flagged in output
    PENDING = "PENDING"                        # no usable rate; cost is not calculated

    USABLE = {GOVERNED, VALIDATED_OBSERVED, REFERENCE_ONLY}


class Treatment:
    INCLUDED = "INCLUDED"   # counted in totals
    PENDING = "PENDING"     # cannot be priced yet; listed, not counted, never zero
    EXCLUDED = "EXCLUDED"   # deliberately out of scope


class Recurrence:
    MONTHLY = "RECURRING_MONTHLY"
    ONE_TIME = "ONE_TIME"


class Evidence:
    """How well-supported a quantity is. Drives the overall confidence rating."""
    CONFIRMED = "CONFIRMED"                      # supplied and confirmed by the team, or measured
    RANGE_DERIVED = "RANGE_DERIVED"              # derived from a team-supplied low/high range
    PLANNING_ASSUMPTION = "PLANNING_ASSUMPTION"  # calculator default, proxy, or unconfirmed frequency

    ALL = (CONFIRMED, RANGE_DERIVED, PLANNING_ASSUMPTION)


WORKLOAD_TYPES = (
    "CONTINUOUS_STREAMING",
    "EVENT_DRIVEN_BATCH",
    "SCHEDULED_BATCH",
    "ML_TRAINING",
    "BATCH_SCORING",
)


# =============================================================================
# PART 2  RATES AND PRICING PROVIDERS
# =============================================================================

@dataclass(frozen=True)
class RateRecord:
    """One price for one billing unit.

    `rate` is USD per `unit`. Units that include a count, such as REQUEST_1000,
    mean the rate is per 1,000 requests.
    """
    provider: str               # DATABRICKS or AWS
    service: str                # JOBS, SQL, S3, KMS, DATASYNC, FOUNDATION_MODEL_API, ...
    sku: str                    # exact SKU or AWS usage type
    unit: str                   # DBU, GB, GB_MONTH, REQUEST_1000, REQUEST_10000, KEY_MONTH
    region: str
    rate: Optional[float]
    status: str
    source: str
    effective_start: str = "2026-01-01"
    effective_end: Optional[str] = None
    observed_rate: Optional[float] = None   # reference only, never applied by default
    notes: str = ""


@dataclass(frozen=True)
class ModelConversion:
    """Databricks DBUs consumed per 1 million tokens for one exact model."""
    model: str
    serving_mode: str
    account_sku: str
    input_dbu_per_million: Optional[float]
    output_dbu_per_million: Optional[float]
    cache_dbu_per_million: Optional[float]
    status: str
    source: str
    effective_start: str = "2026-09-21"
    effective_end: Optional[str] = None


@dataclass(frozen=True)
class RateQuote:
    """What a pricing provider returns for one lookup."""
    rate: Optional[float]
    status: str
    source: str
    warning: Optional[str] = None

    @property
    def usable(self) -> bool:
        return self.rate is not None and self.status in RateStatus.USABLE


DEFAULT_RATE_REGISTRY: Tuple[RateRecord, ...] = (
    # --- Databricks (account rates from system.billing.account_prices) --------------
    RateRecord("DATABRICKS", "JOBS", "ENTERPRISE_JOBS_SERVERLESS", "DBU", "US_EAST_1", 0.3465,
               RateStatus.GOVERNED, "system.billing.account_prices"),
    RateRecord("DATABRICKS", "SQL", "SQL_PRO", "DBU", "US_EAST_1", 0.4235,
               RateStatus.GOVERNED, "system.billing.account_prices"),
    RateRecord("DATABRICKS", "ALL_PURPOSE", "ENTERPRISE_ALL_PURPOSE_COMPUTE", "DBU", "US_EAST_1", 0.5005,
               RateStatus.GOVERNED, "system.billing.account_prices"),
    RateRecord("DATABRICKS", "ALL_PURPOSE", "ENTERPRISE_ALL_PURPOSE_SERVERLESS_COMPUTE", "DBU", "US_EAST_1", 0.7315,
               RateStatus.GOVERNED, "system.billing.account_prices"),
    RateRecord("DATABRICKS", "FOUNDATION_MODEL_API", "ENTERPRISE_SERVERLESS_REAL_TIME_INFERENCE", "DBU", "US_EAST_1",
               0.0700, RateStatus.REFERENCE_ONLY, "Databricks public pricing cross-check",
               effective_start="2026-09-21",
               notes="Model account rate $0.07/DBU is a planning fallback until the exact account rate is resolved."),
    # --- AWS --------------------------------------------------------------------------
    RateRecord("AWS", "DATASYNC", "DataSync-Bytes", "GB", "US_EAST_1", 0.0125,
               RateStatus.VALIDATED_OBSERVED, "Cloudability validated sample", "2026-08-01",
               observed_rate=0.0095),
    RateRecord("AWS", "S3", "TimedStorage-ByteHrs", "GB_MONTH", "US_EAST_1", 0.023,
               RateStatus.REFERENCE_ONLY, "AWS S3 Standard list planning rate", "2026-08-01",
               observed_rate=0.02105655,
               notes="S3 storage uses the $0.023 list rate (matches the presented SEND figure); "
                     "Cloudability observed $0.0211. Standard to be decided."),
    RateRecord("AWS", "S3", "Requests-Tier1", "REQUEST_1000", "US_EAST_1", 0.005,
               RateStatus.VALIDATED_OBSERVED, "Cloudability August 2026 export", "2026-08-01",
               observed_rate=0.0038, notes="Write, copy, list requests."),
    RateRecord("AWS", "S3", "Requests-Tier2", "REQUEST_1000", "US_EAST_1", 0.0004,
               RateStatus.VALIDATED_OBSERVED, "Cloudability August 2026 export", "2026-08-01",
               observed_rate=0.000304, notes="Read requests."),
    RateRecord("AWS", "KMS", "KMS-Requests", "REQUEST_10000", "US_EAST_1", 0.03,
               RateStatus.VALIDATED_OBSERVED, "Cloudability validated sample", "2026-08-01",
               observed_rate=0.0228),
    RateRecord("AWS", "KMS", "KMS-Keys", "KEY_MONTH", "US_EAST_1", 1.00,
               RateStatus.VALIDATED_OBSERVED, "Cloudability validated sample", "2026-08-01",
               observed_rate=0.76),
)

DEFAULT_MODEL_CONVERSIONS: Tuple[ModelConversion, ...] = (
    ModelConversion("gpt-5.4-nano", "PAY_PER_TOKEN", "ENTERPRISE_SERVERLESS_REAL_TIME_INFERENCE",
                    2.857, 17.857, 0.286, RateStatus.GOVERNED, "Databricks public pricing 2026-09-22"),
    ModelConversion("gte-large-en", "PAY_PER_TOKEN", "ENTERPRISE_SERVERLESS_REAL_TIME_INFERENCE",
                    1.857, 0.0, None, RateStatus.GOVERNED, "Databricks public pricing 2026-09-22"),
    ModelConversion("gpt-5-4", "PAY_PER_TOKEN", "ENTERPRISE_SERVERLESS_REAL_TIME_INFERENCE",
                    35.714, 214.286, 3.571, RateStatus.GOVERNED,
                    "Databricks public pricing 2026-09-22 (GPT-5.4 standard short context)"),
    ModelConversion("claude-sonnet-4-6", "PAY_PER_TOKEN", "ENTERPRISE_SERVERLESS_REAL_TIME_INFERENCE",
                    42.857, 214.286, 4.286, RateStatus.GOVERNED, "Databricks public pricing 2026-09-22"),
)


class PricingProvider(Protocol):
    """Anything that can answer rate and model-conversion questions.

    The engines only talk to this interface. To add the AWS Pricing Calculator API,
    implement a provider for AWS lookups and combine it with the registry using
    CompositePricingProvider. No engine code changes.
    """

    def get_rate(self, provider: str, sku: str, unit: str, region: str) -> RateQuote: ...

    def get_model_conversion(self, model: str, serving_mode: str) -> Tuple[Optional[ModelConversion], Optional[str]]: ...


def _is_effective(start: str, end: Optional[str], as_of: str) -> bool:
    return start <= as_of and (end is None or as_of < end)


class RegistryPricingProvider:
    """Default provider: exact-match lookups against the versioned registries."""

    def __init__(self, rates=DEFAULT_RATE_REGISTRY, conversions=DEFAULT_MODEL_CONVERSIONS,
                 as_of: str = DEFAULT_AS_OF):
        self.rates = tuple(rates)
        self.conversions = tuple(conversions)
        self.as_of = as_of

    def get_rate(self, provider, sku, unit, region) -> RateQuote:
        key = tuple(x.upper() for x in (provider, sku, unit, region))
        matches = [r for r in self.rates
                   if (r.provider.upper(), r.sku.upper(), r.unit.upper(), r.region.upper()) == key
                   and _is_effective(r.effective_start, r.effective_end, self.as_of)]
        if len(matches) != 1:
            return RateQuote(None, RateStatus.PENDING, "",
                             f"No single exact rate for {provider}/{sku}/{unit}/{region} "
                             f"(matches={len(matches)}). No substitution allowed.")
        r = matches[0]
        if r.status not in RateStatus.USABLE or r.rate is None:
            return RateQuote(None, RateStatus.PENDING, r.source, f"Rate {sku} is not usable ({r.status}).")
        warning = r.notes if r.status == RateStatus.REFERENCE_ONLY else None
        return RateQuote(float(r.rate), r.status, r.source, warning)

    def get_model_conversion(self, model, serving_mode):
        matches = [c for c in self.conversions
                   if c.model == model and c.serving_mode == serving_mode
                   and _is_effective(c.effective_start, c.effective_end, self.as_of)]
        if len(matches) != 1:
            return None, f"No single exact conversion for model {model!r} ({serving_mode}). No substitute model allowed."
        c = matches[0]
        if c.status not in (RateStatus.GOVERNED, RateStatus.VALIDATED_OBSERVED):
            return None, f"Conversion for {model!r} is {c.status}; not publishable."
        return c, None


class CompositePricingProvider:
    """Ask the primary provider first; fall back when it returns PENDING.

    Example (future):
        provider = CompositePricingProvider(AwsPricingApiProvider(...), RegistryPricingProvider())
    """

    def __init__(self, primary: PricingProvider, fallback: PricingProvider):
        self.primary, self.fallback = primary, fallback

    def get_rate(self, provider, sku, unit, region) -> RateQuote:
        quote = self.primary.get_rate(provider, sku, unit, region)
        return quote if quote.usable else self.fallback.get_rate(provider, sku, unit, region)

    def get_model_conversion(self, model, serving_mode):
        conv, warn = self.primary.get_model_conversion(model, serving_mode)
        return (conv, warn) if conv is not None else self.fallback.get_model_conversion(model, serving_mode)


class AwsPricingApiProvider:
    """Placeholder for the AWS Pricing Calculator API adapter (not yet implemented).

    Contract: receives the same (provider, sku, unit, region) lookup and returns a
    RateQuote in USD per the requested unit. Until connectivity, authentication, and
    service-code mapping are validated, it returns PENDING so the registry is used.
    """

    def get_rate(self, provider, sku, unit, region) -> RateQuote:
        return RateQuote(None, RateStatus.PENDING, "AWS Pricing Calculator API",
                         "AWS pricing adapter not implemented yet.")

    def get_model_conversion(self, model, serving_mode):
        return None, "AWS pricing adapter does not provide model conversions."


# =============================================================================
# PART 3  INPUTS
# =============================================================================

@dataclass
class ProcessingComponent:
    """One Databricks data-processing workload (streaming or batch).

    `dbu_per_hour` optionally fixes compute intensity per scenario for this component,
    for example {"EXPECTED": 1.0, "HIGH": 1.5}. When set it takes priority over both
    historical calibration and calculator defaults, so a presented estimate stays
    reproducible. `dbu_per_hour_basis` and `dbu_per_hour_evidence` say where it came from.
    """
    component_id: str
    name: str
    workload_type: str                       # one of WORKLOAD_TYPES
    quantity: int = 1                        # number of identical copies
    pattern: str = "TBD"                     # CONTINUOUS, SCHEDULED, AS_NEEDED, ...
    runtime_hours_low: Optional[float] = None       # per run (batch only)
    runtime_hours_expected: Optional[float] = None
    runtime_hours_high: Optional[float] = None
    runs_per_month: Optional[float] = None   # confirmed frequency; None = unconfirmed
    dbu_per_hour: Optional[Dict[str, float]] = None
    dbu_per_hour_basis: str = ""
    dbu_per_hour_evidence: str = Evidence.PLANNING_ASSUMPTION
    requires_gpu: bool = False
    requires_custom_runtime: bool = False
    requires_custom_networking: bool = False
    declarative_pipeline_preferred: bool = False
    streaming_mode: str = "UNCONFIRMED"      # TRUE_CONTINUOUS_LOW_LATENCY, INCREMENTAL_AVAILABLE_NOW, UNCONFIRMED
    latency_requirement: str = "UNCONFIRMED" # SECONDS, MINUTES_OR_MORE, UNCONFIRMED
    evidence: str = ""


@dataclass
class SqlReporting:
    """Dashboard and ad hoc SQL usage."""
    component_id: str
    name: str
    concurrent_users: int
    working_days_per_month: int = 22
    total_users: Optional[int] = None
    evidence: str = ""


@dataclass
class DataTransfer:
    """Inbound data movement priced with AWS DataSync."""
    daily_gb_low: Optional[float] = None
    daily_gb_expected: Optional[float] = None
    daily_gb_high: Optional[float] = None
    transfer_days_per_month: int = 22
    initial_transfer_gb: Optional[float] = None   # one-time migration; None = not modeled
    evidence: str = ""


@dataclass
class AwsStorage:
    """S3 storage, S3 requests, and KMS encryption for the workload's data."""
    retained_tb: Optional[float] = None
    files_per_day: Optional[float] = None
    encrypted_files_per_day: float = 0.0
    kms_keys: float = 0.0
    days_per_month: int = 30
    s3_write_requests_per_file: Optional[float] = 1.0   # assumption unless confirmed
    s3_read_requests_per_file: Optional[float] = None   # None = read pattern unknown -> PENDING
    kms_requests_per_encrypted_file: Optional[float] = 1.0
    request_assumptions_confirmed: bool = False
    evidence: str = ""


@dataclass
class ModelEndpoint:
    """One recurring model-serving call pattern billed per token."""
    component_id: str
    model: str
    role: str
    calls_per_day: float
    input_tokens_low: Optional[float] = None      # per call
    input_tokens_expected: Optional[float] = None
    input_tokens_high: Optional[float] = None
    output_tokens_low: Optional[float] = None
    output_tokens_expected: Optional[float] = None
    output_tokens_high: Optional[float] = None
    serving_mode: str = "PAY_PER_TOKEN"
    evidence: str = ""


@dataclass
class OneTimeModelJob:
    """A one-time token workload, such as initial corpus indexing."""
    component_id: str
    model: str
    description: str
    input_tokens_low: Optional[float] = None      # total, not per call
    input_tokens_high: Optional[float] = None
    output_tokens_low: Optional[float] = None
    output_tokens_high: Optional[float] = None
    api_calls: Optional[float] = None
    serving_mode: str = "PAY_PER_TOKEN"


@dataclass
class PlanningAssumptions:
    """Calculator defaults used when the team has not confirmed a value.

    Every use of these values is labeled PLANNING_ASSUMPTION in the output.
    Override per request when better evidence exists.
    """
    continuous_hours_per_month: float = 730.0     # 365 days x 24 hours / 12 months
    continuous_dbu_per_hour: Dict[str, float] = field(
        default_factory=lambda: {"LOW": 0.5, "EXPECTED": 1.0, "HIGH": 1.5, "STRESS": 2.0})
    batch_dbu_per_hour: float = 1.0
    unconfirmed_event_runs_per_month: Dict[str, float] = field(
        default_factory=lambda: {"LOW": 0, "EXPECTED": 1, "HIGH": 1, "STRESS": 4})
    sql_dbu_per_hour_per_band: float = 4.0
    sql_active_hours_per_day: Dict[str, float] = field(
        default_factory=lambda: {"LOW": 0.5, "EXPECTED": 1.0, "HIGH": 2.0, "STRESS": 4.0})
    sql_concurrency_factor: Dict[str, float] = field(
        default_factory=lambda: {"LOW": 0.5, "EXPECTED": 1.0, "HIGH": 1.5, "STRESS": 2.0})
    sql_users_per_capacity_band: int = 10
    model_stress_call_multiplier: float = 1.25
    min_calibration_peers: int = 3


@dataclass
class EstimateRequest:
    """Everything the calculator needs for one estimate. Every section is optional;
    the calculator only runs the engines whose inputs are present.

    `calibration_allowed=False` freezes the estimate: historical calibration is ignored
    even if supplied. Use it for estimates that have already been presented.
    """
    use_case_id: str
    use_case_name: str
    scenarios: List[str] = field(default_factory=lambda: ["LOW", "EXPECTED", "HIGH"])
    region: str = "US_EAST_1"
    processing: List[ProcessingComponent] = field(default_factory=list)
    sql: Optional[SqlReporting] = None
    transfer: Optional[DataTransfer] = None
    storage: Optional[AwsStorage] = None
    model_endpoints: List[ModelEndpoint] = field(default_factory=list)
    model_days_per_month: int = 30
    one_time_model_jobs: List[OneTimeModelJob] = field(default_factory=list)
    assumptions: PlanningAssumptions = field(default_factory=PlanningAssumptions)
    calibration_allowed: bool = True
    scope_included: List[str] = field(default_factory=list)
    scope_excluded: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ---- serialization (lets an agent, a form, or a JSON file create a request) ----
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EstimateRequest":
        if not isinstance(data, dict):
            raise ValueError(f"Request must be a JSON object, got {type(data).__name__}.")
        d = {k: v for k, v in data.items() if not k.startswith("_")}
        _reject_unknown(cls, d, "request")
        # null and missing lists both mean "none"; a non-list is an input error.
        for key, typ in (("processing", ProcessingComponent), ("model_endpoints", ModelEndpoint),
                         ("one_time_model_jobs", OneTimeModelJob)):
            items = d.get(key)
            if items is None:
                items = []
            if not isinstance(items, list):
                raise ValueError(f"{key} must be a list (got {type(items).__name__}).")
            d[key] = [_build(typ, x, f"{key}[{n}]") for n, x in enumerate(items)]
        for key in ("scenarios", "scope_included", "scope_excluded", "notes"):
            if key in d and d[key] is None:
                d.pop(key)
            elif key in d and not isinstance(d[key], list):
                raise ValueError(f"{key} must be a list (got {d[key]!r}).")
        if "metadata" in d and d["metadata"] is None:
            d.pop("metadata")
        for key, typ in (("sql", SqlReporting), ("transfer", DataTransfer), ("storage", AwsStorage)):
            if d.get(key) is not None:
                d[key] = _build(typ, d[key], key)
        d["assumptions"] = _build_assumptions(d.get("assumptions"))
        _require_strings(cls, d, "request")
        return cls(**d)

    @classmethod
    def from_json(cls, text: str) -> "EstimateRequest":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Not valid JSON: {e.msg} at line {e.lineno}, column {e.colno}.") from None
        return cls.from_dict(data)


def _reject_unknown(typ, data: Dict[str, Any], label: str) -> None:
    allowed = {f.name for f in fields(typ)}
    unknown = [k for k in data if k not in allowed and not k.startswith("_")]
    if unknown:
        raise ValueError(f"Unknown field(s) in {label}: {unknown}")


def _required_fields(typ) -> List[str]:
    from dataclasses import MISSING
    return [f.name for f in fields(typ) if f.default is MISSING and f.default_factory is MISSING]


def _require_strings(typ, data: Dict[str, Any], label: str) -> None:
    missing = [k for k in _required_fields(typ) if data.get(k) is None]
    if missing:
        raise ValueError(f"Missing required field(s) in {label}: {missing}")


def _build(typ, data: Any, label: Optional[str] = None):
    label = label or typ.__name__
    if not isinstance(data, dict):
        raise ValueError(f"{label} must be a JSON object (got {type(data).__name__}).")
    clean = {k: v for k, v in data.items() if not k.startswith("_")}
    _reject_unknown(typ, clean, label)
    _require_strings(typ, clean, label)
    return typ(**clean)


def _build_assumptions(overrides: Any) -> "PlanningAssumptions":
    """Apply optional overrides to the calculator defaults. Scenario maps are merged, so
    {"batch_dbu_per_hour": 2.0} or {"sql_active_hours_per_day": {"HIGH": 3}} both work.
    A scenario map cannot be replaced by a single number, and vice versa."""
    base = PlanningAssumptions()
    if overrides is None:
        return base
    if not isinstance(overrides, dict):
        raise ValueError(f"assumptions must be a JSON object (got {type(overrides).__name__}).")
    for k, v in overrides.items():
        if k.startswith("_"):
            continue
        if not hasattr(base, k):
            raise ValueError(f"Unknown planning assumption: {k}")
        current = getattr(base, k)
        if isinstance(current, dict):
            if not isinstance(v, dict):
                raise ValueError(f"assumptions.{k} must be a scenario map such as "
                                 f'{{"EXPECTED": 1.0, "HIGH": 1.5}} (got {v!r}).')
            setattr(base, k, {**current, **v})
        else:
            if isinstance(v, (dict, list)):
                raise ValueError(f"assumptions.{k} must be a single number (got {v!r}).")
            setattr(base, k, v)
    return base


# =============================================================================
# PART 4  VALIDATION
# =============================================================================

def _type_text(f_) -> str:
    return f_.type if isinstance(f_.type, str) else getattr(f_.type, "__name__", str(f_.type))


def _is_numeric_field(f_) -> bool:
    t = _type_text(f_)
    return ("float" in t or t in ("int", "Optional[int]")) and "Dict" not in t and "List" not in t and "Tuple" not in t


def _is_bool_field(f_) -> bool:
    return _type_text(f_) == "bool"


def _is_str_field(f_) -> bool:
    return _type_text(f_) == "str"


def validate_request(req: EstimateRequest) -> None:
    """Raise ValueError listing every problem found. Missing optional values are fine.
    Rejected: negative or non-finite numbers, numbers or true/false typed as text,
    unknown workload types, unknown scenarios, duplicate IDs, and invalid planning
    assumptions (wrong shape, missing scenario, negative value, zero users per band)."""
    errors: List[str] = []

    def nonneg(label, value):
        if value is None:
            return
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or not math.isfinite(value):
            errors.append(f"{label} must be a non-negative number (got {value!r})")

    def check_numeric_fields(obj, label):
        """Every field declared as a number must hold a number. Text such as "30" is
        rejected with a clear message instead of failing later inside the math."""
        for f_ in fields(obj):
            if _is_numeric_field(f_):
                nonneg(f"{label}.{f_.name}", getattr(obj, f_.name))
            elif _is_bool_field(f_) and not isinstance(getattr(obj, f_.name), bool):
                errors.append(f"{label}.{f_.name} must be true or false (got {getattr(obj, f_.name)!r})")
            elif _is_str_field(f_) and not isinstance(getattr(obj, f_.name), str):
                errors.append(f"{label}.{f_.name} must be text (got {getattr(obj, f_.name)!r})")

    bad = [s for s in req.scenarios if s not in SCENARIOS_ALL]
    if bad or not req.scenarios:
        errors.append(f"scenarios must be a non-empty subset of {SCENARIOS_ALL} (got {req.scenarios})")

    ids: List[str] = []
    for c in req.processing:
        ids.append(c.component_id)
        if c.workload_type not in WORKLOAD_TYPES:
            errors.append(f"{c.component_id}: workload_type must be one of {WORKLOAD_TYPES}")
        check_numeric_fields(c, c.component_id)
        if c.dbu_per_hour is not None:
            for k, v in c.dbu_per_hour.items():
                if k not in SCENARIOS_ALL:
                    errors.append(f"{c.component_id}.dbu_per_hour has unknown scenario {k!r}")
                nonneg(f"{c.component_id}.dbu_per_hour[{k}]", v)
        if c.dbu_per_hour_evidence not in Evidence.ALL:
            errors.append(f"{c.component_id}.dbu_per_hour_evidence must be one of {Evidence.ALL}")
    if req.sql:
        ids.append(req.sql.component_id)
        check_numeric_fields(req.sql, "sql")
    if req.transfer:
        check_numeric_fields(req.transfer, "transfer")
    if req.storage:
        check_numeric_fields(req.storage, "storage")
    for e in req.model_endpoints:
        ids.append(e.component_id)
        check_numeric_fields(e, e.component_id)
    for j in req.one_time_model_jobs:
        ids.append(j.component_id)
        check_numeric_fields(j, j.component_id)
    nonneg("model_days_per_month", req.model_days_per_month)

    a = req.assumptions
    if not isinstance(a, PlanningAssumptions):
        errors.append("assumptions must be a PlanningAssumptions object")
    else:
        for f_ in fields(a):
            v = getattr(a, f_.name)
            if isinstance(v, dict):
                for sc in SCENARIOS_ALL:
                    if sc not in v:
                        errors.append(f"assumptions.{f_.name} is missing scenario {sc}")
                for k, x in v.items():
                    if k not in SCENARIOS_ALL:
                        errors.append(f"assumptions.{f_.name} has unknown scenario {k!r}")
                    nonneg(f"assumptions.{f_.name}[{k}]", x)
            else:
                nonneg(f"assumptions.{f_.name}", v)
        if isinstance(a.sql_users_per_capacity_band, (int, float)) and a.sql_users_per_capacity_band <= 0:
            errors.append("assumptions.sql_users_per_capacity_band must be greater than zero")
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        errors.append(f"Duplicate component IDs: {dupes}")
    if errors:
        raise ValueError("Invalid estimate request:\n  - " + "\n  - ".join(errors))


# =============================================================================
# PART 5  COMPUTE SELECTION
# =============================================================================

@dataclass(frozen=True)
class ComputeOption:
    name: str
    workload_types: Tuple[str, ...]
    base_score: int
    sku: Optional[str]      # None = cost inputs still required (not priced by this calculator yet)
    verified: bool


COMPUTE_OPTIONS: Tuple[ComputeOption, ...] = (
    ComputeOption("JOBS_SERVERLESS", ("CONTINUOUS_STREAMING", "SCHEDULED_BATCH", "EVENT_DRIVEN_BATCH",
                                      "ML_TRAINING", "BATCH_SCORING"), 70, "ENTERPRISE_JOBS_SERVERLESS", True),
    ComputeOption("CLASSIC_JOBS_COMPUTE", ("CONTINUOUS_STREAMING", "SCHEDULED_BATCH", "EVENT_DRIVEN_BATCH",
                                           "ML_TRAINING", "BATCH_SCORING"), 45, None, True),
    ComputeOption("SERVERLESS_PIPELINE", ("CONTINUOUS_STREAMING", "SCHEDULED_BATCH"), 55, None, False),
    ComputeOption("GPU_JOBS_COMPUTE", ("ML_TRAINING", "BATCH_SCORING"), 35, None, False),
)


@dataclass
class ComputeDecision:
    component_id: str
    selected: str
    alternative: Optional[str]
    score: Optional[int]
    confidence: str
    status: str
    sku: Optional[str]
    eligible: List[str]
    streaming_decision: str = "NOT_APPLICABLE"


def recommend_compute(c: ProcessingComponent) -> ComputeDecision:
    """Choose the Databricks compute option for one processing component. Rule-based and
    deterministic: the same component always gets the same decision.

    Streaming rules (in order):
      1. Custom runtime or networking  -> CLASSIC_JOBS_COMPUTE (DBU plus AWS cost inputs required)
      2. Low latency or declarative    -> SERVERLESS_PIPELINE (pipeline rate required)
      3. Incremental / minutes latency -> JOBS_SERVERLESS
      4. Unconfirmed                   -> JOBS_SERVERLESS as a visible pricing proxy
    Other workloads are scored against COMPUTE_OPTIONS; highest score wins, ties by name.
    """
    cid = c.component_id
    if c.workload_type == "CONTINUOUS_STREAMING":
        all_three = ["SERVERLESS_PIPELINE", "JOBS_SERVERLESS", "CLASSIC_JOBS_COMPUTE"]
        if c.requires_custom_runtime or c.requires_custom_networking:
            return ComputeDecision(cid, "CLASSIC_JOBS_COMPUTE", "SERVERLESS_PIPELINE", 90, "MEDIUM",
                                   "CUSTOM_REQUIREMENT_COST_INPUTS_REQUIRED", None, all_three, "CUSTOM_REQUIREMENTS")
        if (c.streaming_mode == "TRUE_CONTINUOUS_LOW_LATENCY" or c.latency_requirement == "SECONDS"
                or c.declarative_pipeline_preferred):
            return ComputeDecision(cid, "SERVERLESS_PIPELINE", "JOBS_SERVERLESS", 90, "HIGH",
                                   "PIPELINE_RATE_REQUIRED", None, all_three, "TRUE_CONTINUOUS_LOW_LATENCY")
        if c.streaming_mode == "INCREMENTAL_AVAILABLE_NOW" or c.latency_requirement == "MINUTES_OR_MORE":
            return ComputeDecision(cid, "JOBS_SERVERLESS", "SERVERLESS_PIPELINE", 90, "HIGH",
                                   "COST_MODEL_READY", "ENTERPRISE_JOBS_SERVERLESS", all_three,
                                   "INCREMENTAL_AVAILABLE_NOW")
        return ComputeDecision(cid, "JOBS_SERVERLESS", "SERVERLESS_PIPELINE", 70, "MEDIUM",
                               "PRICING_PROXY_PENDING_STREAMING_CLASSIFICATION", "ENTERPRISE_JOBS_SERVERLESS",
                               all_three, "UNCONFIRMED")

    scored = []
    custom = c.requires_custom_runtime or c.requires_custom_networking
    for o in COMPUTE_OPTIONS:
        if c.workload_type not in o.workload_types:
            continue
        score = o.base_score
        if o.name == "JOBS_SERVERLESS":
            if c.pattern in ("CONTINUOUS", "SCHEDULED", "AS_NEEDED"):
                score += 15
            if custom:
                score -= 45
            if c.requires_gpu:
                score -= 40
        elif o.name == "CLASSIC_JOBS_COMPUTE":
            score += 45 if custom else -15
        elif o.name == "SERVERLESS_PIPELINE":
            score += 35 if c.declarative_pipeline_preferred else -20
        elif o.name == "GPU_JOBS_COMPUTE":
            score += 50 if c.requires_gpu else -50
        scored.append((score, o))
    if not scored:
        return ComputeDecision(cid, "ARCHITECTURE_REVIEW_REQUIRED", None, None, "LOW", "NO_ELIGIBLE_OPTION", None, [])
    scored.sort(key=lambda x: (-x[0], x[1].name))
    best_score, best = scored[0]
    confidence = "HIGH" if best_score >= 85 and best.verified else ("MEDIUM" if best_score >= 60 else "LOW")
    status = "COST_MODEL_READY" if best.sku else "RECOMMENDATION_READY_COST_INPUTS_REQUIRED"
    return ComputeDecision(cid, best.name, scored[1][1].name if len(scored) > 1 else None, best_score,
                           confidence, status, best.sku, [o.name for _, o in scored])


# =============================================================================
# PART 6  ENGINES
# Each engine turns one section of the request into LineItems. Engines never total.
# =============================================================================

@dataclass
class LineItem:
    scenario: str
    component_id: str
    engine: str
    description: str
    quantity: Optional[float]
    unit: str
    rate: Optional[float]
    rate_status: str
    rate_source: str
    cost_usd: Optional[float]
    recurrence: str
    treatment: str
    evidence: str
    basis: str
    formula: str
    warnings: List[str] = field(default_factory=list)


@dataclass
class CalibrationData:
    """Optional historical reference points from comparable workloads.

    Percentile tuples are (p25, p50, p75). Build with calibrate_from_system_tables()
    inside Databricks, or leave as None to use planning assumptions only.
    """
    continuous_peers: int = 0
    continuous_monthly_dbu: Tuple[Optional[float], Optional[float], Optional[float]] = (None, None, None)
    batch_peers: int = 0
    batch_dbu_per_hour: Tuple[Optional[float], Optional[float], Optional[float]] = (None, None, None)
    sql_peers: int = 0
    sql_monthly_dbu: Tuple[Optional[float], Optional[float], Optional[float]] = (None, None, None)
    ten_concurrent_users_validated: Optional[bool] = None
    peak_concurrent_statements: Optional[int] = None
    source: str = ""


def _fmt(x: float) -> str:
    """Readable number for formulas: no scientific notation."""
    if x == int(x):
        return f"{int(x):,}"
    return f"{x:,.4f}".rstrip("0").rstrip(".")


def _price(scenario, component_id, engine, description, quantity, unit, provider_name, sku, region,
           pricing: PricingProvider, recurrence, evidence, basis, formula_prefix, extra_warnings=None) -> LineItem:
    """Shared pricing step: quantity x exact rate. Missing quantity or rate -> PENDING."""
    warnings = list(extra_warnings or [])
    if quantity is None:
        return LineItem(scenario, component_id, engine, description, None, unit, None, RateStatus.PENDING, "",
                        None, recurrence, Treatment.PENDING, evidence, basis,
                        f"{formula_prefix}: quantity missing", warnings + ["Missing quantity remains pending, not zero."])
    quote = pricing.get_rate(provider_name, sku, unit, region)
    if quote.warning:
        warnings.append(quote.warning)
    if not quote.usable:
        return LineItem(scenario, component_id, engine, description, quantity, unit, None, quote.status,
                        quote.source, None, recurrence, Treatment.PENDING, evidence, basis,
                        f"{formula_prefix}: rate pending", warnings)
    cost = quantity * quote.rate
    return LineItem(scenario, component_id, engine, description, quantity, unit, quote.rate, quote.status,
                    quote.source, cost, recurrence, Treatment.INCLUDED, evidence, basis,
                    f"{formula_prefix} = {_fmt(quantity)} {unit} x ${quote.rate:g}/{unit} = ${cost:,.2f}", warnings)


def _pending(scenario, component_id, engine, description, unit, recurrence, basis, reason) -> LineItem:
    return LineItem(scenario, component_id, engine, description, None, unit, None, RateStatus.PENDING, "", None,
                    recurrence, Treatment.PENDING, Evidence.PLANNING_ASSUMPTION, basis, reason, [reason])


def _by_scenario(sc, low, expected, high):
    return {"LOW": low, "EXPECTED": expected, "HIGH": high, "STRESS": high}[sc]


def _calibrated(values, peers, sc, minimum):
    if values is None or peers < minimum:
        return None
    return values[SCENARIO_PERCENTILE_INDEX[sc]]


# ---- 6.1 Data processing ----------------------------------------------------------------

def engine_data_processing(req: EstimateRequest, pricing: PricingProvider,
                           calibration: Optional[CalibrationData]) -> Tuple[List[LineItem], List[ComputeDecision]]:
    """Databricks DBUs for streaming and batch processing components.

    Intensity priority for each component and scenario:
        1. component.dbu_per_hour (fixed by the team or locked from a presented estimate)
        2. historical calibration (only if supplied, allowed, and >= min peers)
        3. calculator planning default
    Continuous: 730 hours x DBU/hour (or calibrated monthly DBUs).
    Batch:      runs/month x runtime/run x DBU/hour. Unconfirmed event-driven frequency
                uses the planning default number of runs and is labeled as an assumption.
    """
    a = req.assumptions
    items: List[LineItem] = []
    decisions = [recommend_compute(c) for c in req.processing]
    minimum = a.min_calibration_peers
    for c, d in zip(req.processing, decisions):
        for sc in req.scenarios:
            desc = f"{c.name} ({d.selected})"
            if d.sku is None:
                items.append(_pending(sc, c.component_id, "DATA_PROCESSING", desc, "DBU", Recurrence.MONTHLY,
                                      d.status, f"{d.selected} selected; cost inputs required before pricing."))
                continue
            fixed = (c.dbu_per_hour or {}).get(sc)

            if c.workload_type == "CONTINUOUS_STREAMING":
                hours = a.continuous_hours_per_month
                hist = _calibrated(calibration.continuous_monthly_dbu if calibration else None,
                                   calibration.continuous_peers if calibration else 0, sc, minimum)
                if fixed is not None:
                    dbu, ev = hours * fixed * c.quantity, c.dbu_per_hour_evidence
                    basis = c.dbu_per_hour_basis or "TEAM_FIXED_DBU_PER_HOUR"
                    formula = f"{_fmt(hours)} h x {_fmt(fixed)} DBU/h x {c.quantity}"
                elif hist is not None:
                    dbu, ev, basis = hist * c.quantity, Evidence.CONFIRMED, "HISTORICALLY_CALIBRATED"
                    formula = f"calibrated {_fmt(hist)} DBU/month x {c.quantity}"
                else:
                    rate_h = a.continuous_dbu_per_hour[sc]
                    dbu, ev = hours * rate_h * c.quantity, Evidence.PLANNING_ASSUMPTION
                    basis = "PLANNING_DEFAULT_DBU_PER_HOUR"
                    formula = f"{_fmt(hours)} h x {_fmt(rate_h)} DBU/h x {c.quantity}"
                if d.streaming_decision == "UNCONFIRMED":
                    basis += " | JOBS_SERVERLESS_PRICING_PROXY_PENDING_STREAMING_CLASSIFICATION"
                items.append(_price(sc, c.component_id, "DATA_PROCESSING", desc, dbu, "DBU", "DATABRICKS",
                                    d.sku, req.region, pricing, Recurrence.MONTHLY, ev, basis, formula))
                continue

            # ---- Batch workloads ----
            runtime = _by_scenario(sc, c.runtime_hours_low, c.runtime_hours_expected, c.runtime_hours_high)
            if c.runs_per_month is not None:
                runs, runs_ev = c.runs_per_month, Evidence.CONFIRMED
            elif c.workload_type == "EVENT_DRIVEN_BATCH":
                runs, runs_ev = a.unconfirmed_event_runs_per_month[sc], Evidence.PLANNING_ASSUMPTION
            else:
                runs, runs_ev = None, Evidence.PLANNING_ASSUMPTION
            if runtime is None or runs is None:
                missing = "runtime per run" if runtime is None else "runs per month"
                items.append(_pending(sc, c.component_id, "DATA_PROCESSING", desc, "DBU", Recurrence.MONTHLY,
                                      "INPUT_REQUIRED", f"{c.component_id}: {missing} is required."))
                continue
            hist = _calibrated(calibration.batch_dbu_per_hour if calibration else None,
                               calibration.batch_peers if calibration else 0, sc, minimum)
            if fixed is not None:
                dbu_per_hour, rate_ev = fixed, c.dbu_per_hour_evidence
                rate_basis = c.dbu_per_hour_basis or "TEAM_FIXED_DBU_PER_HOUR"
            elif hist is not None:
                dbu_per_hour, rate_ev, rate_basis = hist, Evidence.CONFIRMED, "HISTORICALLY_CALIBRATED_DBU_PER_HOUR"
            else:
                dbu_per_hour, rate_ev, rate_basis = a.batch_dbu_per_hour, Evidence.PLANNING_ASSUMPTION, "PLANNING_DEFAULT_DBU_PER_HOUR"
            dbu = runs * runtime * dbu_per_hour * c.quantity
            basis = "NO_RUN_MODELED" if runs == 0 else rate_basis
            if runs_ev == Evidence.PLANNING_ASSUMPTION and runs > 0:
                basis += " | RUN_FREQUENCY_UNCONFIRMED"
            # Weakest evidence wins: frequency and intensity must both be confirmed.
            ev = Evidence.CONFIRMED if (runs_ev == Evidence.CONFIRMED and rate_ev == Evidence.CONFIRMED) else Evidence.PLANNING_ASSUMPTION
            warn = (["No run modeled in this scenario; this is not a forecast of zero cost."] if runs == 0 else [])
            items.append(_price(sc, c.component_id, "DATA_PROCESSING", desc, dbu, "DBU", "DATABRICKS", d.sku,
                                req.region, pricing, Recurrence.MONTHLY, ev, basis,
                                f"{_fmt(runs)} runs x {_fmt(runtime)} h x {_fmt(dbu_per_hour)} DBU/h x {c.quantity}", warn))
    return items, decisions


# ---- 6.2 SQL reporting -------------------------------------------------------------------

def engine_sql(req: EstimateRequest, pricing: PricingProvider,
               calibration: Optional[CalibrationData]) -> List[LineItem]:
    """SQL Pro DBUs for reporting.

    Planning formula: active hours/day x working days x DBU/hour per band x capacity bands,
    where modeled users = ceil(concurrent users x scenario factor) and
    bands = ceil(modeled users / users per band).
    Historical values replace the formula only when concurrency is validated.
    """
    s, a = req.sql, req.assumptions
    if s is None:
        return []
    items = []
    for sc in req.scenarios:
        users = max(1, math.ceil(s.concurrent_users * a.sql_concurrency_factor[sc]))
        bands = max(1, math.ceil(users / a.sql_users_per_capacity_band))
        hist = _calibrated(calibration.sql_monthly_dbu if calibration else None,
                           calibration.sql_peers if calibration else 0, sc, a.min_calibration_peers)
        if hist is not None and calibration and calibration.ten_concurrent_users_validated is True:
            dbu, ev, basis = hist, Evidence.CONFIRMED, "HISTORICALLY_CALIBRATED_CONCURRENCY_VALIDATED"
            formula = f"calibrated {_fmt(hist)} DBU/month"
        else:
            hours = a.sql_active_hours_per_day[sc]
            dbu = hours * s.working_days_per_month * a.sql_dbu_per_hour_per_band * bands
            ev, basis = Evidence.PLANNING_ASSUMPTION, "CONCURRENCY_AWARE_PLANNING_DEFAULT"
            formula = (f"{_fmt(hours)} h/day x {s.working_days_per_month} days x {_fmt(a.sql_dbu_per_hour_per_band)} DBU/h"
                       f" x {bands} band(s) [{users} modeled users]")
        items.append(_price(sc, s.component_id, "SQL_REPORTING", f"{s.name} (SQL_PRO)", dbu, "DBU", "DATABRICKS",
                            "SQL_PRO", req.region, pricing, Recurrence.MONTHLY, ev, basis, formula,
                            ["SQL capacity bands are a planning assumption, not a SQL Pro capacity guarantee."]))
    return items


# ---- 6.3 Data transfer -------------------------------------------------------------------

def engine_data_transfer(req: EstimateRequest, pricing: PricingProvider) -> List[LineItem]:
    """AWS DataSync: recurring daily GB x transfer days, plus an optional one-time migration."""
    t = req.transfer
    if t is None:
        return []
    items = []
    for sc in req.scenarios:
        daily = _by_scenario(sc, t.daily_gb_low, t.daily_gb_expected, t.daily_gb_high)
        gb = None if daily is None else daily * t.transfer_days_per_month
        items.append(_price(sc, "TRANSFER-RECURRING", "DATA_TRANSFER", "Recurring AWS DataSync transfer", gb, "GB",
                            "AWS", "DataSync-Bytes", req.region, pricing, Recurrence.MONTHLY, Evidence.CONFIRMED,
                            "TEAM_SUPPLIED_DAILY_VOLUME",
                            "daily volume" if daily is None else f"{_fmt(daily)} GB/day x {t.transfer_days_per_month} days"))
        if t.initial_transfer_gb is not None:
            items.append(_price(sc, "TRANSFER-MIGRATION", "DATA_TRANSFER", "One-time migration via AWS DataSync",
                                t.initial_transfer_gb, "GB", "AWS", "DataSync-Bytes", req.region, pricing,
                                Recurrence.ONE_TIME, Evidence.CONFIRMED, "TEAM_SUPPLIED_MIGRATION_VOLUME",
                                "one-time migration volume"))
    return items


# ---- 6.4 AWS storage and encryption ------------------------------------------------------

def engine_aws_storage(req: EstimateRequest, pricing: PricingProvider) -> List[LineItem]:
    """S3 storage, S3 write and read requests, KMS keys, and KMS requests.

    Storage is the same in every scenario (retained volume is a team-supplied value).
    Read requests and decrypt-on-read stay PENDING until the read pattern is known.
    """
    s = req.storage
    if s is None:
        return []
    items = []
    req_ev = Evidence.CONFIRMED if s.request_assumptions_confirmed else Evidence.PLANNING_ASSUMPTION
    for sc in req.scenarios:
        gb = None if s.retained_tb is None else s.retained_tb * 1000.0
        items.append(_price(sc, "S3-STORAGE", "AWS_STORAGE", "S3 Standard retained storage", gb, "GB_MONTH", "AWS",
                            "TimedStorage-ByteHrs", req.region, pricing, Recurrence.MONTHLY, Evidence.CONFIRMED,
                            "TEAM_SUPPLIED_RETAINED_STORAGE", f"{s.retained_tb} TB x 1,000 GB"))
        files = None if s.files_per_day is None else s.files_per_day * s.days_per_month
        writes = None if files is None or s.s3_write_requests_per_file is None else files * s.s3_write_requests_per_file / 1000
        items.append(_price(sc, "S3-WRITE-REQUESTS", "AWS_STORAGE", "S3 write/list requests (Tier 1)", writes,
                            "REQUEST_1000", "AWS", "Requests-Tier1", req.region, pricing, Recurrence.MONTHLY, req_ev,
                            "FILES_X_WRITES_PER_FILE",
                            f"{s.files_per_day} files/day x {s.days_per_month} days x {s.s3_write_requests_per_file} write/file / 1,000"))
        if s.s3_read_requests_per_file is None:
            items.append(_pending(sc, "S3-READ-REQUESTS", "AWS_STORAGE", "S3 read requests (Tier 2)", "REQUEST_1000",
                                  Recurrence.MONTHLY, "READ_PATTERN_REQUIRED",
                                  "S3 read frequency not supplied; pending, not zero."))
        else:
            reads = None if files is None else files * s.s3_read_requests_per_file / 1000
            items.append(_price(sc, "S3-READ-REQUESTS", "AWS_STORAGE", "S3 read requests (Tier 2)", reads,
                                "REQUEST_1000", "AWS", "Requests-Tier2", req.region, pricing, Recurrence.MONTHLY,
                                req_ev, "FILES_X_READS_PER_FILE",
                                f"{s.files_per_day} files/day x {s.days_per_month} days x {s.s3_read_requests_per_file} reads/file / 1,000"))
        if s.kms_keys:
            items.append(_price(sc, "KMS-KEYS", "AWS_STORAGE", "KMS customer-managed keys", s.kms_keys, "KEY_MONTH",
                                "AWS", "KMS-Keys", req.region, pricing, Recurrence.MONTHLY, Evidence.CONFIRMED,
                                "TEAM_SUPPLIED_KEY_COUNT", "key count"))
        if s.encrypted_files_per_day:
            enc = s.encrypted_files_per_day * s.days_per_month
            kms_req = None if s.kms_requests_per_encrypted_file is None else enc * s.kms_requests_per_encrypted_file / 10000
            items.append(_price(sc, "KMS-ENCRYPT-REQUESTS", "AWS_STORAGE", "KMS encryption requests", kms_req,
                                "REQUEST_10000", "AWS", "KMS-Requests", req.region, pricing, Recurrence.MONTHLY,
                                req_ev, "ENCRYPTED_FILES_X_REQUESTS_PER_FILE",
                                f"{_fmt(s.encrypted_files_per_day)} files/day x {s.days_per_month} days x "
                                f"{s.kms_requests_per_encrypted_file} request/file / 10,000"))
            if s.s3_read_requests_per_file is None:
                items.append(_pending(sc, "KMS-DECRYPT-REQUESTS", "AWS_STORAGE", "KMS decrypt requests on read",
                                      "REQUEST_10000", Recurrence.MONTHLY, "READ_PATTERN_REQUIRED",
                                      "KMS decrypt-on-read depends on read frequency; pending, not zero."))
            else:
                dec = enc * s.s3_read_requests_per_file * (s.kms_requests_per_encrypted_file or 0) / 10000
                items.append(_price(sc, "KMS-DECRYPT-REQUESTS", "AWS_STORAGE", "KMS decrypt requests on read", dec,
                                    "REQUEST_10000", "AWS", "KMS-Requests", req.region, pricing, Recurrence.MONTHLY,
                                    req_ev, "ENCRYPTED_READS_X_REQUESTS", "encrypted reads / 10,000"))
    return items


# ---- 6.5 Model API (pay per token) -------------------------------------------------------

def _token_value(sc, low, expected, high):
    """Return (tokens, evidence, basis). Expected falls back to the midpoint of a supplied
    range and is labeled; it is never silently invented. STRESS uses HIGH."""
    if sc == "EXPECTED":
        if expected is not None:
            return float(expected), Evidence.CONFIRMED, "TEAM_SUPPLIED_EXPECTED"
        if low is not None and high is not None:
            return (low + high) / 2.0, Evidence.RANGE_DERIVED, "MIDPOINT_OF_SUPPLIED_RANGE"
        return None, Evidence.PLANNING_ASSUMPTION, "EXPECTED_TOKENS_REQUIRED"
    value = low if sc == "LOW" else high
    return (None if value is None else float(value)), Evidence.RANGE_DERIVED, ("SUPPLIED_LOW" if sc == "LOW" else "SUPPLIED_HIGH")


def price_tokens(scenario, component_id, description, model, serving_mode, input_tokens, output_tokens,
                 region, pricing: PricingProvider, recurrence, evidence, basis, cache_tokens: float = 0.0) -> LineItem:
    """tokens / 1,000,000 x model DBUs per 1M tokens = DBUs; DBUs x account USD/DBU = cost."""
    if serving_mode != "PAY_PER_TOKEN":
        return _pending(scenario, component_id, "MODEL_API", description, "DBU", recurrence, basis,
                        f"{serving_mode} is not priced by the token formula.")
    if input_tokens is None or output_tokens is None:
        return _pending(scenario, component_id, "MODEL_API", description, "DBU", recurrence, basis,
                        "Token quantity missing; output is never inferred from input.")
    conv, warn = pricing.get_model_conversion(model, serving_mode)
    if conv is None:
        return _pending(scenario, component_id, "MODEL_API", description, "DBU", recurrence, basis, warn)
    if conv.input_dbu_per_million is None or conv.output_dbu_per_million is None or (
            cache_tokens and conv.cache_dbu_per_million is None):
        return _pending(scenario, component_id, "MODEL_API", description, "DBU", recurrence, basis,
                        f"Conversion for {model} is incomplete.")
    dbu = (input_tokens / 1e6 * conv.input_dbu_per_million
           + output_tokens / 1e6 * conv.output_dbu_per_million
           + cache_tokens / 1e6 * (conv.cache_dbu_per_million or 0.0))
    prefix = (f"{model}: {_fmt(input_tokens)} input tokens x {_fmt(conv.input_dbu_per_million)} + "
              f"{_fmt(output_tokens)} output tokens x {_fmt(conv.output_dbu_per_million)} DBU per 1M")
    return _price(scenario, component_id, "MODEL_API", description, dbu, "DBU", "DATABRICKS", conv.account_sku,
                  region, pricing, recurrence, evidence, basis, prefix)


def engine_model_api(req: EstimateRequest, pricing: PricingProvider) -> List[LineItem]:
    """Recurring endpoint calls and one-time token jobs (for example, corpus indexing)."""
    a, items = req.assumptions, []
    for sc in req.scenarios:
        tsc = "HIGH" if sc == "STRESS" else sc
        for e in req.model_endpoints:
            calls = e.calls_per_day * (a.model_stress_call_multiplier if sc == "STRESS" else 1.0)
            it, iev, ib = _token_value(tsc, e.input_tokens_low, e.input_tokens_expected, e.input_tokens_high)
            ot, oev, ob = _token_value(tsc, e.output_tokens_low, e.output_tokens_expected, e.output_tokens_high)
            monthly_calls = calls * req.model_days_per_month
            weakest = Evidence.PLANNING_ASSUMPTION if Evidence.PLANNING_ASSUMPTION in (iev, oev) else (
                Evidence.RANGE_DERIVED if Evidence.RANGE_DERIVED in (iev, oev) else Evidence.CONFIRMED)
            items.append(price_tokens(
                sc, e.component_id, f"{e.role} ({e.model})", e.model, e.serving_mode,
                None if it is None else monthly_calls * it, None if ot is None else monthly_calls * ot,
                req.region, pricing, Recurrence.MONTHLY, weakest,
                f"input={ib}; output={ob}; {_fmt(calls)} calls/day x {req.model_days_per_month} days"))
        for j in req.one_time_model_jobs:
            it, _, ib = _token_value(tsc, j.input_tokens_low, None, j.input_tokens_high)
            ot, _, ob = _token_value(tsc, j.output_tokens_low, None, j.output_tokens_high)
            items.append(price_tokens(sc, j.component_id, f"{j.description} ({j.model})", j.model, j.serving_mode,
                                      it, ot, req.region, pricing, Recurrence.ONE_TIME,
                                      Evidence.RANGE_DERIVED, f"input={ib}; output={ob}"))
    return items


def compare_models(endpoint: ModelEndpoint, models: List[str], days_per_month: int = 30,
                   region: str = "US_EAST_1", pricing: Optional[PricingProvider] = None) -> List[Dict[str, Any]]:
    """Price the same call pattern on alternative models. Alternatives are mutually
    exclusive and must never be summed."""
    pricing = pricing or RegistryPricingProvider()
    rows = []
    for m in models:
        row: Dict[str, Any] = {"model": m}
        for sc in ("LOW", "EXPECTED", "HIGH"):
            it, _, _ = _token_value(sc, endpoint.input_tokens_low, endpoint.input_tokens_expected, endpoint.input_tokens_high)
            ot, _, _ = _token_value(sc, endpoint.output_tokens_low, endpoint.output_tokens_expected, endpoint.output_tokens_high)
            monthly = endpoint.calls_per_day * days_per_month
            li = price_tokens(sc, endpoint.component_id, endpoint.role, m, endpoint.serving_mode,
                              None if it is None else monthly * it, None if ot is None else monthly * ot,
                              region, pricing, Recurrence.MONTHLY, Evidence.RANGE_DERIVED, "COMPARISON")
            row[f"{sc.lower()}_usd"] = li.cost_usd
        row["status"] = "PRICED" if all(row[k] is not None for k in ("low_usd", "expected_usd", "high_usd")) else "PENDING"
        rows.append(row)
    return rows


# =============================================================================
# PART 7  ORCHESTRATION
# =============================================================================

def estimate(req: EstimateRequest, pricing: Optional[PricingProvider] = None,
             calibration: Optional[CalibrationData] = None) -> "EstimateResult":
    """THE single entry point. Validate, run every applicable engine, roll up totals.

    Same request + same pricing + same calibration -> same result.
    """
    validate_request(req)
    pricing = pricing or RegistryPricingProvider()
    calibration_ignored = calibration is not None and not req.calibration_allowed
    if not req.calibration_allowed:
        calibration = None

    processing_items, decisions = engine_data_processing(req, pricing, calibration)
    items = (processing_items
             + engine_sql(req, pricing, calibration)
             + engine_data_transfer(req, pricing)
             + engine_aws_storage(req, pricing)
             + engine_model_api(req, pricing))

    totals = {sc: _scenario_total(sc, [i for i in items if i.scenario == sc]) for sc in req.scenarios}
    ref = "EXPECTED" if "EXPECTED" in req.scenarios else req.scenarios[0]

    missing = sorted({f"{i.component_id}: {w}" for i in items if i.treatment == Treatment.PENDING for w in i.warnings})
    warnings = sorted({w for i in items if i.scenario == ref and i.treatment == Treatment.INCLUDED for w in i.warnings})
    if calibration_ignored:
        warnings.append("Historical calibration was supplied but ignored: this request is locked (calibration_allowed=False).")
    assumptions_used = sorted({f"{i.component_id} [{i.evidence}]: {i.basis}" for i in items
                               if i.scenario == ref and i.evidence != Evidence.CONFIRMED
                               and i.treatment == Treatment.INCLUDED})

    return EstimateResult(
        calculator_version=CALCULATOR_VERSION,
        use_case_id=req.use_case_id,
        use_case_name=req.use_case_name,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        scenarios=list(req.scenarios),
        line_items=items,
        totals=totals,
        compute_decisions=decisions,
        overall_confidence=_overall_confidence(items, req.scenarios),
        missing_inputs=missing,
        assumptions_used=assumptions_used,
        warnings=warnings,
        calibration_used=calibration is not None,
        scope_included=list(req.scope_included),
        scope_excluded=list(req.scope_excluded),
        notes=list(req.notes),
        metadata=dict(req.metadata),
    )


@dataclass
class ScenarioTotal:
    scenario: str
    recurring_monthly_usd: float
    annual_recurring_usd: float
    one_time_usd: float
    first_year_usd: float
    databricks_dbu_monthly: float
    databricks_monthly_usd: float
    aws_monthly_usd: float
    pending_recurring_items: int
    pending_one_time_items: int
    status: str


def _scenario_total(sc: str, items: List[LineItem]) -> ScenarioTotal:
    """Totals only ever add INCLUDED lines. PENDING lines are counted, never summed.

        recurring monthly = sum of included monthly lines
        annual recurring  = 12 x recurring monthly           (Year 2 onward)
        first year        = 12 x recurring monthly + one-time (one-time counted once)
    """
    inc = [i for i in items if i.treatment == Treatment.INCLUDED]
    rec = [i for i in inc if i.recurrence == Recurrence.MONTHLY]
    one = [i for i in inc if i.recurrence == Recurrence.ONE_TIME]
    monthly = sum(i.cost_usd for i in rec)
    one_time = sum(i.cost_usd for i in one)
    dbx = [i for i in rec if i.unit == "DBU"]
    dbx_usd = sum(i.cost_usd for i in dbx)
    pend_rec = sum(1 for i in items if i.treatment == Treatment.PENDING and i.recurrence == Recurrence.MONTHLY)
    pend_one = sum(1 for i in items if i.treatment == Treatment.PENDING and i.recurrence == Recurrence.ONE_TIME)
    return ScenarioTotal(sc, monthly, monthly * 12, one_time, monthly * 12 + one_time,
                         sum(i.quantity for i in dbx), dbx_usd, monthly - dbx_usd, pend_rec, pend_one,
                         "COMPLETE" if pend_rec + pend_one == 0 else "PARTIAL_PENDING_INPUTS")


def _overall_confidence(items: List[LineItem], scenarios: List[str]) -> Dict[str, Any]:
    """Confidence reflects how much of the reference-scenario recurring cost rests on
    planning assumptions. Rate quality does not raise it.

        planning-assumption share >= 50%           -> LOW
        planning + range-derived share >= 50%      -> MEDIUM
        otherwise                                  -> HIGH
    Any pending recurring item caps confidence at MEDIUM.
    """
    ref = "EXPECTED" if "EXPECTED" in scenarios else scenarios[0]
    rec = [i for i in items if i.scenario == ref and i.recurrence == Recurrence.MONTHLY]
    priced = [i for i in rec if i.treatment == Treatment.INCLUDED]
    total = sum(i.cost_usd for i in priced)
    if not total:
        return {"rating": "LOW", "reference_scenario": ref, "reason": "No recurring cost could be priced."}
    planning = sum(i.cost_usd for i in priced if i.evidence == Evidence.PLANNING_ASSUMPTION) / total
    ranged = sum(i.cost_usd for i in priced if i.evidence == Evidence.RANGE_DERIVED) / total
    rating = "LOW" if planning >= 0.5 else ("MEDIUM" if planning + ranged >= 0.5 else "HIGH")
    if rating == "HIGH" and any(i.treatment == Treatment.PENDING for i in rec):
        rating = "MEDIUM"
    return {"rating": rating, "reference_scenario": ref,
            "planning_assumption_share": round(planning, 4), "range_derived_share": round(ranged, 4),
            "reason": f"{planning:.0%} of {ref} recurring cost rests on planning assumptions; "
                      f"{ranged:.0%} on team-supplied ranges."}


# =============================================================================
# PART 8  OUTPUT
# =============================================================================

@dataclass
class EstimateResult:
    calculator_version: str
    use_case_id: str
    use_case_name: str
    generated_at: str
    scenarios: List[str]
    line_items: List[LineItem]
    totals: Dict[str, ScenarioTotal]
    compute_decisions: List[ComputeDecision]
    overall_confidence: Dict[str, Any]
    missing_inputs: List[str]
    assumptions_used: List[str]
    warnings: List[str]
    calibration_used: bool
    scope_included: List[str]
    scope_excluded: List[str]
    notes: List[str]
    metadata: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    def line_item_rows(self) -> List[Dict[str, Any]]:
        return [{**asdict(i), "warnings": "; ".join(i.warnings)} for i in self.line_items]

    def total_rows(self) -> List[Dict[str, Any]]:
        return [asdict(self.totals[s]) for s in self.scenarios]

    def decision_rows(self) -> List[Dict[str, Any]]:
        return [{**asdict(d), "eligible": ", ".join(d.eligible)} for d in self.compute_decisions]

    def cost_by_component(self, scenario: str, engines: Optional[Tuple[str, ...]] = None,
                          component_ids: Optional[Tuple[str, ...]] = None, recurrence: str = Recurrence.MONTHLY) -> float:
        """Sum of included cost for a filtered slice of one scenario."""
        return sum(i.cost_usd for i in self.line_items
                   if i.scenario == scenario and i.treatment == Treatment.INCLUDED and i.recurrence == recurrence
                   and (engines is None or i.engine in engines)
                   and (component_ids is None or i.component_id in component_ids))


def _money(v: Optional[float]) -> str:
    return "PENDING" if v is None else f"${v:,.2f}"


def format_summary(result: EstimateResult) -> str:
    """Plain-language summary suitable for an email, a deck note, or an agent reply."""
    t = result.totals
    ref = result.overall_confidence["reference_scenario"]
    lines = [f"DCP Smart Estimation Calculator v{result.calculator_version}",
             f"Use case: {result.use_case_id} | {result.use_case_name}", ""]
    lines.append("Recurring monthly planning range:")
    for sc in result.scenarios:
        tag = " (operating sensitivity, not a forecast)" if sc == "STRESS" else ""
        lines.append(f"  {sc:<9}{_money(t[sc].recurring_monthly_usd):>14}/month{tag}")
    if any(t[sc].one_time_usd or t[sc].pending_one_time_items for sc in result.scenarios):
        lines.append("One-time costs (separate from recurring):")
        for sc in result.scenarios:
            lines.append(f"  {sc:<9}{_money(t[sc].one_time_usd):>14}")
    lines += ["", f"First-year total ({ref}): {_money(t[ref].first_year_usd)} = 12 x recurring + one-time",
              f"Annual recurring, Year 2 onward ({ref}): {_money(t[ref].annual_recurring_usd)}",
              f"Overall confidence: {result.overall_confidence['rating']} - {result.overall_confidence['reason']}"]
    if result.missing_inputs:
        lines += ["", "Pending (not counted, not zero):"] + [f"  - {m}" for m in result.missing_inputs]
    if result.assumptions_used:
        lines += ["", f"Assumptions behind the {ref} figure:"] + [f"  - {a}" for a in result.assumptions_used]
    if result.warnings:
        lines += ["", "Notes:"] + [f"  - {w}" for w in result.warnings]
    if result.scope_included:
        lines += ["", "Included scope: " + "; ".join(result.scope_included)]
    if result.scope_excluded:
        lines += ["Excluded scope: " + "; ".join(result.scope_excluded)]
    lines += ["", "Internal planning estimate, not an invoice or committed run rate."]
    return "\n".join(lines)


# =============================================================================
# PART 9  PRESETS
# A preset only builds an EstimateRequest. It contains the team's inputs and nothing else.
# To add a preset: write a builder function and register it in PRESETS.
# =============================================================================

# Backfill intensity locked from the presented SEND estimate (deck slide 7), which used
# historical calibration of comparable batch jobs at the time:
#   Expected: 0.55 DBUs for one 3-hour run   High/Stress: 1.56 DBUs per 5-hour run
SEND_BACKFILL_DBU_PER_HOUR = {"LOW": 0.312, "EXPECTED": 0.55 / 3, "HIGH": 1.56 / 5, "STRESS": 1.56 / 5}
SEND_CONTINUOUS_DBU_PER_HOUR = {"LOW": 0.5, "EXPECTED": 1.0, "HIGH": 1.5, "STRESS": 2.0}


def preset_send_us01(retained_storage_tb: float = 10.0, files_per_day: float = 200,
                     encrypted_files_per_day: float = 40, kms_keys: float = 1,
                     migration_gb: float = 10000, s3_read_requests_per_file: Optional[float] = None,
                     scenarios: Optional[List[str]] = None) -> EstimateRequest:
    """SEND_US01 Reporting and Reconciliation (OZONE -> AWS S3 for Databricks).

    Locked to the compute assumptions SEND accepted on 1 Oct 2026, so it reproduces the
    presented figures on every run. Always exactly two processing components: one
    continuous streaming workload and one event-driven resiliency/backfill batch that runs
    1-5 hours when needed (frequency unconfirmed). Storage, file, and KMS values come from
    SEND's 1 Oct 2026 follow-up.
    """
    return EstimateRequest(
        use_case_id="SEND_US01",
        use_case_name="Reporting and Reconciliation",
        scenarios=scenarios or list(SCENARIOS_ALL),
        calibration_allowed=False,
        processing=[
            ProcessingComponent("SEND_US01-C01", "Continuous streaming data processing", "CONTINUOUS_STREAMING",
                                pattern="CONTINUOUS",
                                dbu_per_hour=dict(SEND_CONTINUOUS_DBU_PER_HOUR),
                                dbu_per_hour_basis="PRESENTED_PLANNING_INTENSITY",
                                dbu_per_hour_evidence=Evidence.PLANNING_ASSUMPTION,
                                evidence="One continuously running workload supporting Bronze, Silver, and Gold."),
            ProcessingComponent("SEND_US01-C02", "Event-driven batch resiliency and backfill", "EVENT_DRIVEN_BATCH",
                                pattern="AS_NEEDED", runtime_hours_low=1, runtime_hours_expected=3,
                                runtime_hours_high=5,
                                dbu_per_hour=dict(SEND_BACKFILL_DBU_PER_HOUR),
                                dbu_per_hour_basis="LOCKED_FROM_PRESENTED_HISTORICAL_CALIBRATION",
                                dbu_per_hour_evidence=Evidence.CONFIRMED,
                                evidence="Runs 1 to 5 hours when backfill is needed; frequency unconfirmed."),
        ],
        sql=SqlReporting("SEND_US01-SQL", "Daily and on-demand reporting", concurrent_users=10,
                         working_days_per_month=22, total_users=50),
        transfer=DataTransfer(daily_gb_low=6, daily_gb_expected=8, daily_gb_high=10, transfer_days_per_month=22,
                              initial_transfer_gb=migration_gb,
                              evidence="6-8 GB/day today, 10 GB/day upper planning value next year."),
        storage=AwsStorage(retained_tb=retained_storage_tb, files_per_day=files_per_day,
                           encrypted_files_per_day=encrypted_files_per_day, kms_keys=kms_keys, days_per_month=30,
                           s3_write_requests_per_file=1, s3_read_requests_per_file=s3_read_requests_per_file,
                           kms_requests_per_encrypted_file=1, request_assumptions_confirmed=False,
                           evidence="SEND supplied 8-10 TB retained, 200 files/day, 40 encrypted files/day, 1 KMS key."),
        scope_included=["Databricks processing and SQL", "AWS DataSync", "S3 storage and requests", "KMS"],
        scope_excluded=["Non-production environments", "AXON/MSK allocation", "Replacement processing service"],
        notes=["1 S3 write request per file and 1 KMS request per encrypted file are assumptions."],
        metadata={"source": "SEND intake and 1 Oct 2026 follow-up",
                  "reference_values": {
                      # Core = processing + SQL + recurring DataSync, from deck slide 7.
                      "core_monthly_usd": {"LOW": 146.76, "EXPECTED": 292.60, "HIGH": 531.78, "STRESS": 808.95},
                      # All-in monthly at 10 TB, from deck slide 10.
                      "high_all_in_monthly_usd": 762.81,
                      "high_first_year_usd": 9278.76}},
    )


def preset_ai_rag_model_api() -> EstimateRequest:
    """MD AI Assistant RAG (Developer Experience). Databricks model inference and
    embeddings only; retrieval, vector search, and application compute run on premises."""
    return EstimateRequest(
        use_case_id="AI_RAG_MODEL_API",
        use_case_name="MD AI Assistant RAG",
        scenarios=["LOW", "EXPECTED", "HIGH"],
        model_days_per_month=30,
        model_endpoints=[
            ModelEndpoint("MD-AI-C01", "gpt-5.4-nano", "Router, rerank, answerability", 330, 500, None, 10000, 50, None, 300),
            ModelEndpoint("MD-AI-C02", "gte-large-en", "Query and incremental chunk embeddings", 300, 50, None, 600, 0, 0, 0),
            ModelEndpoint("MD-AI-C03", "gpt-5-4", "Indexing enrichment", 215, 1000, None, 4000, 100, None, 2000),
            ModelEndpoint("MD-AI-C04", "claude-sonnet-4-6", "Final answer", 132, 5000, None, 40000, 300, None, 1500),
        ],
        one_time_model_jobs=[
            OneTimeModelJob("MD-AI-OT-GPT", "gpt-5-4", "Full-corpus indexing enrichment",
                            70_000_000, 75_000_000, 22_000_000, 24_000_000, api_calls=42_600),
            OneTimeModelJob("MD-AI-OT-EMBED", "gte-large-en", "Full-corpus embeddings",
                            33_000_000, 33_000_000, 0, 0, api_calls=15_000),
        ],
        scope_included=["Databricks model inference", "Embedding generation"],
        scope_excluded=["On-premises indexing orchestration", "Retrieval", "Vector storage and search",
                        "Application compute", "General storage"],
        notes=["990 endpoint calls/day stated; 977 allocated; 13/day unallocated and not assigned.",
               "Expected tokens are midpoints of the supplied ranges until first-run actuals exist."],
        metadata={"owner": "Marco Rocco", "product": "Developer Experience (Mastercard Developer)",
                  "stated_calls_per_day": 990,
                  "final_answer_alternatives": ["claude-sonnet-4-6", "gpt-5-4", "gpt-5.4-nano"]},
    )


def new_workload_template() -> Dict[str, Any]:
    """Blank request for a new workload. Fill in, delete unused sections, then
    EstimateRequest.from_dict(...). Keys starting with '_' are comments and are ignored."""
    return {
        "_how_to_use": "Fill in values. Use null for unknown values (they stay PENDING). Delete sections you do not need.",
        "use_case_id": "NEW_WORKLOAD_01",
        "use_case_name": "Describe the workload",
        "scenarios": ["LOW", "EXPECTED", "HIGH"],
        "region": "US_EAST_1",
        "processing": [{
            "_workload_types": list(WORKLOAD_TYPES),
            "component_id": "NEW-C01", "name": "Daily batch ingestion", "workload_type": "SCHEDULED_BATCH",
            "pattern": "SCHEDULED", "runtime_hours_low": 0.5, "runtime_hours_expected": 1.0,
            "runtime_hours_high": 2.0, "runs_per_month": 30, "evidence": "Who supplied these values"}],
        "sql": {"component_id": "NEW-SQL", "name": "Dashboards", "concurrent_users": 5, "working_days_per_month": 22},
        "transfer": {"daily_gb_low": 1, "daily_gb_expected": 2, "daily_gb_high": 4, "transfer_days_per_month": 22,
                     "initial_transfer_gb": None},
        "storage": {"retained_tb": 1.0, "files_per_day": 50, "encrypted_files_per_day": 0, "kms_keys": 0,
                    "days_per_month": 30, "s3_write_requests_per_file": 1, "s3_read_requests_per_file": None},
        "model_endpoints": [],
        "one_time_model_jobs": [],
        "assumptions": {"_note": "Optional overrides of PlanningAssumptions, e.g. {\"batch_dbu_per_hour\": 2.0}"},
        "scope_included": [], "scope_excluded": [], "notes": [],
    }


PRESETS = {
    "SEND_US01": (preset_send_us01, "Streaming + backfill batch + SQL + DataSync + S3/KMS (locked to presented estimate)"),
    "AI_RAG_MODEL_API": (preset_ai_rag_model_api, "Pay-per-token model inference and embeddings"),
}


def list_presets() -> Dict[str, str]:
    return {k: v[1] for k, v in PRESETS.items()}


def load_preset(name: str, **overrides) -> EstimateRequest:
    if name not in PRESETS:
        raise ValueError(f"Unknown preset {name!r}. Available: {sorted(PRESETS)}")
    return PRESETS[name][0](**overrides)


# =============================================================================
# PART 10  OPTIONAL DATABRICKS HELPERS (require Spark; imported lazily)
# =============================================================================

class DatabricksAccountRateOverlay:
    """Use an exact, currently active row from system.billing.account_prices when exactly
    one distinct price exists; otherwise defer to the base provider (the registry).

    Exact-match rules (same as the registry):
      * provider DATABRICKS and unit DBU only; AWS lookups always go to the base provider
      * region must equal the overlay's region (the account and workspace region);
        any other region defers to the base provider, which fails closed
      * the row must be in effect now (price_start_time <= now < price_end_time)
      * cloud AWS, currency USD, and usage unit DBU when those columns exist
    rate_column is the one price column read. It is not guessed: confirm which column
    holds the account rate (for example $0.3465 for Jobs Serverless) with compare_to_registry().
    """

    def __init__(self, spark, base: Optional[PricingProvider] = None, region: str = "US_EAST_1",
                 rate_column: str = "pricing.default"):
        self.spark, self.base = spark, base or RegistryPricingProvider()
        self.region = region.upper()
        self.rate_column = rate_column
        self._cache: Dict[str, Optional[float]] = {}

    def _lookup(self, sku: str) -> Optional[float]:
        if sku in self._cache:
            return self._cache[sku]
        value = None
        try:
            from pyspark.sql import functions as F
            ap = self.spark.table("system.billing.account_prices")
            q = ap.where(F.upper(F.col("sku_name")) == sku.upper())
            if "price_start_time" in ap.columns:
                q = q.where(F.col("price_start_time") <= F.current_timestamp())
            if "price_end_time" in ap.columns:
                q = q.where(F.col("price_end_time").isNull() | (F.col("price_end_time") > F.current_timestamp()))
            for col_name, expected in (("cloud", "AWS"), ("currency_code", "USD"), ("usage_unit", "DBU")):
                if col_name in ap.columns:
                    q = q.where(F.upper(F.col(col_name)) == expected)
            for col in (self.rate_column,):
                try:
                    vals = [r[0] for r in q.select(F.col(col).cast("double")).where(F.col(col).isNotNull()).distinct().collect()]
                    if len(vals) == 1:
                        value = float(vals[0])
                        break
                except Exception:
                    continue
        except Exception:
            value = None
        self._cache[sku] = value
        return value

    def get_rate(self, provider, sku, unit, region) -> RateQuote:
        if provider.upper() == "DATABRICKS" and unit.upper() == "DBU" and region.upper() == self.region:
            live = self._lookup(sku)
            if live is not None:
                base = self.base.get_rate(provider, sku, unit, region)
                warning = None
                if base.usable and abs(base.rate - live) > 1e-9:
                    warning = (f"Live account rate for {sku} (${live:g}/DBU) differs from the approved "
                               f"rate list (${base.rate:g}/DBU); results will not match presented figures.")
                return RateQuote(live, RateStatus.GOVERNED,
                                 f"system.billing.account_prices (live, {self.rate_column})", warning)
        return self.base.get_rate(provider, sku, unit, region)

    def compare_to_registry(self) -> List[Dict[str, Any]]:
        """One row per Databricks DBU rate in the approved list: approved vs live. Use it to
        confirm rate_column holds the account rate before relying on live rates."""
        base = self.base if isinstance(self.base, RegistryPricingProvider) else RegistryPricingProvider()
        rows = []
        for r in base.rates:
            if r.provider == "DATABRICKS" and r.unit == "DBU" and r.region.upper() == self.region:
                live = self._lookup(r.sku)
                rows.append({"sku": r.sku, "approved_rate": r.rate, "approved_status": r.status,
                             "live_rate": live, "live_column": self.rate_column,
                             "result": ("NO SINGLE LIVE ROW" if live is None else
                                        "MATCH" if r.rate is not None and abs(r.rate - live) < 1e-9 else "DIFFERS")})
        return rows

    def get_model_conversion(self, model, serving_mode):
        return self.base.get_model_conversion(model, serving_mode)


def _percentiles(values) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    a = sorted(float(x) for x in values if x is not None and math.isfinite(float(x)))
    if not a:
        return (None, None, None)

    def q(p):
        z = (len(a) - 1) * p
        i, j = math.floor(z), math.ceil(z)
        return a[i] if i == j else a[i] + (a[j] - a[i]) * (z - i)
    return (q(.25), q(.5), q(.75))


def calibrate_from_system_tables(spark, workspace_id: str, lookback_days: int = 90) -> CalibrationData:
    """Read-only calibration from system.billing.usage and system.query.history.

    Continuous peers: jobs running >= 60 days and >= 18 hours/day (monthly DBU).
    Batch peers:      jobs running <= 10 days and <= 10 hours/day (DBU per hour).
    SQL peers:        SQL Pro warehouses (monthly DBU).
    Results depend on the 90-day window, so they change over time. Lock values in a
    preset (dbu_per_hour) once an estimate has been presented.
    """
    from pyspark.sql import functions as F
    from pyspark.sql.window import Window

    out = CalibrationData(source=f"system.billing.usage workspace {workspace_id}, {lookback_days} days")
    u = (spark.table("system.billing.usage")
         .where(F.col("workspace_id").cast("string") == workspace_id)
         .where(F.col("usage_start_time") >= F.date_sub(F.current_timestamp(), lookback_days))
         .select("usage_start_time", "usage_end_time", F.col("usage_quantity").cast("double").alias("dbu"),
                 F.upper("sku_name").alias("sku"),
                 F.coalesce(F.col("usage_metadata.job_id").cast("string"),
                            F.col("usage_metadata.cluster_id").cast("string")).alias("resource"),
                 F.col("usage_metadata.warehouse_id").cast("string").alias("warehouse")))
    jobs = (u.where(F.col("sku").contains("JOBS") & F.col("resource").isNotNull()).groupBy("resource")
            .agg(F.sum("dbu").alias("dbu"), F.countDistinct(F.to_date("usage_start_time")).alias("days"),
                 F.sum((F.unix_timestamp("usage_end_time") - F.unix_timestamp("usage_start_time")) / 3600).alias("hours"))
            .withColumn("hpd", F.col("hours") / F.greatest("days", F.lit(1))))
    cont = [r[0] for r in jobs.where((F.col("days") >= 60) & (F.col("hpd") >= 18))
            .select(F.col("dbu") * 30 / lookback_days).collect()]
    batch = [r[0] for r in jobs.where((F.col("days") <= 10) & (F.col("hpd") <= 10) & (F.col("hours") > 0))
             .select(F.col("dbu") / F.col("hours")).collect()]
    sql = [r[0] for r in u.where(F.col("sku").contains("SQL") & F.col("sku").contains("PRO") & F.col("warehouse").isNotNull())
           .groupBy("warehouse").agg((F.sum("dbu") * 30 / lookback_days).alias("m")).select("m").collect()]
    out.continuous_peers, out.continuous_monthly_dbu = len(cont), _percentiles(cont)
    out.batch_peers, out.batch_dbu_per_hour = len(batch), _percentiles(batch)
    out.sql_peers, out.sql_monthly_dbu = len(sql), _percentiles(sql)
    try:
        q = (spark.table("system.query.history").where(F.col("workspace_id").cast("string") == workspace_id)
             .where(F.col("start_time") >= F.date_sub(F.current_timestamp(), lookback_days))
             .where(F.col("end_time").isNotNull()))
        ev = (q.select(F.col("start_time").alias("t"), F.lit(1).alias("d"))
              .unionByName(q.select(F.col("end_time").alias("t"), F.lit(-1).alias("d")))
              .groupBy("t").agg(F.sum("d").alias("d")))
        w = Window.orderBy("t").rowsBetween(Window.unboundedPreceding, 0)
        out.peak_concurrent_statements = ev.withColumn("a", F.sum("d").over(w)).agg(F.max("a")).first()[0]
        if "executed_by" in q.columns:
            peak_users = q.groupBy("start_time").agg(F.countDistinct("executed_by").alias("u")).agg(F.max("u")).first()[0]
            out.ten_concurrent_users_validated = (peak_users or 0) >= 10
    except Exception:
        out.source += " | concurrency unavailable"
    return out
