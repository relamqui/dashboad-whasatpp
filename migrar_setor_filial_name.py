"""Migração: preenche filial_name nos setores existentes."""
from app import app, db_sql, Setor, Filial

with app.app_context():
    # Adiciona a coluna se não existir (SQLite e Postgres)
    try:
        db_sql.engine.execute("ALTER TABLE setor ADD COLUMN filial_name VARCHAR(100)")
        print("Coluna filial_name criada.")
    except Exception as e:
        print(f"Coluna ja existe ou erro: {e}")

    setores = Setor.query.all()
    count = 0
    for s in setores:
        filial = Filial.query.get(s.filial_id)
        if filial:
            s.filial_name = filial.name
            count += 1
    db_sql.session.commit()
    print(f"Pronto! {count} setores atualizados com o nome da filial.")
