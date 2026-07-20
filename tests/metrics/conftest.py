"""Fixtures for the metrics layer: the design-doc YAML and a loader helper."""

import pytest

from wh.metrics.loader import load_definitions

DIMS_YAML = """\
dimensions:
  facility:
    table: main.clinic_dim
    key_column: clinic_code
    attributes:
      clinic: clinic_name
      hospital: hospital_name
      district: district
      region: region
    hierarchy: [clinic, hospital, district, region]
  doctor:
    table: main.doctor_dim
    key_column: doctor_id
    attributes:
      specialty: specialty
      seniority: seniority_band
"""

WAITLIST_YAML = """\
waitlist:
  fact: main.waitlist
  description: "Outpatient waitlist snapshots"
  time:
    column: snapshot_date
    cadence: weekly
  snapshot: true
  dimensions:
    facility: clinic_code
    doctor: doctor_id
    urgency: urgency_category
  measures:
    patients_waiting:
      expr: count(DISTINCT ur)
      description: "Distinct patients on the list at snapshot"
    long_waiters:
      expr: count(*)
      where: wait_days > 365
      description: "Patients waiting beyond 365 days at snapshot"
    pct_over_target:
      ratio:
        num: count(*) FILTER (WHERE wait_days > target_days)
        den: count(*)
      description: "% waiting beyond clinically recommended time"
    median_wait:
      expr: median(wait_days)
      time_agg: none
      description: "Median days waiting at snapshot"
"""

REMOVALS_YAML = """\
removals:
  fact: main.waitlist_removals
  description: "Waitlist removal events"
  time:
    column: removal_date
  dimensions:
    facility: clinic_code
    doctor: doctor_id
  measures:
    removals:
      expr: count(*)
      where: removal_reason <> 'ADMIN'
      description: "Clinically meaningful removals"
"""


@pytest.fixture
def make_defs(tmp_path):
    """Write YAML documents as separate files and load them."""

    def _make(*yamls, fiscal_year_start=7):
        d = tmp_path / "semantics"
        d.mkdir(exist_ok=True)
        for existing in d.glob("*.yml"):
            existing.unlink()
        for i, text in enumerate(yamls):
            (d / f"f{i}.yml").write_text(text)
        return load_definitions(d, fiscal_year_start=fiscal_year_start)

    return _make


@pytest.fixture
def defs(make_defs):
    """The design-doc example: shared dims + waitlist (snapshot) + removals."""
    return make_defs(DIMS_YAML, WAITLIST_YAML, REMOVALS_YAML)


@pytest.fixture
def design_yaml():
    return {"dims": DIMS_YAML, "waitlist": WAITLIST_YAML, "removals": REMOVALS_YAML}
