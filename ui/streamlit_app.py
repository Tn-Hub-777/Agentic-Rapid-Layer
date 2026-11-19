"""Streamlit UI.  Run:  streamlit run ui/streamlit_app.py"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import trimesh

from app.config import settings
from app.db.database import reset_db, rows
from app.observability.tracing import summary
from app.service import PrintService
from app.tools import config_rules
from app.tools.scheduler_ops import list_printers

st.set_page_config(page_title="Multi-Agent 3D Print Service", page_icon="🖨️", layout="wide")


@st.cache_resource
def get_service() -> PrintService:
    return PrintService()


svc = get_service()
ss = st.session_state
ss.setdefault("order_id", None)


def render_mesh(path: str):
    mesh = trimesh.load(path, force="mesh")
    if len(mesh.faces) > 20000:
        mesh = mesh.simplify_quadric_decimation(face_count=20000)
    v, f = mesh.vertices, mesh.faces
    fig = go.Figure(go.Mesh3d(x=v[:, 0], y=v[:, 1], z=v[:, 2], i=f[:, 0], j=f[:, 1], k=f[:, 2],
                              color="#4f8bf9", opacity=1.0, flatshading=True))
    fig.update_layout(scene=dict(aspectmode="data"), margin=dict(l=0, r=0, t=0, b=0), height=380)
    return fig


def quote_table(q):
    df = pd.DataFrame(q["line_items"]).rename(columns={"label": "Item", "amount": "₹"})
    df.loc[len(df)] = ["TOTAL", q["total"]]
    st.dataframe(df, hide_index=True, width="stretch")


# ---------------------------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("🖨️ Print Agents")
    st.caption(f"LLM: **{settings.llm_provider}**" + (f" / {settings.llm_model}" if settings.llm_provider != "mock" else " (offline scripted model)"))
    st.caption(f"Slicer: **{settings.slicer_mode}**  ·  Payments: **{settings.payment_provider}**")
    if st.button("Reset demo data"):
        reset_db()
        ss.order_id = None
        st.rerun()
    st.markdown("**Workflow**  \nupload → quote → stock check → *approve* → pay → reserve → schedule → print → deduct")

tab_order, tab_fleet, tab_obs = st.tabs(["New order", "Fleet & inventory", "Agent traces"])

# ---------------------------------------------------------------------------------- order tab
with tab_order:
    if ss.order_id is None:
        st.subheader("1 · Upload and configure")
        left, right = st.columns(2)
        with left:
            up = st.file_uploader("3D model (STL / OBJ / 3MF)", type=["stl", "obj", "3mf"])
            examples = sorted(p.name for p in (ROOT / "examples").glob("*.stl"))
            ex = st.selectbox("…or use an example model", examples)
            goal = st.text_input("Describe what you want (optional)", placeholder="e.g. strong but not too expensive")
        with right:
            mats = [r["name"] for r in rows("SELECT name FROM materials")]
            c1, c2 = st.columns(2)
            material = c1.selectbox("Material", mats)
            color = c2.selectbox("Color", ["white", "black", "red", "blue"])
            infill = c1.select_slider("Infill %", config_rules.ALLOWED_INFILL, value=20)
            layer = c2.select_slider("Layer height (mm)", config_rules.ALLOWED_LAYER_MM, value=0.2)
            walls = c1.number_input("Walls", 1, 6, 2)
            qty = c2.number_input("Quantity", 1, 100, 1)
            delivery = st.selectbox("Delivery", config_rules.ALLOWED_DELIVERY, index=1)
        if st.button("Get quote", type="primary"):
            if up is not None:
                path = Path(tempfile.mkdtemp()) / up.name
                path.write_bytes(up.getvalue())
            else:
                path = ROOT / "examples" / ex
            with st.spinner("Agents are working (quoting → inventory)…"):
                try:
                    v = svc.start_order(str(path), dict(material=material, color=color, infill_percent=infill,
                                                        layer_height_mm=layer, wall_count=int(walls), quantity=int(qty),
                                                        delivery=delivery), goal or None)
                    ss.order_id = v["order_id"]
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
    else:
        v = svc.view(ss.order_id)
        s, pend, status = v["state"], v["pending"], v["status"]
        st.subheader(f"Order {ss.order_id} — {status}")
        cols = st.columns([3, 2])
        with cols[0]:
            if s.get("file_path"):
                st.plotly_chart(render_mesh(s["file_path"]), width="stretch")
        with cols[1]:
            a = s.get("model_analysis") or {}
            if a.get("valid"):
                d = a["dimensions_mm"]
                st.metric("Size (mm)", f"{d['x']:.0f} × {d['y']:.0f} × {d['z']:.0f}")
                st.metric("Volume", f"{a['volume_cm3']} cm³")
                st.metric("Printability", a["printability"])
                for w in a["warnings"]:
                    st.warning(w)

        if status == "FAILED":
            st.error("  \n".join(s.get("errors", ["failed"])))
        if pend and pend["type"] == "quote_approval":
            q = pend["quote"]
            st.markdown("### 2 · Your quote (needs your approval)")
            if pend.get("notice"):
                st.warning(pend["notice"])
            qa, qb = st.columns(2)
            with qa:
                quote_table(q)
                st.caption(f"{q['quote_id']} · valid until {q['expires_at'][:16]} · slicer: {q['slicer']}")
            with qb:
                st.metric("Material", f"{q['weight_g']} g")
                st.metric("Print time", f"{q['print_minutes'] / 60:.1f} h")
                st.info(q["explanation"])
            sg = pend.get("suggestion")
            if sg:
                with st.container(border=True):
                    st.markdown(f"**AI recommendation for “{s.get('goal')}”** — {sg['material']}, {sg['infill_percent']}% infill, "
                                f"{sg['layer_height_mm']} mm layers, {sg['wall_count']} walls  \n_{sg['reason']}_")
                    if st.button("Apply recommendation (re-quote)"):
                        svc.respond_quote(ss.order_id, "modify", {k: sg[k] for k in ("material", "infill_percent", "layer_height_mm", "wall_count")})
                        st.rerun()
            b1, b2, b3 = st.columns(3)
            if b1.button("✅ Approve & pay (mock)", type="primary"):
                with st.spinner("Payment → inventory reservation → scheduler…"):
                    svc.respond_quote(ss.order_id, "approve")
                st.rerun()
            with b2.popover("✏️ Modify configuration"):
                new_inf = st.select_slider("Infill %", config_rules.ALLOWED_INFILL, value=q["config"]["infill_percent"], key="m_inf")
                new_lay = st.select_slider("Layer (mm)", config_rules.ALLOWED_LAYER_MM, value=q["config"]["layer_height_mm"], key="m_lay")
                new_qty = st.number_input("Quantity", 1, 100, q["config"].get("quantity", 1), key="m_qty")
                if st.button("Re-quote"):
                    svc.respond_quote(ss.order_id, "modify", dict(infill_percent=new_inf, layer_height_mm=new_lay, quantity=int(new_qty)))
                    st.rerun()
            if b3.button("Cancel"):
                svc.respond_quote(ss.order_id, "cancel")
                st.rerun()
        if pend and pend["type"] == "print_in_progress":
            asg = pend["assignment"]
            st.success(f"🖨️ Printing on **{asg['printer_name']}** (job {asg['job_id']})")
            st.write(asg.get("explanation", ""))
            p1, p2 = st.columns(2)
            if p1.button("Simulate: print finished ✔"):
                svc.print_finished(ss.order_id, True)
                st.rerun()
            if p2.button("Simulate: print failed ✖"):
                svc.print_finished(ss.order_id, False)
                st.rerun()
        if status == "COMPLETED":
            st.success("Order completed. Filament deducted from inventory.")
            if s.get("inventory_alerts"):
                st.warning("Low stock: " + ", ".join(f"{a['material']} {a['color']} ({a['available_g']:.0f} g)" for a in s["inventory_alerts"]))
        if s.get("payment"):
            st.caption(f"Payment: {s['payment']['status']}")
        with st.expander("Agent conversation / handoffs", expanded=status in ("COMPLETED", "FAILED")):
            for m in s.get("messages", []):
                st.markdown(f"**{m['agent']}** — {m['text']}")
        if st.button("Start a new order"):
            ss.order_id = None
            st.rerun()

# ---------------------------------------------------------------------------------- fleet tab
with tab_fleet:
    a, b = st.columns(2)
    with a:
        st.subheader("Printers")
        df = pd.DataFrame(list_printers())[["id", "name", "status", "loaded_material", "loaded_color", "queue_minutes", "materials"]]
        st.dataframe(df, hide_index=True, width="stretch")
    with b:
        st.subheader("Filament inventory (grams)")
        inv = pd.DataFrame(rows("SELECT material,color,on_hand_g,reserved_g,on_hand_g-reserved_g AS available_g,reorder_level_g FROM stock"))
        st.dataframe(inv, hide_index=True, width="stretch")
    st.subheader("Orders")
    st.dataframe(pd.DataFrame(rows("SELECT id,status,total,updated_at FROM orders ORDER BY updated_at DESC")), hide_index=True, width="stretch")
    st.subheader("Long-term memory (usage per material)")
    items = svc.store.search(("inventory", "usage"))
    st.write({i.key: i.value for i in items} or "nothing yet")

# ---------------------------------------------------------------------------------- observability tab
with tab_obs:
    sm = summary()
    st.subheader("LLM calls by agent")
    st.dataframe(pd.DataFrame(sm["llm_by_agent"]), hide_index=True, width="stretch")
    st.subheader("Tool calls")
    st.dataframe(pd.DataFrame(sm["tools"]), hide_index=True, width="stretch")
    st.subheader("Recent events")
    st.dataframe(pd.DataFrame(rows("SELECT ts,order_id,agent,kind,name,duration_ms,input_tokens,output_tokens,error FROM trace_events ORDER BY id DESC LIMIT 60")),
                 hide_index=True, width="stretch")
