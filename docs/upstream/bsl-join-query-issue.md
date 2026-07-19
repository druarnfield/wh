# [Draft upstream issue for boringdata/boring-semantic-layer]

**Title:** YAML-defined joins fail every query on the model: "No aggregation
results and full join unavailable" (0.3.15, duckdb backend)

## Summary

Declaring a `joins:` entry in a YAML model makes **every** query against that
model raise `ValueError: No aggregation results and full join unavailable` —
including queries that don't touch the joined dimensions. Precedes the
failure: a `Grain mismatch detected ... Upgrading join_one to join_many for
automatic pre-aggregation` warning. The 0.3.14 fix for grain mismatch when
chaining `with_measures` over joined models does not cover this path.

## Repro (BSL 0.3.15, ibis-framework[duckdb] 12.0.0, duckdb 1.5.4, Python 3.13)

```python
import duckdb, ibis, yaml
import boring_semantic_layer as bsl

raw = duckdb.connect()
raw.execute("CREATE TABLE waitlist AS SELECT * FROM (VALUES "
            "('U1','Cardio','C1',40),('U2','Cardio','C2',10),('U3','Ortho','C1',60)"
            ") t(patient_ur, specialty, clinic_code, wait_days)")
raw.execute("CREATE TABLE clinics AS SELECT * FROM (VALUES "
            "('C1','North'),('C2','South')) t(code, region)")
con = ibis.duckdb.from_connection(raw)

cfg = yaml.safe_load("""
clinics:
  table: clinics
  dimensions:
    code: {expr: _.code, is_entity: true}
    region: _.region
  measures:
    n_clinics: _.count()
wl:
  table: waitlist
  dimensions:
    clinic: {expr: _.clinic_code, is_entity: true}
    specialty: _.specialty
  measures:
    patients: _.count()
  joins:
    clinics: {model: clinics, type: one, left_on: clinic, right_on: code}
""")
m = bsl.from_config(cfg, tables={"waitlist": con.table("waitlist"),
                                 "clinics": con.table("clinics")})

# 1) query through the joined dim — fails
m["wl"].group_by("clinics.region").aggregate("patients").execute()
# ValueError: No aggregation results and full join unavailable

# 2) query using ONLY the model's own dims — also fails once the join exists
m["wl"].group_by("clinic").aggregate("patients").execute()
# ValueError: No aggregation results and full join unavailable

# 3) prefixed measure "succeeds" but silently drops the group_by
m["wl"].group_by("clinics.region").aggregate("wl.patients").execute()
#    wl.patients
# 0            3        <- one row, no region column
```

Removing the `joins:` block makes (2) work normally. Same behaviour via
`from_yaml`. Tried `type: one` / `type: many` and with/without `is_entity`
on either side — all variants fail. Expected for (1):
`[["North", 2], ["South", 1]]`.

(3) seems like a second bug: prefixed measures bypass the error but produce a
grand total, discarding the grouping — silent wrong results.

## Environment

- boring-semantic-layer 0.3.15 (pip)
- ibis-framework[duckdb] 12.0.0
- duckdb 1.5.4
- macOS / Python 3.13

---

*Also worth mentioning in a separate issue (observed on the same setup):*
1. *Unknown measure names in `aggregate()` produce an IbisTypeError listing
   the raw table's columns rather than the model's declared
   dimensions/measures — misleading for semantic-layer users.*
2. *A dimension/measure whose name collides case-insensitively with any
   table column (e.g. dim `specialty` over column `Specialty`) fails at
   execution with `schema names don't match input data columns` /
   `KeyError` from pyarrow.*
