"""
Build dashboard map artefacts (line centroids + route polylines).

Reads processed parquet + line_risk_summary.csv and writes:
  outputs/dashboard_exports/line_map_geo.csv
  outputs/dashboard_exports/line_map_routes.json
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

HERE = Path(__file__).resolve().parent
PROCESSED = HERE / "outputs" / "processed"
EXPORTS = HERE / "outputs" / "dashboard_exports"


def _norm_line(value: object) -> str:
    return str(value).strip().upper()


def _valid_coord(lat: float, lon: float) -> bool:
    # Liverpool City Region bounding box ( generous )
    return 53.15 <= lat <= 53.75 and -3.35 <= lon <= -2.45


def build_line_geo(
    journeys: pd.DataFrame,
    lines: pd.DataFrame,
) -> pd.DataFrame:
    """Mean AVL location per line merged with dashboard line metrics."""
    j = journeys.copy()
    j["line_key"] = j["line_key"].map(_norm_line)
    j["latitude"] = pd.to_numeric(j["latitude"], errors="coerce")
    j["longitude"] = pd.to_numeric(j["longitude"], errors="coerce")
    j = j.dropna(subset=["latitude", "longitude"])
    j = j[j.apply(lambda r: _valid_coord(r["latitude"], r["longitude"]), axis=1)]

    centroids = (
        j.groupby("line_key", as_index=False)
        .agg(
            centroid_lat=("latitude", "mean"),
            centroid_lon=("longitude", "mean"),
            n_gps_points=("latitude", "count"),
        )
    )

    out = lines.copy()
    out["line_key"] = out["line_key"].map(_norm_line)
    out = out.merge(centroids, on="line_key", how="left")

    missing = out["centroid_lat"].isna()
    if missing.any():
        # Fallback: median trip point from any journey column available
        for lk in out.loc[missing, "line_key"]:
            sub = j[j["line_key"] == lk]
            if not sub.empty:
                out.loc[out["line_key"] == lk, "centroid_lat"] = sub["latitude"].median()
                out.loc[out["line_key"] == lk, "centroid_lon"] = sub["longitude"].median()

    return out


def _timetable_route(
    line_key: str,
    events: pd.DataFrame,
    stops: pd.DataFrame,
) -> list[list[float]] | None:
    sub = events[events["line_key"] == line_key]
    if sub.empty:
        return None

    jc = sub.groupby("journey_code").size().idxmax()
    seq = sub[sub["journey_code"] == jc].sort_values("stop_sequence")
    stop_lookup = (
        stops.dropna(subset=["latitude", "longitude"])
        .assign(
            latitude=lambda d: pd.to_numeric(d["latitude"], errors="coerce"),
            longitude=lambda d: pd.to_numeric(d["longitude"], errors="coerce"),
        )
        .dropna(subset=["latitude", "longitude"])
        .drop_duplicates(subset=["stop_id"])
        .set_index("stop_id")
    )

    path: list[list[float]] = []
    for sid in seq["stop_id"]:
        if sid not in stop_lookup.index:
            continue
        row = stop_lookup.loc[sid]
        lat, lon = float(row["latitude"]), float(row["longitude"])
        if _valid_coord(lat, lon):
            path.append([lat, lon])
    return path if len(path) >= 2 else None


def _avl_route(line_key: str, journeys: pd.DataFrame) -> list[list[float]] | None:
    sub = journeys[journeys["line_key"] == line_key].copy()
    sub["latitude"] = pd.to_numeric(sub["latitude"], errors="coerce")
    sub["longitude"] = pd.to_numeric(sub["longitude"], errors="coerce")
    sub = sub.dropna(subset=["latitude", "longitude"])
    sub = sub[sub.apply(lambda r: _valid_coord(r["latitude"], r["longitude"]), axis=1)]
    if len(sub) < 2:
        return None

    sub = sub.sort_values(["hour_of_day", "latitude", "longitude"])
    coords: list[list[float]] = []
    last: tuple[float, float] | None = None
    for _, row in sub.iterrows():
        lat, lon = float(row["latitude"]), float(row["longitude"])
        if last and abs(lat - last[0]) < 0.0005 and abs(lon - last[1]) < 0.0005:
            continue
        coords.append([lat, lon])
        last = (lat, lon)
    return coords if len(coords) >= 2 else None


def build_line_routes(
    journeys: pd.DataFrame,
    line_keys: list[str],
    events: pd.DataFrame | None = None,
    stops: pd.DataFrame | None = None,
) -> dict[str, dict[str, Any]]:
    j = journeys.copy()
    j["line_key"] = j["line_key"].map(_norm_line)

    routes: dict[str, dict[str, Any]] = {}
    ev = None
    if events is not None and stops is not None:
        ev = events.copy()
        ev["line_key"] = ev["line_name"].map(_norm_line)

    for lk in line_keys:
        lk = _norm_line(lk)
        path: list[list[float]] | None = None
        source = "avl_trip_points"
        if ev is not None:
            path = _timetable_route(lk, ev, stops)
            if path:
                source = "amsy_timetable"
        if not path:
            path = _avl_route(lk, j)
        if path:
            routes[lk] = {"coordinates": path, "source": source}
    return routes


def build_map_exports(
    processed_dir: Path = PROCESSED,
    exports_dir: Path = EXPORTS,
) -> tuple[Path, Path]:
    exports_dir.mkdir(parents=True, exist_ok=True)
    lines = pd.read_csv(exports_dir / "line_risk_summary.csv")
    journeys = pd.read_parquet(processed_dir / "analytical_journeys.parquet")

    geo = build_line_geo(journeys, lines)
    geo_path = exports_dir / "line_map_geo.csv"
    geo.to_csv(geo_path, index=False)

    events = stops = None
    ev_path = processed_dir / "timetable_schedule_events.parquet"
    st_path = processed_dir / "timetable_stops.parquet"
    if ev_path.exists() and st_path.exists():
        events = pd.read_parquet(ev_path)
        stops = pd.read_parquet(st_path)

    routes = build_line_routes(
        journeys,
        line_keys=geo["line_key"].astype(str).tolist(),
        events=events,
        stops=stops,
    )
    routes_path = exports_dir / "line_map_routes.json"
    routes_path.write_text(json.dumps(routes, indent=2), encoding="utf-8")

    return geo_path, routes_path


if __name__ == "__main__":
    g, r = build_map_exports()
    print("Wrote", g)
    print("Wrote", r)
