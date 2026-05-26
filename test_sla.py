import os
from app import app, db_sql, track_sla_event, SlaHistory
import traceback

with app.app_context():
    print("Testing track_sla_event...")
    try:
        track_sla_event("5511999999999", filial="Matriz", setor="Vendas", event_type='QUEUE_ENTER')
        
        sla = SlaHistory.query.filter_by(numero="5511999999999").first()
        if sla:
            print("SUCCESS! SLA Created:", sla.id, sla.numero, sla.entrou_na_fila_em)
        else:
            print("FAILED! No SLA found in DB after track_sla_event.")
    except Exception as e:
        print("EXCEPTION CAUGHT IN SCRIPT:")
        traceback.print_exc()
