# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pandas",
#   "rapidfuzz",
# ]
# ///
"""
data_profiler.py: flexible table-to-schema mapping with staged escalation.
Public functions are JSON-in / JSON-out so they can be exposed by server.py.
Private helpers (leading underscore) are ignored by the server.
"""
import re, json, difflib, warnings
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

import pandas as pd

try:
    from rapidfuzz import fuzz
    def _ratio(a, b): return fuzz.token_set_ratio(a, b) / 100.0
except ImportError:
    def _ratio(a, b): return difflib.SequenceMatcher(None, a, b).ratio()


# ----------------------------------------------------------------------------
# Schema definitions
# ----------------------------------------------------------------------------
@dataclass
class FieldSpec:
    name: str
    aliases: set[str] = field(default_factory=set)
    patterns: list[str] = field(default_factory=list)   # regex strings
    dtype_hint: Optional[str] = None                     # int|float|date|str|bool
    enum_values: Optional[set[str]] = None
    required: bool = False


@dataclass
class SchemaDef:
    name: str
    fields: list[FieldSpec]
    description: str = ""


CFG = {
    "w_required": 1.0, "w_optional": 0.4,
    "missing_required_penalty": 0.5, "unmatched_col_weight": 0.3,
    "auto_map": 0.85, "review": 0.55, "min_margin": 0.15,
    "sample_rows": 200, "col_accept": 0.45,
    "stop_tokens": {"tbl", "table", "id", "num", "no", "value", "val", "field", "col"},
}

DATE_RE = r"^\d{4}-\d{1,2}-\d{1,2}|^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"


# ----------------------------------------------------------------------------
# Built-in schemas + JSON -> dataclass converters
# ----------------------------------------------------------------------------
_BUILTIN_SCHEMAS: dict[str, SchemaDef] = {
    "customer": SchemaDef("customer", [
        FieldSpec("email", {"e_mail", "mail"}, [r"^[\w.\-]+@[\w.\-]+$"], "str", required=True),
        FieldSpec("signup_date", {"created", "joined"}, dtype_hint="date"),
        FieldSpec("status", {"state"}, enum_values={"active", "inactive"}),
    ]),
    "budget_variance": SchemaDef("budget_variance", [
        FieldSpec("fiscal_year", {"year", "fy"}, dtype_hint="int", required=True),
        FieldSpec("department", {"dept", "division"}, dtype_hint="str", required=True),
        FieldSpec("quarter", {"qtr", "q"}, dtype_hint="int"),
        FieldSpec("budget", {"budget_usd", "budgeted"}, dtype_hint="float", required=True),
        FieldSpec("forecast", {"forecast_usd", "projected"}, dtype_hint="float"),
        FieldSpec("actual", {"actual_usd", "actuals"}, dtype_hint="float"),
        FieldSpec("variance", {"variance_usd", "delta"}, dtype_hint="float"),
        FieldSpec("notes", {"comment", "memo"}, dtype_hint="str"),
    ]),
}


def _to_field(f: Any) -> FieldSpec:
    if isinstance(f, FieldSpec):
        return f
    return FieldSpec(
        name=f["name"],
        aliases=set(f.get("aliases") or []),
        patterns=list(f.get("patterns") or []),
        dtype_hint=f.get("dtype_hint"),
        enum_values={str(e) for e in f["enum_values"]} if f.get("enum_values") else None,
        required=bool(f.get("required", False)),
    )


def _to_schema(s: Any) -> SchemaDef:
    if isinstance(s, SchemaDef):
        return s
    if isinstance(s, str):
        if s not in _BUILTIN_SCHEMAS:
            raise KeyError(f"Unknown built-in schema '{s}'. Available: {sorted(_BUILTIN_SCHEMAS)}")
        return _BUILTIN_SCHEMAS[s]
    return SchemaDef(s["name"], [_to_field(f) for f in s["fields"]], s.get("description", ""))


def _jsonable(o: Any) -> Any:
    if isinstance(o, (set, frozenset)):
        return sorted(o, key=str)
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    return o


# ----------------------------------------------------------------------------
# Input normalisation
# ----------------------------------------------------------------------------
def _records_to_dataframe(payload: Any, id_column: str = "record_id") -> pd.DataFrame:
    """Flatten an Airtable-style {"records":[{"id":..,"fields":{..}}]} payload."""
    if isinstance(payload, str):
        payload = json.loads(payload)
    records = payload.get("records", []) if isinstance(payload, dict) else payload
    rows = []
    for rec in records:
        flat = dict(rec.get("fields", {}))
        if "id" in rec:
            flat[id_column] = rec["id"]
        rows.append(flat)
    return pd.DataFrame(rows)


def _to_df(table: Any) -> pd.DataFrame:
    """Accept DataFrame | list[dict] | dict-of-columns | Airtable envelope | JSON string."""
    if isinstance(table, pd.DataFrame):
        return table
    if isinstance(table, str):
        table = json.loads(table)
    if isinstance(table, dict) and "records" in table:
        return _records_to_dataframe(table)
    return pd.DataFrame(table)


# ----------------------------------------------------------------------------
# Stage 0: profiling
# ----------------------------------------------------------------------------
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")


def _infer_dtype(s: pd.Series) -> str:
    if s.empty: return "str"
    if pd.api.types.is_bool_dtype(s): return "bool"
    if pd.api.types.is_integer_dtype(s): return "int"
    if pd.api.types.is_float_dtype(s): return "float"
    if pd.api.types.is_datetime64_any_dtype(s): return "date"
    num = pd.to_numeric(s, errors="coerce")
    if num.notna().all():
        return "int" if (num % 1 == 0).all() else "float"
    if s.astype(str).str.match(DATE_RE).mean() > 0.8:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            if pd.to_datetime(s, errors="coerce").notna().mean() > 0.9:
                return "date"
    return "str"


def _profile_table(df: pd.DataFrame) -> dict:
    df = df.head(CFG["sample_rows"])
    cols = {}
    for c in df.columns:
        s = df[c].dropna()
        cols[str(c)] = {
            "norm": _norm(c),
            "dtype": _infer_dtype(s),
            "sample": s.astype(str).tolist(),
            "distinct": set(s.astype(str).unique()[:100]),
        }
    return {"columns": cols}


# ----------------------------------------------------------------------------
# Stages 1-4: header + value matching
# ----------------------------------------------------------------------------
def _header_confidence(col_norm: str, fs: FieldSpec) -> float:
    targets = {_norm(fs.name)} | {_norm(a) for a in fs.aliases}
    if col_norm in targets:
        return 1.0
    ta = set(col_norm.split("_")) - CFG["stop_tokens"]
    best = 0.0
    for t in targets:
        tb = set(t.split("_")) - CFG["stop_tokens"]
        jac = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
        contain = 0.8 if (col_norm in t or t in col_norm) else 0.0
        best = max(best, jac * 0.9, _ratio(col_norm, t) * 0.85, contain)
    return float(best)


def _value_confidence(prof: dict, fs: FieldSpec) -> float:
    sample = prof["sample"]
    if not sample: return 0.0
    score = 0.0
    if fs.patterns:
        hits = sum(any(re.match(p, v) for p in fs.patterns) for v in sample)
        score = max(score, 0.7 * hits / len(sample))
    if fs.enum_values:
        enums = {str(e) for e in fs.enum_values}
        denom = len(prof["distinct"] | enums) or 1
        score = max(score, len(prof["distinct"] & enums) / denom)
    if fs.dtype_hint and prof["dtype"] == fs.dtype_hint:
        score = max(score, 0.4)
    return float(score)


def _best_column_for_field(profile: dict, fs: FieldSpec) -> dict:
    best = {"column": None, "confidence": 0.0, "source": None}
    for col, prof in profile["columns"].items():
        hc = _header_confidence(prof["norm"], fs)
        vc = _value_confidence(prof, fs)
        conf = 1 - (1 - hc) * (1 - vc)
        src = "header+value" if min(hc, vc) >= 0.4 else ("header" if hc >= vc else "value")
        if conf > best["confidence"]:
            best = {"column": col, "confidence": round(conf, 3), "source": src}
    if best["confidence"] < CFG["col_accept"]:
        best = {"column": None, "confidence": best["confidence"], "source": None}
    return best

def _score_schema(profile: dict, schema: SchemaDef) -> dict:
    total_w = matched_w = penalties = 0.0
    mapping, used = {}, set()
    for fs in schema.fields:
        w = CFG["w_required"] if fs.required else CFG["w_optional"]
        total_w += w
        m = _best_column_for_field(profile, fs)
        if m["column"] and m["column"] not in used:      # greedy, not global-optimal
            matched_w += w * m["confidence"]
            used.add(m["column"])
        else:
            if fs.required:
                penalties += CFG["missing_required_penalty"]
            m = {"column": None, "confidence": 0.0, "source": None}
        mapping[fs.name] = m
    unmatched = [c for c in profile["columns"] if c not in used]
    ratio = len(unmatched) / max(1, len(profile["columns"]))
    raw = (matched_w / total_w if total_w else 0.0) - penalties \
          - CFG["unmatched_col_weight"] * ratio
    return {
        "schema": schema.name,
        "score": round(max(0.0, min(1.0, raw)), 3),
        "field_mapping": mapping,
        "unmatched_columns": unmatched,
    }


def _verdict(ranked: list[dict]) -> str:
    if not ranked: return "UNMATCHED"
    top = ranked[0]["score"]
    margin = top - (ranked[1]["score"] if len(ranked) > 1 else 0.0)
    if top >= CFG["auto_map"] and margin >= CFG["min_margin"]:
        return "AUTO_MAP"
    if top >= CFG["review"]:
        return "NEEDS_REVIEW"
    return "UNMATCHED"


# ----------------------------------------------------------------------------
# Public API (exposed by server.py): JSON in, JSON out
# ----------------------------------------------------------------------------
def list_schemas() -> dict:
    """Return the built-in schemas, usable by name in triage_table."""
    return _jsonable({k: asdict(v) for k, v in _BUILTIN_SCHEMAS.items()})


def flatten_records(payload: Any, id_column: str = "record_id") -> list[dict]:
    """Flatten an Airtable-style payload into a list of flat row dicts."""
    df = _records_to_dataframe(payload, id_column)
    return json.loads(df.to_json(orient="records"))      # NaN -> null


def profile(table: Any) -> dict:
    """Profile each column: normalised name, inferred dtype, samples, distinct values."""
    df = _to_df(table)
    if df.empty or len(df.columns) == 0:
        return {"columns": {}}
    return _jsonable(_profile_table(df))


def triage_table(table: Any, schemas: Optional[list] = None,
                 table_name: str = "unnamed") -> dict:
    """Match a table against schemas.

    table:   list[dict] | dict-of-columns | Airtable envelope | JSON string
    schemas: list of built-in names and/or schema dicts
             {"name":..,"fields":[{"name":..,"aliases":[..],...}]};
             defaults to all built-in schemas.
    """
    if isinstance(schemas, str):
        schemas = json.loads(schemas)
    schema_objs = [_to_schema(s) for s in (schemas or list(_BUILTIN_SCHEMAS.values()))]

    df = _to_df(table)
    if df.empty or len(df.columns) == 0:
        return {"table": table_name, "verdict": "UNMATCHED",
                "reason": "empty table", "best_match": None, "candidates": []}

    prof = _profile_table(df)
    ranked = sorted((_score_schema(prof, s) for s in schema_objs),
                    key=lambda r: r["score"], reverse=True)
    return _jsonable({
        "table": table_name,
        "verdict": _verdict(ranked),
        "best_match": ranked[0]["schema"] if ranked else None,
        "candidates": ranked[:3],
    })


# ----------------------------------------------------------------------------
# Smoke test (only when run directly)
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    data = [{"E-Mail Address": "a@x.com", "Joined On": "2024-01-02", "State": "active"},
            {"E-Mail Address": "b@y.io", "Joined On": "2024-03-11", "State": "inactive"}]

    # Built-in schemas by name
    print(json.dumps(triage_table(data, ["customer", "budget_variance"], "loose_table_1"), indent=2))

    # Custom schema passed as JSON-style dict
    custom = {"name": "contact", "fields": [
        {"name": "email", "aliases": ["mail"], "patterns": [r"^[\w.\-]+@[\w.\-]+$"], "required": True},
    ]}
    print(json.dumps(triage_table(data, [custom, "customer"], "loose_table_2"), indent=2))