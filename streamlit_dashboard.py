"""
Streamlit dashboard for the Merseyside reliability audit pipeline.

Run from the coursework project root:
  streamlit run streamlit_dashboard.py
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

try:
    import folium
    from branca.colormap import LinearColormap
    from streamlit_folium import st_folium

    _MAP_LIBS_OK = True
except ImportError:
    _MAP_LIBS_OK = False

st.set_page_config(page_title="Merseytravel Reliability Audit", layout="wide")

HERE = Path(__file__).resolve().parent
OUT = HERE / "outputs"
FEATURES_META = OUT / "features" / "feature_metadata.json"
DASHBOARD_EXPORTS = OUT / "dashboard_exports"
VIS_A = OUT / "visuals" / "dashboard_charts"
VIS_B = HERE / "visuals" / "dashboard_charts"

# Display names for CSV columns that still use internal/notebook naming
COLUMN_LABELS = {
    "hypothetical_exposure": "fare_exposure",
    "total_hypothetical_exposure": "total_fare_exposure",
    "mean_hypothetical_exposure": "mean_fare_exposure",
    "prob_noncompliant": "P(non-compliant)",
    "mean_prob_noncompliant": "mean P(non-compliant)",
    "service_reliability": "service_reliability",
    "observed_compliance_rate": "observed_compliance_rate",
}

DROP_COLS = {"exposure_disclaimer"}

DOW_LABELS = [
    (1, "Sunday"),
    (2, "Monday"),
    (3, "Tuesday"),
    (4, "Wednesday"),
    (5, "Thursday"),
    (6, "Friday"),
    (7, "Saturday"),
]

LIVERPOOL_CENTER = (53.4084, -2.9916)
MAP_ZOOM = 11

COLOR_MODES: dict[str, dict[str, Any]] = {
    "Trip volume (busiest lines)": {
        "column": "n_trips",
        "higher_is_better": False,
        "cmap": ["#deebf7", "#08306b"],
        "caption": "Darker blue = more observed trips on the line (crowding proxy).",
    },
    "Service reliability": {
        "column": "service_reliability",
        "higher_is_better": True,
        "cmap": ["#b33a3a", "#f7f7f7", "#2f6b4f"],
        "caption": "Green = higher observed on-time performance; red = below target.",
    },
    "Mean P(non-compliant)": {
        "column": "mean_prob_noncompliant",
        "higher_is_better": False,
        "cmap": ["#ffffcc", "#800026"],
        "caption": "Darker red = higher model-estimated lateness risk.",
    },
    "Total fare exposure": {
        "column": "total_hypothetical_exposure",
        "higher_is_better": False,
        "cmap": ["#fee6ce", "#7f2704"],
        "caption": "Darker = higher fare-weighted exposure (scenario metric, not a real fine).",
    },
    "Below 85% reliability": {
        "column": "below_85_threshold",
        "higher_is_better": False,
        "binary": True,
        "caption": "Red = line below the 85% service reliability threshold.",
    },
}


def _sk(field: str) -> str:
    return f"sc_{field}"


def present(df: pd.DataFrame) -> pd.DataFrame:
    """Drop internal disclaimer columns and rename labels for a live-ops look."""
    out = df.drop(columns=[c for c in DROP_COLS if c in df.columns], errors="ignore")
    return out.rename(columns={k: v for k, v in COLUMN_LABELS.items() if k in out.columns})


@st.cache_data(show_spinner=False)
def load_trip_templates() -> pd.DataFrame:
    return pd.read_csv(DASHBOARD_EXPORTS / "trip_risk.csv")


@st.cache_data(show_spinner=False)
def load_field_metadata() -> tuple[list[str], list[str], dict]:
    meta = json.loads(FEATURES_META.read_text(encoding="utf-8"))
    return meta["cat_cols"], meta["num_cols"], meta


@st.cache_data(show_spinner="Preparing network map…")
def load_map_data() -> tuple[pd.DataFrame, dict[str, Any]]:
    geo_path = DASHBOARD_EXPORTS / "line_map_geo.csv"
    routes_path = DASHBOARD_EXPORTS / "line_map_routes.json"

    if not geo_path.exists() or not routes_path.exists():
        from map_exports import build_map_exports

        build_map_exports()

    geo = pd.read_csv(geo_path)
    routes = json.loads(routes_path.read_text(encoding="utf-8"))
    return geo, routes


def _metric_value(row: pd.Series, column: str) -> float:
    val = row.get(column)
    if pd.isna(val):
        return 0.0
    return float(val)


def _color_for_value(value: float, vmin: float, vmax: float, cfg: dict[str, Any]) -> str:
    if cfg.get("binary"):
        return "#b33a3a" if value >= 0.5 else "#2f6b4f"
    if vmax <= vmin:
        return cfg["cmap"][-1]
    cmap = LinearColormap(colors=cfg["cmap"], vmin=vmin, vmax=vmax)
    return cmap(value)


def _popup_html(row: pd.Series) -> str:
    return (
        f"<b>Line {row['line_key']}</b><br>"
        f"Trips observed: {int(row.get('n_trips', 0))}<br>"
        f"Service reliability: {float(row.get('service_reliability', 0)):.1%}<br>"
        f"Mean P(late): {float(row.get('mean_prob_noncompliant', 0)):.1%}<br>"
        f"Total fare exposure: {float(row.get('total_hypothetical_exposure', 0)):.2f}<br>"
        f"Status: {row.get('status', 'n/a')}"
    )


def build_network_map(
    geo: pd.DataFrame,
    routes: dict[str, Any],
    color_mode: str,
    show_routes: bool,
    only_below_85: bool,
) -> folium.Map:
    cfg = COLOR_MODES[color_mode]
    column = cfg["column"]
    plot_df = geo.dropna(subset=["centroid_lat", "centroid_lon"]).copy()
    if only_below_85 and "below_85_threshold" in plot_df.columns:
        plot_df = plot_df[plot_df["below_85_threshold"] == 1]

    fmap = folium.Map(location=LIVERPOOL_CENTER, zoom_start=MAP_ZOOM, tiles="OpenStreetMap")

    if plot_df.empty:
        return fmap

    values = plot_df[column].astype(float)
    vmin, vmax = float(values.min()), float(values.max())
    if cfg.get("binary"):
        legend = (
            "<div style='font-size:13px;line-height:1.5'>"
            "<span style='color:#b33a3a'>■</span> Below 85% reliability &nbsp; "
            "<span style='color:#2f6b4f'>■</span> Meets / above 85%"
            "</div>"
        )
    else:
        cmap = LinearColormap(colors=cfg["cmap"], vmin=vmin, vmax=vmax)
        cmap.caption = color_mode
        fmap.add_child(cmap)

    n_max = max(float(plot_df["n_trips"].max()), 1.0)

    for _, row in plot_df.iterrows():
        lk = str(row["line_key"])
        value = _metric_value(row, column)
        color = _color_for_value(value, vmin, vmax, cfg)
        radius = 6 + 18 * (float(row.get("n_trips", 1)) / n_max)

        if show_routes and lk in routes:
            coords = routes[lk].get("coordinates") or []
            if len(coords) >= 2:
                folium.PolyLine(
                    locations=coords,
                    color=color,
                    weight=5,
                    opacity=0.85,
                    popup=folium.Popup(_popup_html(row), max_width=280),
                ).add_to(fmap)

        folium.CircleMarker(
            location=(float(row["centroid_lat"]), float(row["centroid_lon"])),
            radius=radius,
            color="#222222",
            weight=1,
            fill=True,
            fill_color=color,
            fill_opacity=0.88,
            popup=folium.Popup(_popup_html(row), max_width=280),
            tooltip=f"Line {lk}",
        ).add_to(fmap)

        folium.Marker(
            location=(float(row["centroid_lat"]), float(row["centroid_lon"])),
            icon=folium.DivIcon(
                html=(
                    f"<div style='font-size:11px;font-weight:700;color:#111;"
                    f"text-shadow:1px 1px 2px #fff;'>{lk}</div>"
                )
            ),
        ).add_to(fmap)

    if cfg.get("binary"):
        fmap.get_root().html.add_child(folium.Element(f"<div style='padding:8px'>{legend}</div>"))

    return fmap


def render_network_map_tab(lines: pd.DataFrame) -> None:
    st.subheader("Network map")
    st.caption(
        "Leaflet map of Merseyside bus lines coloured by reliability, risk, trip volume, "
        "or fare exposure. Marker size reflects how many trips were observed."
    )

    if not _MAP_LIBS_OK:
        st.error("Install map dependencies: pip install folium streamlit-folium")
        return

    try:
        geo, routes = load_map_data()
    except Exception as exc:
        st.error(f"Could not build map data: {exc}")
        st.info("Run the full analysis notebook (Phase 7) or execute: python map_exports.py")
        return

    c1, c2, c3 = st.columns([1.4, 1.0, 1.0])
    with c1:
        color_mode = st.selectbox("Colour lines by", list(COLOR_MODES.keys()))
    with c2:
        show_routes = st.checkbox("Show route paths", value=True)
    with c3:
        only_below = st.checkbox("Only lines below 85%", value=False)

    st.caption(COLOR_MODES[color_mode]["caption"])

    fmap = build_network_map(geo, routes, color_mode, show_routes, only_below)
    st_folium(fmap, width=None, height=520, returned_objects=[])

    with st.expander("Map data table"):
        st.dataframe(
            geo.sort_values("n_trips", ascending=False),
            width="stretch",
        )

    route_sources = pd.Series(
        {k: v.get("source", "?") for k, v in routes.items()}
    ).value_counts()
    st.caption(
        "Route geometry: "
        + ", ".join(f"{cnt} lines from {src}" for src, cnt in route_sources.items())
        + ". AVL paths use observed trip GPS; timetable paths use AMSY stop sequences where matched."
    )


@st.cache_resource(show_spinner="Loading reliability model…")
def cached_scoring_bundle():
    from trip_scorer import get_scoring_bundle

    return get_scoring_bundle()


def _form_key(field: str) -> str:
    return f"form_sc_{field}"


def _init_scenario_state(trips: pd.DataFrame) -> None:
    # Bump this version whenever calculator widget structure changes so an
    # existing Streamlit session cannot retain stale/orphaned widget state.
    state_version = 3
    if st.session_state.get("_scenario_state_version") == state_version:
        return
    from trip_scorer import scenario_defaults

    defaults = scenario_defaults(trips)
    for field, value in defaults.items():
        st.session_state[_sk(field)] = value
        st.session_state[_form_key(field)] = value
    dow_labels = [label for _, label in DOW_LABELS]
    dow_values = [val for val, _ in DOW_LABELS]
    dow_val = int(float(defaults.get("day_of_week", 3)))
    st.session_state["_sc_dow_picker"] = (
        dow_labels[dow_values.index(dow_val)] if dow_val in dow_values else "Tuesday"
    )
    st.session_state["_form_sc_day_of_week"] = st.session_state["_sc_dow_picker"]
    st.session_state["_sc_peak_picker"] = (
        "Peak" if float(defaults.get("is_peak", 0)) >= 0.5 else "Off-peak"
    )
    st.session_state["_form_sc_is_peak"] = st.session_state["_sc_peak_picker"]
    st.session_state["_scenario_initialized"] = True
    st.session_state["_scenario_state_version"] = state_version
    st.session_state.pop("sc_result", None)
    st.session_state.pop("sc_result_for", None)
    st.session_state.pop("sc_result_inputs", None)


def _scenario_val(field: str, fallback: Any = "") -> Any:
    return st.session_state.get(_sk(field), fallback)


def _read_form_values_from_state(cat_cols: list[str], num_cols: list[str]) -> dict[str, Any]:
    """Read submitted form widget values from session state (after form submit)."""
    values: dict[str, Any] = {}
    for field in cat_cols + num_cols:
        key = _form_key(field)
        if key in st.session_state:
            values[field] = st.session_state[key]
    if "_form_sc_day_of_week" in st.session_state:
        values["day_of_week"] = st.session_state["_form_sc_day_of_week"]
    if "_form_sc_is_peak" in st.session_state:
        values["is_peak"] = st.session_state["_form_sc_is_peak"]
    return values


def _assemble_scenario_inputs(
    cat_cols: list[str],
    num_cols: list[str],
    form_values: dict[str, Any],
) -> dict:
    """Normalise raw form widget values into a scorer input dict."""
    num_set = set(num_cols)
    picker_fields = {"day_of_week", "is_peak"}
    dow_labels = [label for _, label in DOW_LABELS]
    dow_values = [val for val, _ in DOW_LABELS]
    inputs: dict = {}

    for field in cat_cols + num_cols:
        if field in picker_fields:
            continue
        if field not in form_values:
            continue
        val = form_values[field]
        if field in num_set:
            inputs[field] = float(val)
        else:
            inputs[field] = str(val)

    dow_pick = form_values.get("day_of_week")
    if dow_pick in dow_labels:
        inputs["day_of_week"] = float(dow_values[dow_labels.index(dow_pick)])

    peak_pick = form_values.get("is_peak", "Off-peak")
    inputs["is_peak"] = 1.0 if peak_pick == "Peak" else 0.0

    return inputs


def _collect_scenario_inputs(cat_cols: list[str], num_cols: list[str]) -> dict:
    """Fallback reader from session state (used after persisting form values)."""
    num_set = set(num_cols)
    picker_fields = {"day_of_week", "is_peak"}
    inputs: dict = {}

    for field in cat_cols + num_cols:
        if field in picker_fields:
            continue
        key = _sk(field)
        if key not in st.session_state:
            continue
        val = st.session_state[key]
        if field in num_set:
            inputs[field] = float(val)
        else:
            inputs[field] = str(val)

    dow_labels = [label for _, label in DOW_LABELS]
    dow_values = [val for val, _ in DOW_LABELS]
    pick = st.session_state.get("_sc_dow_picker")
    if pick in dow_labels:
        inputs["day_of_week"] = float(dow_values[dow_labels.index(pick)])

    peak = st.session_state.get("_sc_peak_picker", "Off-peak")
    inputs["is_peak"] = 1.0 if peak == "Peak" else 0.0

    return inputs


def _inputs_fingerprint(inputs: dict) -> str:
    return json.dumps({k: inputs[k] for k in sorted(inputs)}, sort_keys=True, default=str)


def _scenario_input_summary(inputs: dict) -> str:
    dow_labels = {val: label for val, label in DOW_LABELS}
    hour = int(inputs.get("hour_of_day", 0))
    day = dow_labels.get(int(inputs.get("day_of_week", 3)), "?")
    peak = "Peak" if float(inputs.get("is_peak", 0)) >= 0.5 else "Off-peak"
    return (
        f"Line {inputs.get('line_key', '?')} · {day} · {hour:02d}h · {peak} · "
        f"fare £{float(inputs.get('fare_proxy', 0)):.2f}"
    )


def render_scenario_scorer(trips_raw: pd.DataFrame) -> None:
    st.subheader("Trip risk calculator")
    st.caption(
        "Enter service details to estimate the chance of late running and "
        "associated fare exposure for that journey."
    )

    cat_cols, num_cols, _ = load_field_metadata()
    gbt_path = HERE / "outputs" / "machine_learning" / "best_model" / "GBT"
    if not gbt_path.exists():
        st.warning("Risk model not available — run the full analysis notebook first.")
        return

    from trip_scorer import line_history_defaults, load_field_options, score_scenario

    _init_scenario_state(trips_raw)
    line_opts = sorted(trips_raw["line_key"].astype(str).unique().tolist())
    field_opts = load_field_options()

    col_l, col_r = st.columns([1.1, 0.9])
    form_values: dict[str, Any] = {}

    with col_l:
        st.markdown("**Service**")
        if st.button("Fill from typical values for selected line", width="stretch"):
            line = str(st.session_state.get(_form_key("line_key"), line_opts[0]))
            for k, v in line_history_defaults(trips_raw, line).items():
                st.session_state[_sk(k)] = v
                st.session_state[_form_key(k)] = v
            st.session_state.pop("sc_result", None)
            st.session_state.pop("sc_result_for", None)
            st.rerun()

        c1, c2 = st.columns(2)
        with c1:
            form_values["line_key"] = st.selectbox(
                "Bus line",
                line_opts,
                key=_form_key("line_key"),
            )
        with c2:
            form_values["hour_of_day"] = st.select_slider(
                "Departure hour (24h)",
                options=list(range(24)),
                key=_form_key("hour_of_day"),
            )

        c3, c4 = st.columns(2)
        dow_labels = [label for _, label in DOW_LABELS]
        with c3:
            form_values["day_of_week"] = st.selectbox(
                "Day of week",
                dow_labels,
                key="_form_sc_day_of_week",
            )
        with c4:
            form_values["is_peak"] = st.radio(
                "Time band",
                ["Off-peak", "Peak"],
                horizontal=True,
                key="_form_sc_is_peak",
            )

        form_values["fare_proxy"] = st.slider(
            "Typical fare (£ proxy)",
            min_value=0.0,
            max_value=10.0,
            step=0.05,
            key=_form_key("fare_proxy"),
        )

        st.markdown("**Recent performance on this line**")
        h1, h2 = st.columns(2)
        with h1:
            form_values["hist_line_noncompliance_rate"] = st.slider(
                "Share of recent trips late (0–1)",
                min_value=0.0,
                max_value=1.0,
                step=0.01,
                key=_form_key("hist_line_noncompliance_rate"),
            )
            form_values["hist_line_delay_mean"] = st.number_input(
                "Average delay on line (minutes)",
                step=0.5,
                format="%.1f",
                key=_form_key("hist_line_delay_mean"),
            )
        with h2:
            form_values["hist_line_delay_std"] = st.number_input(
                "Delay variability (std dev, minutes)",
                min_value=0.0,
                step=0.5,
                format="%.1f",
                key=_form_key("hist_line_delay_std"),
            )
            form_values["hist_line_n_prior"] = st.number_input(
                "Journeys used for line average",
                min_value=0.0,
                step=1.0,
                format="%.0f",
                key=_form_key("hist_line_n_prior"),
            )

        with st.expander("Additional service context"):
            for field in cat_cols:
                if field == "line_key":
                    continue
                opts = field_opts.get(field, [str(_scenario_val(field, ""))])
                opts = [str(o) for o in opts]
                cur = str(st.session_state.get(_form_key(field), opts[0] if opts else ""))
                if cur not in opts and cur:
                    opts = [cur] + opts
                form_values[field] = st.selectbox(
                    field.replace("_", " ").title(),
                    opts,
                    key=_form_key(field),
                )

            extra_nums = [
                ("n_obs", "GPS updates for journey"),
                ("scheduled_trip_minutes", "Scheduled trip length (min)"),
                ("latitude", "Departure latitude"),
                ("longitude", "Departure longitude"),
                ("has_disruption", "Active disruption (0=no, 1=yes)"),
                ("disruption_severity_score", "Disruption severity"),
            ]
            for field, label in extra_nums:
                if field in num_cols:
                    form_values[field] = st.number_input(
                        label,
                        key=_form_key(field),
                    )

        st.caption(
            "Results update automatically when an input is changed."
        )

    with col_r:
        st.markdown("**Results**")

        inputs = _assemble_scenario_inputs(cat_cols, num_cols, form_values)
        fingerprint = _inputs_fingerprint(inputs)
        if st.session_state.get("sc_result_for") != fingerprint:
            try:
                cached_scoring_bundle()
                result = score_scenario(inputs)
                st.session_state["sc_result"] = result
                st.session_state["sc_result_for"] = fingerprint
                st.session_state["sc_result_inputs"] = inputs
            except Exception as exc:
                st.error(f"Could not calculate: {exc}")

        if "sc_result" in st.session_state:
            r = st.session_state["sc_result"]
            saved_inputs = st.session_state.get("sc_result_inputs", {})
            if saved_inputs:
                st.caption(_scenario_input_summary(saved_inputs))
            st.metric("Chance of late running", f"{r['prob_noncompliant']:.1%}")
            st.metric("Chance on time", f"{r['probability_compliant']:.1%}")
            st.metric("Fare exposure", f"£{r['fare_exposure']:.2f}")
            st.metric("Risk level", r["risk_band"])

            outcome = "On time" if r["prediction"] == 1 else "Late running likely"
            st.markdown(
                f"**Likely outcome:** {outcome}  \n"
                f"Exposure = late-running probability × fare (£{r['fare_proxy']:.2f})"
            )
            st.progress(min(max(r["prob_noncompliant"], 0.0), 1.0))
        else:
            st.markdown("_Enter trip details and click **Calculate** to see results._")


st.title("Fare-Aware Service Reliability Audit")
st.caption("Merseytravel / Liverpool City Region · Arriva Merseyside service performance")

trip_path = DASHBOARD_EXPORTS / "trip_risk.csv"
line_path = DASHBOARD_EXPORTS / "line_risk_summary.csv"
hour_path = DASHBOARD_EXPORTS / "hour_risk_summary.csv"
below_path = DASHBOARD_EXPORTS / "lines_below_85.csv"

if not trip_path.exists():
    st.error(
        "Dashboard exports not found under outputs/dashboard_exports/.\n\n"
        "Open and run: ALL_PHASES_Merseyside_Reliability_Audit.ipynb "
        "(run the full notebook top to bottom), then refresh this page."
    )
    st.stop()

trips_raw = load_trip_templates()
trips = present(trips_raw.copy())
lines = present(pd.read_csv(line_path))
hours = present(pd.read_csv(hour_path))
below = present(pd.read_csv(below_path)) if below_path.exists() else pd.DataFrame()

exposure_col = "fare_exposure" if "fare_exposure" in trips.columns else "hypothetical_exposure"
prob_col = "P(non-compliant)" if "P(non-compliant)" in trips.columns else "prob_noncompliant"
line_exposure_col = (
    "total_fare_exposure"
    if "total_fare_exposure" in lines.columns
    else "total_hypothetical_exposure"
)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Trips scored", len(trips))
c2.metric("Lines below 85%", len(below))
c3.metric("Mean P(non-compliant)", f"{trips[prob_col].mean():.3f}")
c4.metric("Total fare exposure", f"{trips[exposure_col].sum():.2f}")

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(
    ["Network map", "By line", "By hour", "Trip risk", "Charts", "Risk calculator"]
)

with tab1:
    render_network_map_tab(lines)

with tab2:
    st.subheader("Line risk & reliability")
    st.dataframe(
        lines.sort_values(line_exposure_col, ascending=False),
        width="stretch",
    )
    if not below.empty:
        st.subheader("Lines below 85% observed reliability")
        st.dataframe(below, width="stretch")

with tab3:
    st.subheader("Predicted non-compliance by hour")
    hour_prob = (
        "mean P(non-compliant)"
        if "mean P(non-compliant)" in hours.columns
        else "mean_prob_noncompliant"
    )
    st.line_chart(hours.set_index("hour_of_day")[hour_prob])
    st.dataframe(hours, width="stretch")

with tab4:
    min_p = st.slider("Min P(non-compliant)", 0.0, 1.0, 0.5, 0.05)
    line_opts = ["(all)"] + sorted(trips["line_key"].astype(str).unique().tolist())
    line_sel = st.selectbox("Line filter", line_opts)
    view = trips[trips[prob_col] >= min_p].copy()
    if line_sel != "(all)":
        view = view[view["line_key"].astype(str) == line_sel]
    st.dataframe(
        view.sort_values(prob_col, ascending=False),
        width="stretch",
    )

with tab5:
    fig_dir = VIS_A if VIS_A.exists() else VIS_B
    for name, title in [
        ("reliability_vs_85.png", "Reliability vs 85% target"),
        ("hypothetical_exposure_by_line.png", "Fare exposure by line"),
        ("prob_noncompliant_by_hour.png", "P(non-compliant) by hour"),
        ("reliability_vs_exposure.png", "Reliability vs fare exposure"),
        ("top_trip_noncompliance_prob.png", "Top risky trips"),
    ]:
        p = fig_dir / name
        if p.exists():
            st.markdown(f"**{title}**")
            st.image(str(p), width="stretch")
        else:
            st.caption(f"Missing chart: {name} (run the notebook Phase 7)")

with tab6:
    render_scenario_scorer(trips_raw)
