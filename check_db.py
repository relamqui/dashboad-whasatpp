from app import app, db_sql
with app.app_context():
    res = db_sql.session.execute(db_sql.text("SELECT DISTINCT ultimo_setor FROM atendimentos_chat WHERE status = 'atendente'")).fetchall()
    print("ATENDIMENTOS CHAT:")
    for r in res: print(repr(r[0]))
