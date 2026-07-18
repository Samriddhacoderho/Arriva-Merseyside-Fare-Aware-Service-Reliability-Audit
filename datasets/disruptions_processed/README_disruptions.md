# Disruptions dataset (Merseyside / Arriva)

## Files
- `merseytravel_seed_disruptions.csv` — real Merseytravel events from BODS catalogue (25 rows)
- `synthesised_amsy_anwe_disruptions.csv` — synthetic events aligned to AMSY/ANWE (2000 rows)
- `disruptions_combined.csv` — seed + synthetic for modelling (2025 rows)
- `synthesis_metadata.json` — generation parameters and distributions

## Why synthesis?
Merseytravel publishes relatively few disruption records, and the BODS catalogue often omits operator/service names.
For predictive modelling (delay / non-compliance features), additional geographically and line-aligned events were synthesised.

## Method
1. Extract Merseytravel rows from `datasets/disruptions/disruptions_data_catalogue.csv`.
2. Collect AMSY line codes and stop points from TransXChange timetables.
3. Collect ANWE line codes observed in AVL feed 709 snapshots.
4. Sample reason types using weights informed by the Merseytravel seed and BODS reason vocabulary.
5. Attach real line codes and stop IDs; set `is_synthetic=true` on generated rows.

## Reproducibility
```bash
python3 scripts/synthesise_disruptions.py --n-synthetic 2000 --seed 42
```

RNG seed: `42`
