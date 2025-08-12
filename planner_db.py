
# planner_db.py — DB compartida para Streamlit y FastAPI
from __future__ import annotations
import os, json
from datetime import date
from typing import List, Optional, Dict, Any

from sqlalchemy import create_engine, Column, Integer, String, Text, Float, Date, JSON
from sqlalchemy.orm import declarative_base, sessionmaker
import pandas as pd

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///planner.db")

engine = create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, default="")
    status = Column(String(50), default="Backlog")  # Backlog | Waiting Info | In Progress | Blocked | Done
    category = Column(String(50), default="Trabajo")  # Escuela | Trabajo
    due_date = Column(String(20), nullable=True)  # ISO YYYY-MM-DD (string para simplicidad cross-DB)
    required_info = Column(JSON, default=list)  # lista de strings
    received_info = Column(JSON, default=list)
    business_impact = Column(String(20), default="Medium")  # Low | Medium | High | Critical
    effort = Column(Integer, default=3)  # 1..8
    dependencies = Column(JSON, default=list)  # lista de ids
    priority_label = Column(String(5), default="P4")
    priority_score = Column(Float, default=0.0)
    last_updated = Column(String(32), nullable=True)
    tags = Column(JSON, default=list)

def init_db():
    Base.metadata.create_all(bind=engine)

def _to_py_list(v):
    if v is None: return []
    if isinstance(v, list): return v
    try:
        return json.loads(v)
    except Exception:
        return []

def fetch_tasks_df() -> pd.DataFrame:
    with SessionLocal() as s:
        rows = s.query(Task).all()
        data = []
        for r in rows:
            data.append(dict(
                id=r.id,
                title=r.title,
                description=r.description or "",
                status=r.status or "Backlog",
                category=r.category or "Trabajo",
                due_date=r.due_date,
                required_info=_to_py_list(r.required_info),
                received_info=_to_py_list(r.received_info),
                business_impact=r.business_impact or "Medium",
                effort=r.effort or 3,
                dependencies=_to_py_list(r.dependencies),
                priority_label=r.priority_label or "P4",
                priority_score=r.priority_score or 0.0,
                last_updated=r.last_updated,
                tags=_to_py_list(r.tags),
            ))
        return pd.DataFrame(data) if data else pd.DataFrame(columns=[
            "id","title","description","status","category","due_date","required_info","received_info","business_impact",
            "effort","dependencies","priority_label","priority_score","last_updated","tags"
        ])

def upsert_task_dict(task: Dict[str, Any], task_id: Optional[int] = None) -> int:
    with SessionLocal() as s:
        if task_id:
            obj = s.get(Task, int(task_id))
            if not obj:
                obj = Task(id=int(task_id))
                s.add(obj)
        else:
            obj = Task()
            s.add(obj)
        # asignar campos
        for k, v in task.items():
            if hasattr(obj, k):
                setattr(obj, k, v)
        s.commit()
        s.refresh(obj)
        return obj.id

def delete_task_by_id(task_id: int):
    with SessionLocal() as s:
        obj = s.get(Task, int(task_id))
        if obj:
            s.delete(obj)
            s.commit()

# --------- Prioridad (compartida) ---------
from dateutil import parser as dateparser
from datetime import date

def _impact_points(impact: str) -> int:
    mapping = {"Low": 5, "Medium": 15, "High": 30, "Critical": 40}
    return mapping.get(impact, 15)

def _urgency_points(due: Optional[str], days_window: int) -> float:
    if not due:
        return 0.0
    try:
        due_dt = dateparser.parse(due).date()
    except Exception:
        return 0.0
    days_left = (due_dt - date.today()).days
    if days_left <= 0:
        return 30.0
    score = max(0.0, 30.0 * (1 - (days_left / days_window)))
    return min(30.0, score)

def _info_points(required: List[str], received: List[str]) -> float:
    if not required:
        return 0.0
    got = len(set(x.strip().lower() for x in received) & set(x.strip().lower() for x in required))
    ratio = got / max(1, len(required))
    return 40.0 * ratio

def _effort_penalty(effort: int) -> float:
    e = max(1, min(8, int(effort)))
    return -1.5 * (e - 1)

def _blocked_penalty(status: str, deps_open: bool) -> float:
    if status == "Blocked" or deps_open:
        return -25.0
    return 0.0

def _label_from_score(score: float) -> str:
    if score >= 80: return "P1"
    if score >= 60: return "P2"
    if score >= 40: return "P3"
    return "P4"

def recalc_priority(row: pd.Series, tasks_df: pd.DataFrame, days_window: int = 14):
    required = row.get("required_info", []) or []
    received = row.get("received_info", []) or []
    deps = row.get("dependencies", []) or []

    # dependencias abiertas
    deps_open = False
    if isinstance(deps, list) and deps:
        dep_ids = set(int(d) for d in deps if str(d).isdigit() or isinstance(d, int))
        open_ids = set(dep_ids)
        for _, dep_row in tasks_df.iterrows():
            if int(dep_row["id"]) in dep_ids and dep_row.get("status") == "Done":
                open_ids.discard(int(dep_row["id"]))
        deps_open = len(open_ids) > 0

    base = _impact_points(row.get("business_impact","Medium"))
    urgency = _urgency_points(row.get("due_date"), days_window)
    info = _info_points(required, received)
    effort_pen = _effort_penalty(int(row.get("effort",3)))
    blocked_pen = _blocked_penalty(row.get("status","Backlog"), deps_open)
    score = max(0.0, min(100.0, base + urgency + info + effort_pen + blocked_pen))
    label = _label_from_score(score)

    status = row.get("status","Backlog")
    info_ratio = 0.0 if not required else min(1.0, len(set([x.lower() for x in received]) & set([x.lower() for x in required])) / len(required))
    if status == "Waiting Info" and info_ratio >= 1.0:
        status = "Backlog"
    if deps_open and status not in ("Done","Blocked"):
        status = "Blocked"

    escalated = (label == "P1" and info_ratio >= 1.0)
    return score, label, status, escalated

def top5(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty: return df
    return df.sort_values(by=["priority_label","priority_score"], ascending=[True, False]).head(5)[
        ["id","title","category","status","priority_label","priority_score","due_date"]
    ]
