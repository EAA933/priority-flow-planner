# fastapi_app.py — WhatsApp webhook con parser natural en español
from __future__ import annotations
import os, re, datetime as dt
import pandas as pd
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from twilio.twiml.messaging_response import MessagingResponse

from planner_db import init_db, fetch_tasks_df, upsert_task_dict, recalc_priority, top5

init_db()
app = FastAPI(title="PriorityFlow FastAPI")

@app.get("/health")
def health():
    return {"ok": True}

# ---------- Utilidades parsing ----------
def norm(s: str) -> str:
    s = s.lower().strip()
    for a,b in zip("áéíóúñ", "aeioun"):
        s = s.replace(a,b)
    return s

def parse_date_es(text: str) -> str | None:
    """Devuelve YYYY-MM-DD. Acepta 15/08/2025, 15-08-2025, 15/08, hoy, manana, pasado manana, proximo sabado, etc."""
    t = norm(text)
    today = dt.date.today()

    # hoy / manana / pasado manana
    if "hoy" in t: return today.isoformat()
    if "manana" in t and "pasado" not in t: return (today + dt.timedelta(days=1)).isoformat()
    if "pasado manana" in t: return (today + dt.timedelta(days=2)).isoformat()

    # dd/mm[/yyyy] o dd-mm[-yyyy]
    m = re.search(r"\b(\d{1,2})[\/\-](\d{1,2})(?:[\/\-](\d{2,4}))?\b", t)
    if m:
        d,mn,y = int(m.group(1)), int(m.group(2)), m.group(3)
        year = int(y) if y else today.year
        if year < 100: year += 2000
        try:
            return dt.date(year, mn, d).isoformat()
        except ValueError:
            return None

    # proximo/próximo <dia>, o "el sabado"
    dias = ["lunes","martes","miercoles","jueves","viernes","sabado","domingo"]
    for dia_idx, dia in enumerate(dias):
        if f"proximo {dia}" in t or f"el {dia}" in t or f"{dia}" == t:
            # día de la semana siguiente (si ya pasó hoy)
            curr = today.weekday()  # 0=lunes
            delta = (dia_idx - curr) % 7
            delta = 7 if delta == 0 else delta
            return (today + dt.timedelta(days=delta)).isoformat()
    return None

def parse_add_legacy(body: str) -> dict:
    # add: Título | cat: Trabajo | due: 2025-08-15 | impact: High | info: x,y | tags: a,b | effort: 3
    parts = [p.strip() for p in body.split("|")]
    title = parts[0]
    data = {"title": title, "category":"Trabajo","business_impact":"Medium","effort":3,
            "required_info":[], "received_info":[], "tags":[]}
    for p in parts[1:]:
        if ":" in p:
            k,v = p.split(":",1)
            k = norm(k); v = v.strip()
            if k.startswith("cat"):
                data["category"] = "Escuela" if norm(v).startswith("esc") else "Trabajo"
            elif k == "due" or "fecha" in k:
                iso = parse_date_es(v) or v
                data["due_date"] = iso
            elif "impact" in k or "impacto" in k:
                vv = norm(v)
                mp = {"bajo":"Low","medio":"Medium","alto":"High","critico":"Critical","critical":"Critical","high":"High","medium":"Medium","low":"Low"}
                data["business_impact"] = mp.get(vv, v.capitalize())
            elif "info" in k:
                data["required_info"] = [x.strip() for x in v.split(",") if x.strip()]
            elif "tags" in k or "etiquetas" in k:
                data["tags"] = [x.strip() for x in v.split(",") if x.strip()]
            elif "esfuerzo" in k or "effort" in k:
                try: data["effort"] = int(v)
                except: pass
    return data

def parse_add_natural(body: str) -> dict:
    """Ej: 'agregar Presentacion sabado, categoria escuela, fecha 15/08/2025, impacto alto, esfuerzo 3, info x,y'."""
    raw = body.strip()
    nb = norm(raw)

    # título = texto después de la palabra clave y antes de 'categoria/fecha/impacto/esfuerzo/info/tags'
    # heurística: toma hasta la primera coma
    title = raw
    m = re.match(r"^(agregar|anadir|añadir|crear( tarea)?|nueva( tarea)?)\s+(.*)$", nb)
    if m:
        # título original con mayúsculas: usa raw
        title = raw[len(m.group(0)) - len(m.group(3)) :].strip()
    if "," in title:
        title = title.split(",",1)[0].strip()

    data = {"title": title, "category":"Trabajo","business_impact":"Medium","effort":3,
            "required_info":[], "received_info":[], "tags":[]}

    # categoría
    m = re.search(r"(categoria|categoría)\s*[:=]?\s*(escuela|trabajo)", nb)
    if m:
        data["category"] = "Escuela" if "escuela" in m.group(2) else "Trabajo"

    # fecha
    m = re.search(r"(fecha|para|el)\s*[:=]?\s*([^\.,;]+)", nb)
    if m:
        cand = m.group(2).strip()
        iso = parse_date_es(cand)
        if iso:
            data["due_date"] = iso

    # impacto
    m = re.search(r"(impacto|impact)\s*[:=]?\s*(bajo|medio|alto|critico|critical|high|medium|low)", nb)
    if m:
        mp = {"bajo":"Low","medio":"Medium","alto":"High","critico":"Critical",
              "critical":"Critical","high":"High","medium":"Medium","low":"Low"}
        data["business_impact"] = mp[m.group(2)]

    # esfuerzo
    m = re.search(r"(esfuerzo|effort)\s*[:=]?\s*(\d+)", nb)
    if m:
        try: data["effort"] = int(m.group(2))
        except: pass

    # info requerida
    m = re.search(r"(info|informacion|información)\s*[:=]?\s*([^\n]+)", nb)
    if m:
        lst = [x.strip() for x in m.group(2).split(",") if x.strip()]
        data["required_info"] = lst

    # tags/etiquetas
    m = re.search(r"(tags|etiquetas)\s*[:=]?\s*([^\n]+)", nb)
    if m:
        lst = [x.strip() for x in m.group(2).split(",") if x.strip()]
        data["tags"] = lst

    return data

def build_task(payload: dict) -> dict:
    # valores por defecto y recálculo
    payload.setdefault("status","Backlog")
    payload.setdefault("priority_label","P4")
    payload.setdefault("priority_score",0.0)
    df = fetch_tasks_df()
    s = pd.Series({**payload, "id": -1, "dependencies": []})
    score, label, status, _ = recalc_priority(s, df)
    payload["priority_score"] = score
    payload["priority_label"] = label
    payload["status"] = status
    return payload

# ---------- Webhook ----------
@app.post("/webhook/whatsapp", response_class=PlainTextResponse)
async def whatsapp_webhook(request: Request):
    form = await request.form()
    body = (form.get("Body") or "").strip()
    nb = norm(body)
    resp = MessagingResponse()
    msg = resp.message()

    # ----- crear tarea -----
    if nb.startswith("add:"):
        payload = parse_add_legacy(body[4:].strip())
        task_id = upsert_task_dict(build_task(payload))
        msg.body(f"✅ Tarea creada #{task_id}: {payload['title']} · {payload['category']} · {payload['priority_label']} (score {int(payload['priority_score'])})")
        return PlainTextResponse(str(resp))

    if nb.startswith(("agregar","añadir","anadir","crear","nueva")):
        payload = parse_add_natural(body)
        task_id = upsert_task_dict(build_task(payload))
        msg.body(f"✅ Tarea creada #{task_id}: {payload['title']} · {payload['category']} · {payload['priority_label']} (score {int(payload['priority_score'])})")
        return PlainTextResponse(str(resp))

    # ----- info recibida -----
    if nb.startswith(("recibi","recibí")):
        m = re.search(r"recib[ií]:?\s*(.+?)\s+para\s+(\d+)", body, re.IGNORECASE)
        if not m:
            msg.body("Formato: recibi: <info> para <id>")
            return PlainTextResponse(str(resp))
        item = m.group(1).strip()
        task_id = int(m.group(2))
        df = fetch_tasks_df()
        row = df[df["id"]==task_id]
        if row.empty:
            msg.body(f"❌ No encontré la tarea {task_id}.")
            return PlainTextResponse(str(resp))
        r = row.iloc[0].to_dict()
        rec = set(r.get("received_info",[])); rec.add(item)
        r["received_info"] = list(rec)
        score, label, status, _ = recalc_priority(pd.Series(r), df)
        r["priority_score"], r["priority_label"], r["status"] = score, label, status
        upsert_task_dict(r, task_id=task_id)
        msg.body(f"📥 Registrado '{item}' para tarea #{task_id}. Prioridad: {label} ({int(score)}) · Estado: {status}")
        return PlainTextResponse(str(resp))

    # ----- recordar -----
    if nb.startswith("recordar"):
        df = fetch_tasks_df()
        t5 = top5(df)
        if t5.empty:
            msg.body("No hay tareas pendientes.")
        else:
            lines = [f"#{int(r['id'])} {r['title']} · {r['category']} · {r['priority_label']} ({int(r['priority_score'])}) · due {r.get('due_date') or '-'}"
                     for _, r in t5.iterrows()]
            msg.body("🗒 Top 5:\n" + "\n".join(lines))
        return PlainTextResponse(str(resp))

    # ayuda
    msg.body("Comandos:\n"
             "• agregar <título>, categoría <Escuela/Trabajo>, fecha <dd/mm/aaaa|hoy|mañana|próximo sabado>, impacto <bajo/medio/alto/crítico>, esfuerzo <1-8>, info <a,b>\n"
             "• recordar\n"
             "• recibi: <info> para <id>\n"
             "• (también funciona el formato: add: Título | cat: ... | due: ...)")
    return PlainTextResponse(str(resp))
