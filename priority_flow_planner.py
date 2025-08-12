
# priority_flow_planner.py — Streamlit usando DB compartida + tabs + fallback Mermaid
from __future__ import annotations
import streamlit as st
import pandas as pd
from datetime import date
import json

from planner_db import init_db, fetch_tasks_df, upsert_task_dict, delete_task_by_id, recalc_priority, top5
import streamlit.components.v1 as components

# ---------------- Diagram builders ----------------
def build_dot(df: pd.DataFrame) -> str:
    CAT_COLORS = {"Escuela": "#e0f2ff", "Trabajo": "#fff7e0"}
    lanes = ["Backlog", "Waiting Info", "In Progress", "Blocked", "Done"]

    def esc(s: str) -> str:
        return s.replace('"',"''").replace("\n"," ")

    subgraphs, edges = [], []
    for lane in lanes:
        sub = [f'subgraph cluster_{lane.replace(" ","_")} {{', f'label="{lane}"; style="rounded"; color="lightgrey";']
        sub_df = df[df["status"]==lane]
        for _, r in sub_df.iterrows():
            nid = f'n{int(r["id"])}'
            title = esc(str(r["title"]))[:40]
            prio = r.get("priority_label","P4")
            due = (r.get("due_date") or "")
            cat = r.get("category","Trabajo")
            fill = CAT_COLORS.get(cat, "#ffffff")
            label = f'{title}\\n{cat} · {prio}  score={int(r.get("priority_score",0))}'
            if due:
                label += f'\\nDue: {due}'
            sub.append(f'{nid} [shape=box, style="rounded,filled", fillcolor="{fill}", label="{label}"];')
        sub.append("}")
        subgraphs.append("\\n".join(sub))

    for _, r in df.iterrows():
        if isinstance(r.get("dependencies"), list):
            for d in r["dependencies"]:
                if str(d).isdigit():
                    edges.append(f'n{int(d)} -> n{int(r["id"])};')

    dot = "digraph G {\\nrankdir=LR;\\nnode [fontname=Helvetica];\\n" + "\\n".join(subgraphs) + "\\n" + "\\n".join(edges) + "\\n}"
    return dot

def build_mermaid(df: pd.DataFrame) -> str:
    # Mermaid flowchart with subgraphs (swimlane-like)
    lanes = ["Backlog", "Waiting Info", "In Progress", "Blocked", "Done"]
    def esc(s: str) -> str:
        # Mermaid node label supports <br/> for line breaks
        return (str(s).replace('"', '\\"').replace('[', '(').replace(']', ')'))
    lines = ["flowchart LR", "classDef escuela fill:#e0f2ff,stroke:#8aa6c1,color:#1f2d3d;",
             "classDef trabajo fill:#fff7e0,stroke:#cbb86a,color:#1f2d3d;"]
    # subgraphs
    for lane in lanes:
        lines.append(f"subgraph {lane.replace(' ', '_')}[\"{lane}\"]")
        sub_df = df[df["status"]==lane]
        for _, r in sub_df.iterrows():
            nid = f"n{int(r['id'])}"
            title = esc(str(r["title"])[:40])
            prio = r.get("priority_label","P4")
            due = (r.get("due_date") or "-")
            cat = r.get("category","Trabajo")
            cls = "escuela" if cat == "Escuela" else "trabajo"
            label = f"{title}<br/>{cat} · {prio} score={int(r.get('priority_score',0))}<br/>Due: {due}"
            lines.append(f'{nid}["{label}"]')
            lines.append(f"class {nid} {cls}")
        lines.append("end")
    # edges
    for _, r in df.iterrows():
        deps = r.get("dependencies", [])
        if isinstance(deps, list):
            for d in deps:
                try:
                    did = int(d)
                    lines.append(f"n{did} --> n{int(r['id'])}")
                except:
                    pass
    return "\n".join(lines)

def render_flow(df: pd.DataFrame, height: int = 520):
    # Try Graphviz; if not available, render Mermaid as fallback (no system package required).
    try:
        dot = build_dot(df)
        st.graphviz_chart(dot, use_container_width=True)
    except Exception as e:
        mermaid = build_mermaid(df)
        html = f"""
        <div class="mermaid">
        {mermaid}
        </div>
        <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
        <script>
        mermaid.initialize({{ startOnLoad: true, theme: 'dark', securityLevel: 'loose' }});
        </script>
        """
        components.html(html, height=height, scrolling=True)

# ---------------- Main App ----------------
def main():
    st.set_page_config(page_title="Priority Flow Planner", page_icon="🗂️", layout="wide")
    st.title("🗂️ Priority Flow Planner (MVP)")

    init_db()

    with st.sidebar:
        st.header("⚙️ Filtros")
        f_status = st.multiselect("Estatus", ["Backlog","Waiting Info","In Progress","Blocked","Done"], default=["Backlog","Waiting Info","In Progress","Blocked"])
        f_prio = st.multiselect("Prioridad", ["P1","P2","P3","P4"], default=["P1","P2","P3","P4"])
        f_cat = st.multiselect("Categoría", ["Escuela","Trabajo"], default=["Escuela","Trabajo"])
        search = st.text_input("Buscar (título/tags)")
        if st.button("Recargar"):
            st.experimental_rerun()

    df = fetch_tasks_df()

    # Filtros
    if not df.empty:
        def matches(row):
            ok_status = row["status"] in f_status
            ok_prio = row["priority_label"] in f_prio
            ok_cat = row.get("category","Trabajo") in f_cat
            if search:
                s = (row.get("title","") + " " + " ".join(row.get("tags",[]))).lower()
                ok_search = search.lower() in s
            else:
                ok_search = True
            return ok_status and ok_prio and ok_cat and ok_search
        view_df = df[df.apply(matches, axis=1)].copy()
        view_df = view_df.sort_values(by=["priority_label","priority_score"], ascending=[True, False])
    else:
        view_df = df

    tab_manage, tab_view = st.tabs(["✍️ Gestionar tareas", "📊 Vista (Tabla & Diagrama)"])

    with tab_manage:
        st.subheader("➕ Nueva / Editar tarea")
        edit_id = st.selectbox("Editar tarea (opcional)", [None] + (view_df["id"].astype(int).tolist() if not view_df.empty else []))
        initial = {}
        if edit_id:
            row = df[df["id"]==edit_id].iloc[0].to_dict()
            initial = row

        title = st.text_input("Título", value=initial.get("title",""))
        description = st.text_area("Descripción", value=initial.get("description",""))
        col1, col2 = st.columns(2)
        with col1:
            status = st.selectbox("Estatus", ["Backlog","Waiting Info","In Progress","Blocked","Done"], index=["Backlog","Waiting Info","In Progress","Blocked","Done"].index(initial.get("status","Backlog")))
            category = st.selectbox("Categoría", ["Escuela","Trabajo"], index=["Escuela","Trabajo"].index(initial.get("category","Trabajo")))
            impact = st.selectbox("Impacto", ["Low","Medium","High","Critical"], index=["Low","Medium","High","Critical"].index(initial.get("business_impact","Medium")))
        with col2:
            due_date = st.date_input("Vencimiento", value=(date.fromisoformat(initial["due_date"]) if initial.get("due_date") else None))
            effort = st.slider("Esfuerzo (1 fácil → 8 alto)", 1, 8, int(initial.get("effort",3)))
            tags = st.text_input("Tags (coma separada)", value=",".join(initial.get("tags",[])))
        required_info = st.text_input("Info requerida (coma separada)", value=",".join(initial.get("required_info",[])))
        received_info = st.text_input("Info recibida (coma separada)", value=",".join(initial.get("received_info",[])))
        dependencies = st.text_input("Dependencias (IDs coma separada)", value=",".join([str(x) for x in initial.get("dependencies",[])]))

        colA, colB, colC = st.columns([1,1,1])
        if colA.button("Guardar / Actualizar", type="primary"):
            task = dict(
                title=title.strip(),
                description=description.strip(),
                status=status,
                category=category,
                due_date=due_date.isoformat() if due_date else None,
                required_info=[x.strip() for x in required_info.split(",") if x.strip()],
                received_info=[x.strip() for x in received_info.split(",") if x.strip()],
                business_impact=impact,
                effort=int(effort),
                dependencies=[int(x) for x in dependencies.split(",") if x.strip().isdigit()],
                tags=[x.strip() for x in tags.split(",") if x.strip()],
            )
            import pandas as pd
            temp = pd.Series({**task, "id": int(edit_id) if edit_id else -1})
            score, label, new_status, esc = recalc_priority(temp, df)
            task["priority_score"] = score
            task["priority_label"] = label
            task["status"] = new_status
            new_id = upsert_task_dict(task, task_id=int(edit_id) if edit_id else None)
            st.success(f"Tarea {'actualizada' if edit_id else 'creada'} #{new_id} → {label} (score {int(score)})")

        if colB.button("Eliminar", disabled=not bool(edit_id)):
            delete_task_by_id(int(edit_id))
            st.warning(f"Tarea {edit_id} eliminada.")

        if colC.button("Top 5 ahora"):
            t5 = top5(view_df)
            st.dataframe(t5, use_container_width=True, hide_index=True)

    with tab_view:
        st.subheader("📋 Tareas (filtradas)")
        cols = ["id","title","category","status","priority_label","priority_score","due_date","business_impact","effort","required_info","received_info","dependencies","tags"]
        present = [c for c in cols if c in view_df.columns]
        st.dataframe(view_df[present], use_container_width=True, hide_index=True)
        st.subheader("🧭 Diagrama de flujo")
        if view_df.empty:
            st.info("No hay tareas para graficar.")
        else:
            render_flow(view_df, height=560)

if __name__ == "__main__":
    main()
