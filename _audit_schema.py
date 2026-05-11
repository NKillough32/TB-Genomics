import sys, os
sys.path.insert(0, '.')
os.chdir(os.path.dirname(os.path.abspath(__file__)))
from backend.database import SessionLocal
from sqlalchemy import text

db = SessionLocal()
tables = ['cases','tb_interpretation','consensus_sequences','sequencing_runs',
          'sample_qc_metrics','analysis_provenance','clusters','case_clusters','audit_log']
print("=== ROW COUNTS ===")
for t in tables:
    try:
        n = db.execute(text(f'SELECT COUNT(*) FROM {t}')).scalar()
        print(f'  {t}: {n}')
    except Exception as e:
        print(f'  {t}: ERROR {e}')

print("\n=== COLUMNS ===")
res = db.execute(text(
    "SELECT table_name, column_name, data_type "
    "FROM information_schema.columns "
    "WHERE table_schema = 'public' "
    "ORDER BY table_name, ordinal_position"
)).fetchall()
for row in res:
    print(f'  {row[0]:30s}  {row[1]:35s}  {row[2]}')

print("\n=== SAMPLE DATA (cases) ===")
rows = db.execute(text("SELECT * FROM cases LIMIT 3")).fetchall()
cols = db.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='cases' ORDER BY ordinal_position")).scalars().all()
print('  COLS:', cols)
for row in rows:
    print(' ', dict(zip(cols, row)))

print("\n=== SAMPLE DATA (sequencing_runs) ===")
rows = db.execute(text("SELECT * FROM sequencing_runs LIMIT 3")).fetchall()
for row in rows:
    print(' ', row)

db.close()
