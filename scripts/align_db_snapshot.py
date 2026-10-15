from datetime import datetime, UTC
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from pathlib import Path
from app.evaluation import v3_cli
from app.knowledge.postgres import PostgresKnowledgeStore

db_url = 'postgresql+psycopg://postgres:@127.0.0.1:55433/evaluation'
engine = create_engine(db_url)
with engine.begin() as conn:
    conn.execute(text("UPDATE v2_knowledge_corpus_versions SET published_at = '2026-10-04 14:28:49.458015+00:00' WHERE id = 'cor_eeb982b0fe5cc975c10ea64ea036ebd93c34d51de31012484c28dfe65a4d'"))
    print("Database published_at updated!")

sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
store = PostgresKnowledgeStore(sessions)
db_snapshot = store.resolve_published_snapshot('cor_eeb982b0fe5cc975c10ea64ea036ebd93c34d51de31012484c28dfe65a4d', index_manifest_id='idx_deb43ea4281d908581b73f624db6e735a371816267f990731ca935808f5f')

argv = [
    'validate',
    '--project-root', str(Path('.').resolve()),
    '--experiment', 'evaluation/v3/review-successor-v1/experiment.v3.json',
    '--gold', 'evaluation/v3/review-successor-v1/gold.v3.json',
    '--split', 'evaluation/v3/review-successor-v1/split.v3.json',
    '--runtime-manifest', 'output/review-remediation/live-runtime-manifest.v2.json',
]
args = v3_cli._parser().parse_args(argv)
inputs = v3_cli._load_inputs(args)
print('inputs hash:', v3_cli._index_sha256(inputs.knowledge_snapshot))
print('db hash:    ', v3_cli._index_sha256(db_snapshot))
print('MATCH:', v3_cli._index_sha256(inputs.knowledge_snapshot) == v3_cli._index_sha256(db_snapshot))
