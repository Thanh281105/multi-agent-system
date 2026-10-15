import os
from dataclasses import replace
from pathlib import Path
from app.evaluation import v3_cli
from app.core.config import Settings

root = Path('.').resolve()
argv = [
    'validate',
    '--project-root', str(root),
    '--experiment', 'evaluation/v3/review-successor-v1/experiment.v3.json',
    '--gold', 'evaluation/v3/review-successor-v1/gold.v3.json',
    '--split', 'evaluation/v3/review-successor-v1/split.v3.json',
    '--runtime-manifest', 'output/review-remediation/live-runtime-manifest.v2.json',
]
args = v3_cli._parser().parse_args(argv)
inputs = v3_cli._load_inputs(args)

settings = Settings()
os.environ['OPENAI_API_KEY'] = settings.openai_api_key_value
os.environ['DATABASE_URL'] = 'postgresql+psycopg://postgres:@127.0.0.1:55433/evaluation'
os.environ['MODEL_RUNTIME_MODE'] = 'required'
os.environ['OPENAI_PLANNING_MODEL'] = 'gpt-5.4-mini-2026-03-17'
os.environ['OPENAI_SPECIALIST_MODEL'] = 'gpt-5.4-mini-2026-03-17'
os.environ['OPENAI_SYNTHESIS_MODEL'] = 'gpt-5.4-mini-2026-03-17'
os.environ['EMBEDDING_BACKEND'] = 'openai'
os.environ['OPENAI_EMBEDDING_MODEL'] = 'text-embedding-3-small'
os.environ['OPENAI_EMBEDDING_DIMENSIONS'] = '1536'
os.environ['V2_CORPUS_VERSION_ID'] = 'cor_eeb982b0fe5cc975c10ea64ea036ebd93c34d51de31012484c28dfe65a4d'
os.environ['V2_INDEX_MANIFEST_ID'] = 'idx_deb43ea4281d908581b73f624db6e735a371816267f990731ca935808f5f'

inputs = replace(inputs, database_url='postgresql+psycopg://postgres:@127.0.0.1:55433/evaluation')
try:
    factory = v3_cli._build_live_executor_factory(inputs)
    print('SUCCESS! Live executor factory built and all live services validated perfectly!')
except Exception as e:
    import traceback
    traceback.print_exc()
