# Overnight AVL collection (feed 709)

```bash
cd "/Users/suhritsatyal/Desktop/softwarica_bachelors/SEM4/BIG_DATA/coursework"

export BODS_API_KEY='paste_your_key_here'

# Foreground (leave Terminal open):
python3 scripts/collect_avl_709.py --interval 30 --target 100000 --hours 16

# Or background overnight (recommended):
nohup python3 scripts/collect_avl_709.py --interval 30 --target 100000 --hours 16 \
  > datasets/location/collect_avl_709.log 2>&1 &

# Watch progress:
tail -f datasets/location/collect_avl_709.log
```

Stops automatically at ~100k VehicleActivity rows **or** 16 hours (whichever first).  
Files go to `datasets/location/snapshots/avl_709_*.xml`.

Next day: re-run the backup notebook Phase 0 → 7 to rebuild labels/ML.
