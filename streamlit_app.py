from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

from backend.data import (
    CANONICAL_COLUMNS,
    DataLoadError,
    build_data_profile,
    data_source_catalog,
    inspect_kaggle_source,
    list_kaggle_tables,
    load_demand_data,
)
from backend.decision_intelligence import run_decision_intelligence
from backend.llm_providers import PROVIDER_MODELS, ProviderError, configured as provider_configured
from backend.memory import JsonMemoryStore
from backend.modeling import ModelBundle, train_forecaster
from backend.persistence import get_decision_store
from backend.rag import LocalRagIndex
from backend.service import build_forecast_package
from backend.token_meter import TokenMeter
from backend.call_e_integration import dispatch_escalation

load_dotenv(override=True)

st.set_page_config(
    page_title="GridGuard AI",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .block-container {padding-top: 3rem; padding-bottom: 2rem;}
    .gg-card {
        border: 1px solid rgba(128,128,128,.25);
        border-radius: 14px;
        padding: 1rem 1.1rem;
        background: rgba(128,128,128,.04);
        margin-bottom: .75rem;
    }
    .gg-title {font-size: 2.1rem; font-weight: 750; letter-spacing: -.03em;}
    .gg-subtitle {opacity: .78; margin-top: -.4rem; margin-bottom: 1rem;}
    .status-normal {border-left: 6px solid #2e7d32;}
    .status-watch {border-left: 6px solid #ed9b00;}
    .status-elevated {border-left: 6px solid #e65100;}
    .status-critical {border-left: 6px solid #b71c1c;}
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(show_spinner=False)
def cached_kaggle_tables(raw: bytes, filename: str) -> list[str]:
    return list_kaggle_tables(raw, filename=filename)


@st.cache_data(show_spinner=False)
def cached_kaggle_inspection(raw: bytes, filename: str, table_name: str | None) -> dict:
    return inspect_kaggle_source(raw, filename=filename, table_name=table_name)


@st.cache_resource
def resource_store():
    return get_decision_store()


@st.cache_resource
def resource_memory():
    return JsonMemoryStore(
        os.getenv("GRIDGUARD_MEMORY_PATH", "data/runtime/decision_memory.json"),
        max_records=int(os.getenv("GRIDGUARD_MEMORY_MAX_RECORDS", "200")),
    )


@st.cache_resource
def resource_token_meter():
    return TokenMeter(os.getenv("GRIDGUARD_TOKEN_LEDGER_PATH", "data/runtime/token_usage.json"))


@st.cache_resource
def resource_rag():
    return LocalRagIndex(os.getenv("GRIDGUARD_RAG_DOCS_DIR", "docs/rag"))


if "bundle" not in st.session_state:
    st.session_state.bundle = None
if "forecast_package" not in st.session_state:
    st.session_state.forecast_package = None
if "scenario" not in st.session_state:
    st.session_state.scenario = {"temperature_delta": 0.0, "outage_mw": 0.0, "demand_shock_pct": 0.0}
if "last_data_signature" not in st.session_state:
    st.session_state.last_data_signature = None
if "data_profile" not in st.session_state:
    st.session_state.data_profile = None
if "last_decision_intelligence" not in st.session_state:
    st.session_state.last_decision_intelligence = None

store = resource_store()
memory = resource_memory()
meter = resource_token_meter()
rag = resource_rag()

provider_labels = {
    "internal_expert_system": "Internal Expert System (no tokens)",
    "groq": "GROQ",
    "gemini": "Gemini",
}
provider_options = list(provider_labels)

with st.sidebar:
    st.header("GridGuard Controls")
    source_labels = {
        "synthetic": "Synthetic Demo",
        "kaggle_historical": "Kaggle Historical",
        "eia_live": "EIA Live",
    }
    source_options = list(source_labels)
    default_source = os.getenv("GRIDGUARD_DATA_MODE", "synthetic").strip().lower()
    default_source_index = source_options.index(default_source) if default_source in source_options else 0
    data_mode = st.selectbox(
        "Data source",
        source_options,
        index=default_source_index,
        format_func=lambda value: source_labels[value],
        help="Each adapter converts its source into the same canonical hourly GridGuard schema.",
    )

    kaggle_source = None
    kaggle_filename = None
    kaggle_table_name = None
    kaggle_timestamp_column = None
    kaggle_demand_column = None
    kaggle_temperature_column = None
    kaggle_region = None
    kaggle_timezone = os.getenv("GRIDGUARD_KAGGLE_TIMEZONE", "America/New_York")
    kaggle_missing_policy = os.getenv("GRIDGUARD_KAGGLE_MISSING_POLICY", "interpolate").strip().lower()
    synthetic_seed = 42

    if data_mode == "synthetic":
        history_days = st.slider("Synthetic history (days)", 30, 365, 90, step=15)
        synthetic_seed = int(st.number_input("Synthetic random seed", min_value=1, value=42, step=1))
        st.caption("Generated internally for offline demos and controlled stress testing.")

    elif data_mode == "kaggle_historical":
        history_days = st.slider("Kaggle history window (days)", 30, 3650, 1095, step=30)
        kaggle_input_mode = st.radio(
            "Kaggle input",
            ["Upload CSV or ZIP", "Configured local file"],
            horizontal=False,
        )
        if kaggle_input_mode == "Upload CSV or ZIP":
            uploaded_kaggle = st.file_uploader(
                "Upload Kaggle hourly-energy data",
                type=["csv", "zip"],
                help="Supports a single CSV or a Kaggle ZIP containing one or more CSV files.",
            )
            if uploaded_kaggle is not None:
                kaggle_source = uploaded_kaggle.getvalue()
                kaggle_filename = uploaded_kaggle.name
        else:
            kaggle_path = st.text_input(
                "Local Kaggle file",
                value=os.getenv("GRIDGUARD_KAGGLE_DATA_PATH", "data/kaggle/hourly_energy_consumption_synthetic_benchmark.csv"),
            )
            if kaggle_path:
                kaggle_source = kaggle_path
                kaggle_filename = os.path.basename(kaggle_path)

        if kaggle_source is not None:
            try:
                if isinstance(kaggle_source, bytes):
                    tables = cached_kaggle_tables(kaggle_source, kaggle_filename or "uploaded.csv")
                else:
                    tables = list_kaggle_tables(kaggle_source, filename=kaggle_filename)
                kaggle_table_name = st.selectbox("CSV within source", tables)
                if isinstance(kaggle_source, bytes):
                    inspection = cached_kaggle_inspection(
                        kaggle_source, kaggle_filename or "uploaded.csv", kaggle_table_name
                    )
                else:
                    inspection = inspect_kaggle_source(
                        kaggle_source, filename=kaggle_filename, table_name=kaggle_table_name
                    )
                timestamp_candidates = inspection["timestamp_candidates"]
                demand_candidates = inspection["demand_candidates"]
                temperature_candidates = inspection["temperature_candidates"]
                if timestamp_candidates:
                    kaggle_timestamp_column = st.selectbox("Timestamp column", timestamp_candidates)
                else:
                    st.error("No timestamp column was detected in the selected CSV.")
                if demand_candidates:
                    kaggle_demand_column = st.selectbox("Demand series", demand_candidates)
                else:
                    st.error("No numeric demand series was detected in the selected CSV.")
                temperature_options = ["Use seasonal proxy", *temperature_candidates]
                selected_temperature = st.selectbox("Temperature feature", temperature_options)
                kaggle_temperature_column = None if selected_temperature == "Use seasonal proxy" else selected_temperature
                inferred_region = (kaggle_demand_column or "KAGGLE").removesuffix("_MW").removesuffix(" MW")
                kaggle_region = st.text_input("Region label", value=inferred_region)
                kaggle_timezone = st.text_input("Source timezone", value=kaggle_timezone)
                missing_options = ["interpolate", "drop", "error"]
                missing_index = missing_options.index(kaggle_missing_policy) if kaggle_missing_policy in missing_options else 0
                kaggle_missing_policy = st.selectbox(
                    "Missing-hour policy",
                    missing_options,
                    index=missing_index,
                    format_func=lambda value: {
                        "interpolate": "Interpolate and flag",
                        "drop": "Drop missing hours",
                        "error": "Stop and report",
                    }[value],
                )
                st.caption(
                    f"Detected {inspection['rows']:,} rows, {len(demand_candidates)} demand series, "
                    f"and {len(temperature_candidates)} temperature candidates."
                )
            except DataLoadError as exc:
                st.error(str(exc))
        else:
            st.info("Upload a Kaggle CSV/ZIP or configure a local file path.")

    else:
        default_eia_days = max(30, min(180, int(os.getenv("GRIDGUARD_EIA_HISTORY_HOURS", "2160")) // 24))
        history_days = st.slider("EIA history (days)", 30, 180, default_eia_days, step=15)
        st.caption(f"Balancing authority: {os.getenv('GRIDGUARD_EIA_RESPONDENT', 'ISNE')}")
        if not os.getenv("EIA_API_KEY"):
            st.warning(
                "EIA_API_KEY is not configured. Live loading will fail clearly rather than being presented as live data."
            )

    forecast_hours = st.slider(
        "Forecast horizon", 12, 48, int(os.getenv("GRIDGUARD_FORECAST_HOURS", "24")), step=6
    )
    capacity_mw = st.number_input(
        "Available capacity (MW)", min_value=1000.0, value=27000.0, step=500.0
    )
    train_clicked = st.button("Train / refresh forecast", type="primary", use_container_width=True)

    st.divider()
    
    snapshot = meter.snapshot()["providers"]
    groq_tokens = snapshot["groq"]["total_tokens"]
    gemini_tokens = snapshot["gemini"]["total_tokens"]
    col_tk1, col_tk2 = st.columns([5, 3])
    with col_tk1:
        st.caption(f"**Tokens:** Groq ({groq_tokens:,}) | Gemini ({gemini_tokens:,})")
    with col_tk2:
        if st.button("🔄 Reset", key="reset_all_tokens", use_container_width=True):
            meter.reset()
            st.rerun()
            
    st.subheader("Decision intelligence")
    default_provider = os.getenv("GRIDGUARD_DECISION_PROVIDER", "groq").strip().lower()
    provider_index = provider_options.index(default_provider) if default_provider in provider_options else 0
    decision_provider = st.selectbox(
        "Decision engine",
        provider_options,
        index=provider_index,
        format_func=lambda value: provider_labels[value],
    )
    model_options = PROVIDER_MODELS.get(decision_provider, ["deterministic-rules-v1"])
    env_model_names = {
        "groq": os.getenv("GRIDGUARD_GROQ_MODEL", "openai/gpt-oss-120b"),
        "gemini": os.getenv("GRIDGUARD_GEMINI_MODEL", "gemini-2.5-flash"),
    }
    default_model = env_model_names.get(decision_provider, "deterministic-rules-v1")
    if default_model not in model_options:
        model_options = [default_model, *model_options]
    selected_model = st.selectbox("Model", model_options)
    max_completion_tokens = st.slider("Maximum response tokens", 200, 1500, 700, step=100)

    if decision_provider != "internal_expert_system":
        if provider_configured(decision_provider):
            st.success(f"{provider_labels[decision_provider]} key detected.")
        else:
            required = {"groq": "GROQ_API_KEY", "gemini": "GEMINI_API_KEY"}.get(decision_provider, "API_KEY")
            st.warning(f"{required} is not configured. The internal expert system remains available.")
    else:
        st.caption("Deterministic rules use no external LLM tokens.")

    st.divider()
    persistence = store.status()
    rag_status = rag.status()
    st.caption(f"Persistence: **{persistence['mode'].upper()}**")
    st.caption(f"Shared database: **{'Yes' if persistence['shared'] else 'No'}**")
    st.caption(f"JSON memory records: **{memory.status()['records']}**")
    st.caption(f"RAG index: **{rag_status['documents']} docs / {rag_status['chunks']} chunks**")
    st.caption("Forecast: **XGBoost + seasonal-naive baseline**")

source_signature = (
    data_mode,
    history_days,
    synthetic_seed,
    kaggle_filename,
    kaggle_table_name,
    kaggle_timestamp_column,
    kaggle_demand_column,
    kaggle_temperature_column,
    kaggle_region,
    kaggle_timezone,
    kaggle_missing_policy,
    len(kaggle_source) if isinstance(kaggle_source, bytes) else str(kaggle_source or ""),
)

if train_clicked or st.session_state.bundle is None or st.session_state.last_data_signature != source_signature:
    try:
        with st.spinner("Loading the selected data source and training XGBoost..."):
            demand = load_demand_data(
                mode=data_mode,
                history_days=history_days,
                synthetic_seed=synthetic_seed,
                kaggle_source=kaggle_source,
                kaggle_filename=kaggle_filename,
                kaggle_table_name=kaggle_table_name,
                kaggle_timestamp_column=kaggle_timestamp_column,
                kaggle_demand_column=kaggle_demand_column,
                kaggle_temperature_column=kaggle_temperature_column,
                kaggle_region=kaggle_region,
                kaggle_timezone=kaggle_timezone,
                kaggle_missing_policy=kaggle_missing_policy,
            )
            data_profile = build_data_profile(demand)
            bundle = train_forecaster(demand)
            package = build_forecast_package(
                bundle=bundle,
                horizon=forecast_hours,
                capacity_mw=float(capacity_mw),
                scenario=st.session_state.scenario,
            )
            st.session_state.bundle = bundle
            st.session_state.forecast_package = package
            st.session_state.data_profile = data_profile
            st.session_state.last_data_signature = source_signature
            st.session_state.last_decision_intelligence = None
    except DataLoadError as exc:
        st.error(str(exc))
        st.stop()
    except Exception as exc:
        st.exception(exc)
        st.stop()

bundle: ModelBundle = st.session_state.bundle
package = st.session_state.forecast_package
data_profile = st.session_state.data_profile or build_data_profile(bundle.history)
risk = package["risk"]
forecast = package["forecast"]

st.markdown('<div class="gg-title">⚡ GridGuard Voice Escalation Agent</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="gg-subtitle">Approval-gated, disclosed CALL-E escalation calls informed by XGBoost forecasting and human-reviewed evidence</div>',
    unsafe_allow_html=True,
)

tab_escalation, tab_control, tab_intelligence, tab_scenario, tab_model, tab_audit, tab_committee, tab_data = st.tabs(
    ["Voice Escalation", "Forecast Evidence", "X-Decision & RAG", "Scenario Lab", "Model Quality", "Audit & Operations", "Committee Transcript", "Data Sources"]
)

with tab_control:
    st.caption(
        f"Active source: **{source_labels[data_mode]}** · Region: **{data_profile.get('region', 'Unknown')}** · "
        f"Rows: **{data_profile.get('rows_loaded', len(bundle.history)):,}** · "
        f"Temperature: **{data_profile.get('temperature_method', 'dataset')}**"
    )
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Forecast peak", f"{risk['peak_mw']:,.0f} MW")
    c2.metric("Peak hour", pd.Timestamp(risk["peak_time"]).strftime("%a %H:%M"))
    c3.metric("Reserve margin", f"{risk['reserve_margin_pct']:.1f}%")
    c4.metric("High-risk hours", str(risk["high_risk_hours"]))
    c5.metric("Risk level", risk["level"])

    css_class = f"status-{risk['level'].lower()}"
    st.markdown(
        f"""
        <div class="gg-card {css_class}">
          <strong>{risk['level']} — {risk['headline']}</strong><br>
          {risk['recommendation']}
        </div>
        """,
        unsafe_allow_html=True,
    )

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=bundle.history["timestamp"].tail(168), y=bundle.history["demand_mw"].tail(168), name="Recent actual", mode="lines"))
    fig.add_trace(go.Scatter(x=forecast["timestamp"], y=forecast["forecast_mw"], name="XGBoost forecast", mode="lines+markers"))
    fig.add_hline(y=float(capacity_mw), line_dash="dash", line_color="#fbbf24", annotation_text="Available capacity")
    fig.update_layout(
        title="Recent demand and forward forecast",
        xaxis_title="Timestamp",
        yaxis_title="Demand (MW)",
        height=440,
        legend_orientation="h",
        margin=dict(l=20, r=20, t=60, b=20),
    )
    st.plotly_chart(fig, width="stretch")

    st.subheader("Operator decision")
    decision_col, note_col = st.columns([1, 2])
    with note_col:
        operator_note = st.text_area("Decision rationale", placeholder="Record the operational rationale, constraints, and responsible reviewer.")
    with decision_col:
        approve = st.button("Approve recommendation", type="primary", width="stretch")
        reject = st.button("Reject / hold", width="stretch")

    if approve or reject:
        status = "approved" if approve else "rejected"
        record = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "decision_status": status,
            "risk_level": risk["level"],
            "peak_mw": risk["peak_mw"],
            "reserve_margin_pct": risk["reserve_margin_pct"],
            "recommendation": risk["recommendation"],
            "operator_note": operator_note.strip(),
            "scenario": st.session_state.scenario,
            "model_version": bundle.model_version,
            "data_mode": data_mode,
            "data_source": data_profile.get("source"),
            "data_region": data_profile.get("region"),
        }
        store.append(record)
        st.success(f"Decision {status} and recorded in {store.status()['mode']} persistence.")

    st.dataframe(
        forecast.assign(
            timestamp=lambda frame: frame["timestamp"].dt.strftime("%Y-%m-%d %H:%M"),
            forecast_mw=lambda frame: frame["forecast_mw"].round(0),
        ),
        width="stretch",
        hide_index=True,
    )

with tab_intelligence:
    st.subheader("X-Decision System")
    st.write(
        "The hybrid decision layer combines the XGBoost forecast, transparent expert rules, local RAG policy retrieval, "
        "and an optional LLM explanation. The internal expert recommendation remains visible and human approval is always required."
    )
    i1, i2, i3, i4 = st.columns(4)
    i1.metric("Engine", provider_labels[decision_provider])
    i2.metric("Model", selected_model)
    i3.metric("RAG chunks", rag.status()["chunks"])
    i4.metric("Memory records", memory.status()["records"])

    operator_question = st.text_area(
        "Ask for an operational decision briefing",
        value="Explain the current grid risk, the rules that fired, the recommended action, and what the human operator must verify.",
        height=110,
    )
    
    use_debate = decision_provider in ["groq", "gemini"]

    generate_clicked = st.button("Generate X-Decision briefing", type="primary")
    if generate_clicked:
        if decision_provider != "internal_expert_system" and not provider_configured(decision_provider):
            st.error(f"Cannot generate briefing: The API key for {provider_labels[decision_provider]} is not configured.")
        else:
            try:
                active_provider = "debate_committee" if use_debate else decision_provider
                spinner_text = "The AI Debate Committee is currently in session..." if use_debate else f"Running {provider_labels[decision_provider]} with local RAG..."
                with st.spinner(spinner_text):
                    result = run_decision_intelligence(
                        provider=active_provider,
                        model=selected_model,
                        risk=risk,
                        model_metrics=bundle.metrics,
                        scenario=st.session_state.scenario,
                        operator_question=operator_question,
                        rag=rag,
                        memory=memory,
                        meter=meter,
                        max_completion_tokens=max_completion_tokens,
                    )
                    st.session_state.last_decision_intelligence = result
                st.rerun()
            except ProviderError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.exception(exc)

    result = st.session_state.last_decision_intelligence
    if result:
        st.markdown(result["text"])
        u1, u2, u3, u4 = st.columns(4)
        u1.metric("Provider", result["provider"].upper())
        u2.metric("Prompt tokens", f"{result['usage']['prompt_tokens']:,}")
        u3.metric("Completion tokens", f"{result['usage']['completion_tokens']:,}")
        u4.metric("Total tokens", f"{result['usage']['total_tokens']:,}")

        with st.expander("Explainable expert-system trace", expanded=True):
            st.metric("Rule confidence", f"{result['expert']['confidence']:.0%}")
            st.dataframe(pd.DataFrame(result["expert"]["rules_fired"]), width="stretch", hide_index=True)
            st.warning(result["expert"]["guardrail"])

        with st.expander("Retrieved RAG sources"):
            if result["rag_sources"]:
                for source in result["rag_sources"]:
                    st.code(source, language="text")
            else:
                st.info("No RAG chunks met the retrieval threshold for this question.")

    with st.expander("JSON-backed decision memory"):
        memories = memory.list(limit=20)
        if memories:
            st.dataframe(pd.DataFrame(memories), width="stretch", hide_index=True)
        else:
            st.info("No decision-intelligence conversations have been stored.")
        if st.button("Clear decision memory"):
            memory.clear()
            st.session_state.last_decision_intelligence = None
            st.success("Local JSON-backed decision memory cleared.")
            st.rerun()

with tab_scenario:
    st.write("Stress the forecast without retraining the model.")
    
    presets = {
        "Custom": None,
        "Summer Heatwave": {"temperature_delta": 20.0, "outage_mw": 1500.0, "demand_shock_pct": 8.0},
        "Winter Freeze": {"temperature_delta": -20.0, "outage_mw": 1500.0, "demand_shock_pct": 10.0},
        "Major Plant Trip": {"temperature_delta": 0.0, "outage_mw": 4500.0, "demand_shock_pct": 0.0},
        "Extreme Grid Stress": {"temperature_delta": 15.0, "outage_mw": 6000.0, "demand_shock_pct": 15.0},
    }
    
    with st.container(border=True):
        st.markdown("**Quick Presets** (Dropdown menu)")
        st.write("Select a stress-test preset from the dropdown below.")
        selected_preset = st.selectbox("Choose scenario preset", list(presets.keys()))
        st.caption("This preset updates the stress inputs used to test grid risk, peak demand, capacity, and reserve margin.")
    if selected_preset != st.session_state.get("last_preset"):
        st.session_state.last_preset = selected_preset
        if presets[selected_preset] is not None:
            new_scenario = presets[selected_preset].copy()
        else:
            new_scenario = {"temperature_delta": 0.0, "outage_mw": 0.0, "demand_shock_pct": 0.0}
            
        st.session_state.scenario = new_scenario
        
        # Explicitly update the slider widget states in session_state
        st.session_state.slider_temp = float(new_scenario["temperature_delta"])
        st.session_state.slider_outage = float(new_scenario["outage_mw"])
        st.session_state.slider_shock = float(new_scenario["demand_shock_pct"])
        
        # Automatically run the scenario
        st.session_state.forecast_package = build_forecast_package(
            bundle=bundle,
            horizon=forecast_hours,
            capacity_mw=float(capacity_mw),
            scenario=new_scenario,
        )
        st.session_state.last_decision_intelligence = None
        st.rerun()

    sc1, sc2, sc3 = st.columns(3)
    with sc1:
        temperature_delta = st.slider("Temperature change (°F)", -15.0, 20.0, float(st.session_state.scenario["temperature_delta"]), 1.0, key="slider_temp")
    with sc2:
        outage_mw = st.slider("Generation outage (MW)", 0.0, 6000.0, float(st.session_state.scenario["outage_mw"]), 250.0, key="slider_outage")
    with sc3:
        demand_shock_pct = st.slider("Unexpected demand shock (%)", -10.0, 20.0, float(st.session_state.scenario["demand_shock_pct"]), 1.0, key="slider_shock")

    if st.button("Run scenario", type="primary"):
        scenario = {"temperature_delta": temperature_delta, "outage_mw": outage_mw, "demand_shock_pct": demand_shock_pct}
        st.session_state.scenario = scenario
        st.session_state.forecast_package = build_forecast_package(
            bundle=bundle,
            horizon=forecast_hours,
            capacity_mw=float(capacity_mw),
            scenario=scenario,
        )
        st.session_state.last_decision_intelligence = None
        st.rerun()

    srisk = st.session_state.forecast_package["risk"]
    sf = st.session_state.forecast_package["forecast"]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Scenario risk", srisk["level"])
    m2.metric("Scenario peak", f"{srisk['peak_mw']:,.0f} MW")
    m3.metric("Effective capacity", f"{srisk['effective_capacity_mw']:,.0f} MW")
    m4.metric("Reserve margin", f"{srisk['reserve_margin_pct']:.1f}%")

    scenario_fig = go.Figure(go.Scatter(x=sf["timestamp"], y=sf["forecast_mw"], mode="lines+markers", name="Scenario forecast"))
    scenario_fig.add_hline(y=srisk["effective_capacity_mw"], line_dash="dash", line_color="#fbbf24", annotation_text="Effective capacity")
    scenario_fig.update_layout(height=400, yaxis_title="Demand (MW)", title="Scenario forecast")
    st.plotly_chart(scenario_fig, width="stretch")
    st.info(srisk["recommendation"])

with tab_model:
    st.markdown(f"**Data Provenance**: {bundle.metrics.get('evaluation_type', 'Historical chronological holdout')}")

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("RMSE improvement", f"{bundle.metrics.get('rmse_improvement_pct', 0):.1f}%")
    m2.metric("MAE improvement", f"{bundle.metrics.get('mae_improvement_pct', 0):.1f}%")
    m3.metric("XGBoost RMSE", f"{bundle.metrics.get('xgb_rmse', 0):,.0f} MW")
    m4.metric("Seasonal-naive RMSE", f"{bundle.metrics.get('naive_rmse', 0):,.0f} MW")
    m5.metric("Holdout observations", f"{bundle.metrics.get('test_rows', 0):,}")

    improvement = bundle.metrics.get("mae_improvement_pct", 0)
    if improvement > 0:
        st.success("XGBoost beats the seasonal-naive baseline on the chronological holdout.")
    else:
        st.warning("XGBoost does not beat the seasonal-naive baseline yet. Treat the baseline as the selected model until features or tuning improve.")

    benchmark_json_path = "artifacts/gridguard_benchmark.json"
    benchmark_csv_path = "artifacts/gridguard_benchmark.csv"
    
    if st.button("🚀 Generate Benchmark Artifacts", type="primary", use_container_width=True):
        with st.spinner(f"Running benchmark engine in {data_mode} mode..."):
            env = os.environ.copy()
            env["PYTHONPATH"] = os.path.abspath(os.getcwd())
            result = subprocess.run(
                [sys.executable, "scripts/run_benchmark.py", "--mode", data_mode], 
                env=env, 
                capture_output=True, 
                text=True
            )
            if result.returncode == 0:
                st.success("Benchmark artifacts generated successfully!")
            else:
                st.error(f"Failed to generate artifacts. Error: {result.stderr}")
        st.rerun()
    
    json_exists = os.path.exists(benchmark_json_path)
    csv_exists = os.path.exists(benchmark_csv_path)

    dl_col1, dl_col2, dl_col3 = st.columns([1, 1, 2])
    with dl_col1:
        if json_exists:
            with open(benchmark_json_path, "rb") as f:
                st.download_button("📥 Download Benchmark JSON", f, file_name="gridguard_benchmark.json", mime="application/json", use_container_width=True)
        else:
            st.download_button("📥 Download Benchmark JSON", "", disabled=True, use_container_width=True)
            
    with dl_col2:
        if csv_exists:
            with open(benchmark_csv_path, "rb") as f:
                st.download_button("📥 Download Benchmark CSV", f, file_name="gridguard_benchmark.csv", mime="text/csv", use_container_width=True)
        else:
            st.download_button("📥 Download Benchmark CSV", "", disabled=True, use_container_width=True)

    imp = bundle.feature_importance.head(15).sort_values("importance")
    imp_fig = go.Figure(go.Bar(x=imp["importance"], y=imp["feature"], orientation="h"))
    imp_fig.update_layout(title="XGBoost feature importance (via SHAP)", height=480, xaxis_title="Mean Absolute SHAP Value")
    st.plotly_chart(imp_fig, width="stretch")
    st.caption("Feature importance is extracted using SHAP (SHapley Additive exPlanations) values to show true predictive impact.")

    comparison = bundle.test_predictions.copy()
    compare_fig = go.Figure()
    compare_fig.add_trace(go.Scatter(x=comparison["timestamp"], y=comparison["actual_mw"], name="Actual"))
    compare_fig.add_trace(go.Scatter(x=comparison["timestamp"], y=comparison["xgb_mw"], name="XGBoost"))
    compare_fig.add_trace(go.Scatter(x=comparison["timestamp"], y=comparison["naive_mw"], name="Seasonal naive"))
    compare_fig.update_layout(title="Chronological holdout performance (Backtesting)", height=430, yaxis_title="Demand (MW)")
    st.plotly_chart(compare_fig, width="stretch")
    st.info("""
    **Understanding this Backtesting Graph:**
    This graph represents our strict **chronological backtesting** methodology. To prevent data leakage, the most recent 20% of the dataset was entirely hidden during the training phase. 
    The chart plots how accurately the trained XGBoost model predicted this unseen "future" holdout window compared to the true actuals and a simple seasonal naive baseline (which just assumes demand matches exactly 168 hours prior).
    """)

    st.subheader("Feature Correlation")
    from backend.features import build_training_frame
    full_features = build_training_frame(bundle.history)
    top_features = bundle.feature_importance["feature"].head(10).tolist()
    corr_cols = ["demand_mw"] + top_features
    corr_matrix = full_features[corr_cols].corr().round(2)
    
    corr_fig = go.Figure(data=go.Heatmap(
        z=corr_matrix.values,
        x=corr_matrix.columns,
        y=corr_matrix.index,
        colorscale='RdBu',
        zmin=-1, zmax=1,
        text=corr_matrix.values,
        texttemplate="%{text}",
        hoverinfo="text"
    ))
    corr_fig.update_layout(title="Top Features Correlation Heatmap", height=500)
    st.plotly_chart(corr_fig, width="stretch")
    
    st.info("""
    **How to read the Correlation Heatmap:**
    - **Deep Blue (+1.0) & Deep Red (-1.0):** Indicate strong positive and negative correlations.
        - **What is "Good":** You want your predictive features (like lags or temperature) to have strong correlations (deep colors) with the target variable (`demand_mw`). This indicates they possess high predictive power.
    - **White / Pale Colors (Near 0.0):** Indicate little to no correlation.
        - **What is "Good":** You want the features to have pale/white correlations *with each other*. If two features are highly correlated with each other (deep colors), it means they are redundant (multicollinearity), which can make the model less stable and harder to interpret.
    """)

with tab_audit:
    st.subheader("Persistence and service readiness")
    status = store.status()
    memory_status = memory.status()
    rag_status = rag.status()
    p1, p2, p3, p4 = st.columns(4)
    p1.metric("Decision persistence", status["mode"].upper())
    p2.metric("Shared", "Yes" if status["shared"] else "No")
    p3.metric("JSON memory", f"{memory_status['records']} records")
    p4.metric("RAG", "Ready" if rag_status["ready"] else "Empty")

    st.code(
        f"""Streamlit UI
  ↓
XGBoost forecast + expert rules + local RAG
  ↓
Optional Grok / Groq / Gemini explanation
  ↓
Human approval
  ↓
{status['mode'].upper()} decision store + JSON memory/token ledger""",
        language="text",
    )

    st.subheader("Token ledger")
    usage_snapshot = meter.snapshot()["providers"]
    usage_rows = [{"provider": provider, **counters} for provider, counters in usage_snapshot.items()]
    st.dataframe(pd.DataFrame(usage_rows), width="stretch", hide_index=True)
    st.caption("These are tokens reported to this app by API responses. Resetting the meter does not reset provider quotas or billing.")
    if st.button("Reset all local token meters"):
        meter.reset()
        st.success("All GridGuard token counters reset.")
        st.rerun()

    st.subheader("Human decision records")
    records = store.list(limit=100)
    if records:
        st.dataframe(pd.DataFrame(records), width="stretch", hide_index=True)
    else:
        st.info("No human decisions have been recorded yet.")

    st.caption("Flask endpoints: /health, /ready, /api/status, /api/decisions, /api/data/sources, /api/intelligence/status, /api/tokens")

with tab_committee:
    st.subheader("Multi-Agent Committee Debate")
    if 'last_decision_intelligence' in st.session_state and st.session_state.last_decision_intelligence and st.session_state.last_decision_intelligence.get("provider") == "debate_committee":
        result = st.session_state.last_decision_intelligence
        st.write("Review the step-by-step decision process as the agents debated the risk and policies.")
        
        st.markdown("### Processing Pipeline")
        cols = st.columns(len(result["committee_transcript"]))
        for idx, (col, msg) in enumerate(zip(cols, result["committee_transcript"])):
            with col:
                st.markdown(f"**Step {idx+1}**")
                st.info(f"**{msg['role']}**\n\n*(via {msg['provider']})*")
        
        st.divider()
        
        st.markdown("### Official Transcript")
        for msg in result["committee_transcript"]:
            with st.chat_message("assistant", avatar="🤖"):
                st.markdown(f"**{msg['role']}** _({msg['provider']})_")
                st.markdown(msg["content"])
    else:
        st.info("Run the **Debate Committee** engine from the X-Decision tab to view the multi-agent transcript.")

with tab_data:
    st.subheader("Three-source data ingestion")
    st.write(
        "GridGuard can run from one selected source at a time. Every adapter validates and converts its input into the same "
        "canonical hourly schema before feature engineering and XGBoost training."
    )

    source_catalog = data_source_catalog(kaggle_available=kaggle_source is not None)
    source_columns = st.columns(3)
    for column, descriptor in zip(source_columns, source_catalog):
        with column:
            is_active = descriptor["id"] == data_mode
            readiness = "ACTIVE" if is_active else ("READY" if descriptor["ready"] else "NEEDS CONFIGURATION")
            data_kind = "Real data" if descriptor["real_data"] else "Synthetic data"
            st.markdown(
                f"""
                <div class="gg-card">
                  <strong>{descriptor['label']}</strong><br>
                  <small>{readiness} · {data_kind}</small><br><br>
                  {descriptor['purpose']}
                </div>
                """,
                unsafe_allow_html=True,
            )

    st.code(
        """Synthetic generator ─┐
Kaggle CSV / ZIP ───┼──> Source adapter ──> Validation ──> Canonical hourly schema ──> XGBoost
EIA hourly API ─────┘""",
        language="text",
    )

    st.subheader("Active-source profile")
    d1, d2, d3, d4, d5 = st.columns(5)
    d1.metric("Source", source_labels[data_mode])
    d2.metric("Region", str(data_profile.get("region", "Unknown")))
    d3.metric("Rows", f"{data_profile.get('rows_loaded', len(bundle.history)):,}")
    d4.metric("Missing hours", f"{data_profile.get('missing_hours_detected', 0):,}")
    d5.metric("Interpolated", f"{data_profile.get('interpolated_hours', 0):,}")

    profile_rows = [
        {"property": key.replace("_", " ").title(), "value": str(value)}
        for key, value in data_profile.items()
        if key != "quality_counts"
    ]
    st.dataframe(pd.DataFrame(profile_rows), width="stretch", hide_index=True)

    st.subheader("Canonical GridGuard schema")
    schema_purpose = {
        "timestamp": "UTC hourly observation",
        "demand_mw": "Target electricity demand in MW",
        "temperature_f": "Observed temperature or clearly identified seasonal proxy",
        "region": "Balancing authority or historical demand series",
        "is_holiday": "Calendar indicator used by feature engineering",
        "source": "Traceable source identifier",
        "data_quality_status": "Observed, interpolated, or synthetic status",
    }
    st.dataframe(
        pd.DataFrame(
            [{"column": column, "purpose": schema_purpose[column]} for column in CANONICAL_COLUMNS]
        ),
        width="stretch",
        hide_index=True,
    )

    st.subheader("Latest normalized observations")
    st.dataframe(bundle.history.tail(100), width="stretch", hide_index=True)
    
    st.subheader("Export Data")
    csv_col1, csv_col2 = st.columns(2)
    with csv_col1:
        st.download_button(
            label="📥 Download Historical Data (CSV)",
            data=bundle.history.to_csv(index=False).encode('utf-8'),
            file_name=f"gridguard_history_{data_mode}.csv",
            mime="text/csv",
            use_container_width=True
        )
    with csv_col2:
        st.download_button(
            label="📥 Download Forecast Data (CSV)",
            data=forecast.to_csv(index=False).encode('utf-8'),
            file_name=f"gridguard_forecast_{data_mode}.csv",
            mime="text/csv",
            use_container_width=True
        )

    with st.expander("How to use each source", expanded=True):
        st.markdown(
            """
            **Synthetic Demo** — works immediately without credentials. Use it for offline demonstrations and controlled stress cases.

            **Kaggle Historical** — upload a CSV or ZIP, choose the timestamp and MW demand series, select a missing-hour policy, and train on a multiyear historical window. PJM-style columns such as `Datetime`, `PJME_MW`, `AEP_MW`, and similar regional series are detected automatically.

            **EIA Live** — configure `EIA_API_KEY` and a balancing authority such as `ISNE`. The connector retrieves recent hourly demand and clearly reports API failures. It never silently substitutes synthetic data.
            """
        )

    st.subheader("Limitations")
    st.markdown(
        """
        - The MVP uses point forecasts and does not yet provide calibrated uncertainty intervals.
        - Synthetic mode is representative rather than an ISO-NE replica and is not presented as real-world validation.
        - Kaggle datasets may use regional local time; the selected timezone is converted to UTC and shown in the profile.
        - Kaggle and EIA modes use a clearly identified seasonal temperature proxy unless the source includes a temperature column.
        - EIA live mode currently loads demand only; dedicated weather, generation, interchange, and outage feeds are planned.
        - The first implementation trains on one selected source at a time; it does not mix regions with incompatible demand scales.
        - Recursive forecasting can accumulate error over longer horizons.
        - Local TF-IDF RAG is intentionally lightweight and does not replace an enterprise vector database.
        - LLM text is advisory and may be incorrect; the deterministic expert trace remains visible.
        - Token counters reflect responses observed by GridGuard, not authoritative provider billing balances.
        - Recommendations require human approval and the software does not control critical infrastructure.
        """
    )

with tab_escalation:
    st.subheader("Approval-Gated Voice Escalation via CALL-E")
    
    # Lifecycle Visualization
    st.markdown("##### 🔄 Escalation Lifecycle")
    st.markdown(
        """
        `Risk detected` ➔ `Human review` ➔ `CALL-E plan` ➔ `Explicit approval` ➔ `Call result` ➔ `Escalation packet`
        """
    )
    st.divider()
    
    # Check trigger condition
    if risk["level"] in ["CRITICAL", "ELEVATED"]:
        # Create Advisory Record
        import random
        if "advisory_id" not in st.session_state:
            st.session_state.advisory_id = f"GG-{random.randint(1000, 9999)}"
            
        advisory = {
            "advisory_id": st.session_state.advisory_id,
            "asset_id": "Feeder-05",
            "risk_type": f"{risk['level']} Outage Risk",
            "risk_score": float(risk['peak_mw']),
            "confidence": "High",
            "severity": risk['level'],
            "evidence_summary": f"Grid risk level is {risk['level']} with peak of {risk['peak_mw']:,.0f} MW.",
            "recommended_action": "Request emergency response availability.",
            "status": "PENDING_HUMAN_REVIEW"
        }

        st.markdown("### 🚨 Advisory Record")
        st.info(f"**Advisory ID**: {advisory['advisory_id']} | **Asset**: {advisory['asset_id']} | **Severity**: {advisory['severity']}")
        st.write(f"**Evidence**: {advisory['evidence_summary']}")
        st.write(f"**Recommended Action**: {advisory['recommended_action']}")
        
        test_number = os.environ.get("CALLE_AUTHORIZED_TEST_NUMBER")
        masked_number = f"***-***-{test_number[-4:]}" if test_number else "Not configured"
        
        c1, c2 = st.columns([2, 1])
        with c1:
            st.text_input("Authorized Test Recipient", value=masked_number, disabled=True, help="Masked for security")
            

        # Safety Panel
        with st.container(border=True):
            st.markdown("#### 🛡️ Live-Call Safety Panel")
            st.info("⚠️ **AI Disclosure**: This call will be placed by an AI voice agent (CALL-E). The recipient will be informed they are speaking with an AI.")
            
            st.warning("**Decision support only.** This agent never controls grid infrastructure, makes emergency decisions, or contacts recipients without explicit human initiation and second-level consent.")
            
            dry_run_toggle = st.toggle("Dry-run preview (Default)", value=True)
            live_mode = not dry_run_toggle
            
            from backend.call_e_integration import DRY_RUN_BRANCHES
            dry_run_scenario = DRY_RUN_BRANCHES[4]
            if not live_mode:
                with st.container(border=True):
                    st.markdown("**Dry-Run Simulator Outcome** (Dropdown menu)")
                    st.write("Select a simulated caller response branch from the dropdown below.")
                    dry_run_scenario = st.selectbox(
                        "Choose dry-run branch", 
                        DRY_RUN_BRANCHES
                    )
                    st.caption("This selection controls the simulated human response, final status, audit result, and escalation package behavior.")
            
            can_dispatch = True
            if live_mode:
                if not test_number:
                    st.error("🚨 LIVE MODE BLOCKED: CALLE_AUTHORIZED_TEST_NUMBER is not configured.")
                    can_dispatch = False
                else:
                    st.error("🚨 LIVE MODE: This will place a real disclosed test call to the configured authorized test recipient.")
                    st.warning("Live demonstration call: a non-emergency test call to the authorized recipient only. No grid action or emergency response is requested.")
            
            consent_draft = st.checkbox("1️⃣ I verify the call details and consent to drafting this voice escalation.")
            
            if consent_draft:
                st.success("Draft prepared. Awaiting final confirmation to dispatch.")
                
                confirm_label = (
                    "2️⃣ I confirm I want to place this real disclosed test call." if live_mode
                    else "2️⃣ I confirm I want to run the selected dry-run branch. No call will be placed."
                )
                consent_dispatch = st.checkbox(confirm_label)
                
                button_label = "Place real disclosed test call" if live_mode else "Run Selected Dry-Run Branch"
                if st.button(button_label, type="primary", disabled=not (consent_dispatch and can_dispatch)):
                    if "current_idempotency_key" not in st.session_state:
                        import uuid
                        st.session_state.current_idempotency_key = str(uuid.uuid4())
                        
                    try:
                        with st.status("Waiting for CALL-E result...") as status:
                            from backend.call_e_integration import dispatch_escalation
                            final_res = dispatch_escalation(
                                advisory, 
                                dry_run=not live_mode,
                                idempotency_key=st.session_state.current_idempotency_key,
                                scenario=dry_run_scenario
                            )
                            
                        if final_res.get("status") == "failed":
                            status.update(label="Escalation result retrieval failed.", state="error")
                            msg = final_res.get("message", "Call did not complete successfully.")
                            st.error(f"🚨 {msg}")
                            with st.expander("CALL-E diagnostic details"):
                                st.warning("No additional call will be attempted automatically.")
                                st.json(final_res)
                        else:
                            status.update(label="Escalation result received.", state="complete")
                            final_status = final_res.get("final_status")
                            
                            if not live_mode:
                                st.markdown("### Dry-run Human Approval Branch")
                                st.info("This is a simulated CALL-E result. No phone call was placed. The dry run proves how GridGuard handles each human-in-the-loop outcome before any real escalation.")
                                
                                st.info("Rule: escalation requires authorization, evidence review, and explicit approval. Review alone does not authorize escalation.")
                                
                                from backend.call_e_integration import dispatch_escalation, DRY_RUN_BRANCHES
                                import pandas as pd
                                mapping_data = []
                                for b in DRY_RUN_BRANCHES:
                                    res = dispatch_escalation(advisory, dry_run=True, scenario=b)
                                    mapping_data.append({
                                        "Dry-run selection": b,
                                        "Final status": res.get("final_status", "UNKNOWN"),
                                        "Workflow result": res.get("workflow_result_text", "Unknown")
                                    })
                                df_mapping = pd.DataFrame(mapping_data)
                                
                                executed_scenario = final_res.get("dry_run_scenario", dry_run_scenario)
                                current_row = df_mapping[df_mapping["Dry-run selection"] == executed_scenario].iloc[0]
                                st.info(
                                    f"**Current Dry-run Selection**\n\n"
                                    f"- **Selected dry-run branch:** {current_row['Dry-run selection']}\n"
                                    f"- **Current expected status:** {current_row['Final status']}\n"
                                    f"- **Workflow result:** {current_row['Workflow result']}"
                                )
                                
                                with st.container(border=True):
                                    st.markdown("**Reference: All Dry-run Outcome Branches**")
                                    st.caption("Reference only: this table shows every possible dry-run branch. The highlighted row matches the current selected branch.")
                                    
                                    def highlight_selected_row(row):
                                        if row["Dry-run selection"] == executed_scenario:
                                            return ['background-color: #166534; color: #ffffff; font-weight: 600'] * len(row)
                                        return ['background-color: #1e293b; color: #e2e8f0'] * len(row)
                                    
                                    styled_df = df_mapping.style.apply(highlight_selected_row, axis=1)
                                    st.table(styled_df)
                            
                            st.markdown("### Human-in-the-loop Status Panel")
                            
                            # Safely infer mode if not explicitly passed in final_res
                            inferred_mode = "Unknown"
                            if live_mode:
                                inferred_mode = "Live CALL-E result"
                            else:
                                inferred_mode = "Dry-run fixture"
                                
                            st.write(f"- Mode: **{inferred_mode}**")
                            st.write(f"- Current Status: **{final_status}**")
                            auth_val = final_res.get('auth_confirmed')
                            if auth_val is True: auth_display = "Confirmed"
                            elif auth_val is False: auth_display = "False"
                            else: auth_display = "Unknown"
                            
                            rev_val = final_res.get('review_confirmed')
                            if rev_val is True: review_display = "Yes"
                            elif rev_val is False: review_display = "No"
                            else: review_display = "Unknown"
                            
                            decision_val = str(final_res.get('approval_decision')).lower() if final_res.get('approval_decision') else "none"
                            if decision_val == "approve":
                                decision_display = "Approved"
                            elif decision_val == "reject":
                                decision_display = "Rejected"
                            elif decision_val == "hold":
                                decision_display = "Hold"
                            elif decision_val == "none":
                                decision_display = "None"
                            else:
                                decision_display = decision_val.capitalize()
                            st.write(f"- Authorized Approver: **{auth_display}**")
                            st.write(f"- Review Confirmed: **{review_display}**")
                            st.write(f"- Escalation Decision: **{decision_display}**")
                            response_understood = final_res.get("response_understood", "Unknown")
                            st.write(f"- Response Understood: **{response_understood}**")
                            
                            if final_status == "WRONG_RECIPIENT":
                                st.error("Call ended. Recipient was not authorized. Escalation blocked.")
                            elif final_status == "PENDING_REVIEW":
                                st.warning("Escalation blocked. Advisory remains pending review.")
                            elif final_status == "REVIEWED_NOT_APPROVED":
                                st.warning("Human reviewed advisory but did not approve escalation.")
                            elif final_status == "REVIEWED_HOLD":
                                st.info("Escalation placed on hold by reviewer.")
                            elif final_status == "ESCALATION_APPROVED":
                                st.success("Human approval confirmed. Escalation workflow activated.")
                                st.markdown("#### 📦 Escalation Package")
                                st.json({
                                    "advisory_id": advisory["advisory_id"],
                                    "incident_priority": advisory["severity"],
                                    "recommended_response_team": "Grid Operations Shift Team",
                                    "action_summary": advisory["recommended_action"],
                                    "customer_impact_risk": "Moderate",
                                    "next_steps": "Deploy rapid response unit to asset.",
                                    "audit_reference": final_res.get("call_id") or "simulated_call_id"
                                })
                            else:
                                st.warning("Response unclear. Manual follow-up needed.")
                                
                            if not final_res.get("create_escalation_package", final_status == "ESCALATION_APPROVED"):
                                st.info("No escalation package created because human approval was not completed.")
                            
                            from backend.persistence import get_decision_store
                            store = get_decision_store()
                            audit_packet = {
                                "timestamp": final_res.get("timestamp"),
                                "advisory_id": advisory["advisory_id"],
                                "mode": "Live CALL-E result" if live_mode else "Dry-run fixture",
                                "called_number": masked_number if live_mode else "Simulated",
                                "expected_approver_role": "Grid Operations Shift Supervisor",
                                "auth_confirmed": final_res.get("auth_confirmed"),
                                "review_confirmed": final_res.get("review_confirmed"),
                                "approval_decision": final_res.get("approval_decision"),
                                "final_status": final_status,
                                "response_understood": final_res.get("response_understood", "Unknown"),
                                "recipient_response": final_res.get("recipient_response", "None"),
                                "create_escalation_package": final_res.get("create_escalation_package", final_status == "ESCALATION_APPROVED"),
                                "transcript_summary": final_res.get("transcript_summary")
                            }
                            if not live_mode and "dry_run_scenario" in final_res:
                                audit_packet["dry_run_scenario"] = final_res["dry_run_scenario"]
                                
                            store.append({"type": "voice_escalation", "packet": audit_packet})
                            
                            with st.container(border=True):
                                st.markdown("### Escalation Result Packet")
                                st.markdown(f"**Mode:** {'Live CALL-E result' if live_mode else 'Dry-run fixture'}")
                                st.markdown(f"**Recipient:** `{masked_number if live_mode else 'Not applicable'}`")
                                st.markdown(f"**Call ID:** `{final_res.get('call_id') if live_mode else 'Not applicable'}`")
                                st.markdown(f"**Terminal Status:** `{final_status}`")
                                st.markdown(f"**Audit Packet:** `Saved`")
                                st.write(f"- Recipient Response: {final_res.get('recipient_response', 'None')}")
                                st.markdown("#### Compact Transcript")
                                
                                import json, ast, re
                                import pandas as pd
                                
                                turns = final_res.get("transcript_turns", [])
                                raw_transcript = final_res.get("transcript_summary", "")
                                
                                if not turns and raw_transcript:
                                    if isinstance(raw_transcript, list):
                                        turns = raw_transcript
                                    elif isinstance(raw_transcript, str):
                                        try:
                                            turns = json.loads(raw_transcript)
                                        except:
                                            try:
                                                turns = ast.literal_eval(raw_transcript)
                                            except:
                                                pass
                                        
                                        if not isinstance(turns, list):
                                            turns = []
                                            text_turns = re.split(r'(Bot:|User:|Agent:|Operator:|Recipient:)', raw_transcript, flags=re.IGNORECASE)
                                            if len(text_turns) > 1:
                                                current_speaker = "bot"
                                                for part in text_turns:
                                                    if not part.strip(): continue
                                                    lower_part = part.strip().lower()
                                                    if lower_part in ["bot:", "agent:"]:
                                                        current_speaker = "bot"
                                                    elif lower_part in ["user:", "operator:", "recipient:"]:
                                                        current_speaker = "user"
                                                    else:
                                                        turns.append({"speaker": current_speaker, "text": part.strip()})
                                
                                if turns and isinstance(turns, list) and len(turns) > 0:
                                    table_data = []
                                    for t in turns:
                                        if not isinstance(t, dict): continue
                                        
                                        speaker_raw = str(t.get("speaker", t.get("role", ""))).lower()
                                        msg = t.get("text", t.get("message", t.get("content", "")))
                                        time_val = t.get("time", t.get("offset", ""))
                                        
                                        if speaker_raw in ["bot", "assistant", "agent"]:
                                            speaker_label = "GridGuard Agent"
                                        elif speaker_raw in ["user", "human", "recipient", "operator"]:
                                            speaker_label = "Human Recipient"
                                        else:
                                            speaker_label = "GridGuard Agent"
                                            
                                        row = {"Speaker": speaker_label, "Message": msg}
                                        if time_val:
                                            row["Time/Offset"] = time_val
                                        table_data.append(row)
                                        
                                    st.table(pd.DataFrame(table_data))
                                    
                                    with st.expander("Raw transcript data"):
                                        st.write(raw_transcript)
                                else:
                                    st.info(raw_transcript if raw_transcript else "Mock fixture")
                                    
                                st.markdown("##### Interpretation Summary")
                                auth_display = "Confirmed" if final_res.get('auth_confirmed') else ("Not confirmed" if final_res.get('auth_confirmed') is False else "Unknown")
                                review_display = "Yes" if final_res.get('review_confirmed') else ("No" if final_res.get('review_confirmed') is False else "Unknown")
                                decision_display = str(final_res.get('approval_decision')).capitalize() if final_res.get('approval_decision') else "Unknown"
                                
                                st.write(f"- Authorized approver: {auth_display}")
                                st.write(f"- Review confirmed: {review_display}")
                                st.write(f"- Escalation decision: {decision_display}")
                                st.write(f"- Final status: {final_status}")
                            
                            if live_mode and "field_inspection" in final_res:
                                with st.expander("CALL-E response field inspection"):
                                    for k, v in final_res["field_inspection"].items():
                                        st.write(f"- `{k}`: **{v}**")
                    except Exception as e:
                        st.error(f"Failed to execute call: {str(e)}")
                    finally:
                        if "current_idempotency_key" in st.session_state:
                            del st.session_state.current_idempotency_key
            else:
                st.warning("Please check the consent box to proceed with drafting the call.")
                
        st.markdown("---")

        st.markdown("---")
        st.markdown("### Live CALL-E result retrieval")
        st.info("GridGuard retrieves results only for calls created by this application, using the provider API call ID returned at creation time. Dashboard call-record IDs are not accepted here.")
            
    else:
        st.success(f"Current grid risk is {risk['level']}. No automated escalation is required at this time.")
