"""Shared metric-layer test data: the design-doc YAML and seed SQL."""

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
    facility: {shared: clinic_code}
    doctor: {shared: doctor_id}
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
    facility: {shared: clinic_code}
    doctor: {shared: doctor_id}
  measures:
    removals:
      expr: count(*)
      where: removal_reason <> 'ADMIN'
      description: "Clinically meaningful removals"
"""


SEED_SQL = """
        CREATE TABLE main.clinic_dim AS FROM (VALUES
            ('C1','Harbour Clinic','H1','Coastal','North'),
            ('C2','Valley Clinic','H2','Inland','North'),
            ('C3','Seaside Clinic','H3','Seaside','South'),
            ('C4','Range Clinic','H4','Range','South')
        ) t(clinic_code, clinic_name, hospital_name, district, region);
        CREATE TABLE main.doctor_dim AS FROM (VALUES
            ('D1','ENT','senior'), ('D2','ENT','junior'), ('D3','Ophthal','senior')
        ) t(doctor_id, specialty, seniority_band);
        CREATE TABLE main.waitlist AS FROM (VALUES
            (DATE '2026-06-05','C1','D1','Cat 1','U1',100,90),
            (DATE '2026-06-05','C1','D2','Cat 2','U2', 50,60),
            (DATE '2026-06-05','C2','D3','Cat 1','U3',400,90),
            (DATE '2026-06-05','C3','D1','Cat 2','U4', 20,60),
            (DATE '2026-06-12','C1','D1','Cat 1','U1',107,90),
            (DATE '2026-06-12','C1','D2','Cat 2','U2', 57,60),
            (DATE '2026-06-12','C2','D3','Cat 1','U3',407,90),
            (DATE '2026-06-12','C3','D1','Cat 2','U4', 27,60),
            (DATE '2026-06-19','C1','D1','Cat 1','U1',114,90),
            (DATE '2026-06-19','C1','D2','Cat 2','U2', 64,60),
            (DATE '2026-06-19','C2','D3','Cat 1','U3',414,90),
            (DATE '2026-06-19','C3','D1','Cat 2','U4', 34,60),
            (DATE '2026-06-19','C4','D3','Cat 3','U5',  5,30),
            (DATE '2026-06-26','C1','D1','Cat 1','U1',121,90),
            (DATE '2026-06-26','C1','D2','Cat 2','U2', 71,60),
            (DATE '2026-06-26','C2','D3','Cat 1','U3',421,90),
            (DATE '2026-06-26','C4','D3','Cat 3','U5', 12,30),
            (DATE '2026-07-03','C1','D1','Cat 1','U1',128,90),
            (DATE '2026-07-03','C1','D2','Cat 2','U2', 78,60),
            (DATE '2026-07-03','C4','D3','Cat 3','U5', 19,30),
            (DATE '2026-07-03','C3','D2','Cat 1','U6', 10,90),
            (DATE '2026-07-10','C1','D1','Cat 1','U1',135,90),
            (DATE '2026-07-10','C1','D2','Cat 2','U2', 85,60),
            (DATE '2026-07-10','C4','D3','Cat 3','U5', 26,30),
            (DATE '2026-07-10','C3','D2','Cat 1','U6', 17,90)
        ) t(snapshot_date, clinic_code, doctor_id, urgency_category, ur, wait_days, target_days);
        CREATE TABLE main.waitlist_removals AS FROM (VALUES
            (DATE '2026-06-10','C1','D1','TREATED'),
            (DATE '2026-06-15','C2','D3','ADMIN'),
            (DATE '2026-06-20','C1','D2','TREATED'),
            (DATE '2026-07-01','C3','D1','TREATED'),
            (DATE '2026-07-05','C4','D3','ADMIN'),
            (DATE '2026-07-08','C1','D1','TRANSFER')
        ) t(removal_date, clinic_code, doctor_id, removal_reason);
"""
