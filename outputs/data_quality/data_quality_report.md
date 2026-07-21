# Data quality report

After Phase 1 ingest.

## `timetable_stops` — 4,870 rows

## `timetable_services` — 516 rows

## `timetable_vehicle_journeys` — 15,775 rows

## `timetable_schedule_events` — 769,705 rows

## `avl_vehicle_activities` — 279,198 rows
- null `origin_aimed_departure_time`: 13.67%
- null `destination_aimed_arrival_time`: 13.67%

## `fares_files` — 861 rows
- null `fare_amount_min`: 0.23%
- null `fare_amount_max`: 0.23%
- null `fare_amount_median`: 0.23%

## `fares_line_proxy` — 63 rows

## `disruptions` — 2,025 rows
- null `operator_noc`: 1.23%
- null `services_affected`: 1.23%
- null `stops_affected`: 1.23%
- null `stops_affected_names`: 1.23%
- null `expected_delay_minutes`: 1.23%

**Scale PASS:** schedule_events = 769,705 rows (>= 100k)
**Combined rows:** 1,073,013