
# cron_daily.py — Enviar resumen diario por WhatsApp
from __future__ import annotations
import os
from twilio.rest import Client
from planner_db import fetch_tasks_df, top5, init_db

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.getenv("TWILIO_FROM")  # ej: 'whatsapp:+14155238886' (sandbox)
USER_WHATSAPP = os.getenv("USER_WHATSAPP")  # ej: 'whatsapp:+52155XXXXXXXX'

def main():
    init_db()
    df = fetch_tasks_df()
    t5 = top5(df)
    if t5.empty:
        text = "No hay tareas pendientes por ahora. 🎉"
    else:
        lines = []
        for _, r in t5.iterrows():
            lines.append(f"#{int(r['id'])} {r['title']} · {r['category']} · {r['priority_label']} ({int(r['priority_score'])}) · due {r.get('due_date') or '-'}")
        text = "Plan del día (Top 5):\n" + "\n".join(lines)

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    client.messages.create(from_=TWILIO_FROM, to=USER_WHATSAPP, body=text)
    print("Mensaje enviado.")

if __name__ == '__main__':
    main()
