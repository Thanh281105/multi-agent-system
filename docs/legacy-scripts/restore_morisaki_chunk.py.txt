import sys, json, psycopg
sys.stdout.reconfigure(encoding='utf-8')

with open('data/knowledge/books-v2/sources.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

morisaki_src = None
for s in data['sources']:
    if s['source_id'] == 'src_morisaki_nlv':
        morisaki_src = s
        break

content = morisaki_src['content_markdown']
chunk_row_id = "row_64f4dfa4fb133884da62121c7143c5b9b460eb997324e11e2c19dbbb1883"

new_meta = {
    "spans": [
        {
            "span_id": "spn_2b14026eea6596f33d4ceaec7910de405f0e2e3ddeb12a7ebabd3d6c0a1f",
            "end_char": 436,
            "start_char": 0,
            "content_hash": "01de0deee6fddbfb49361dd61c58df1ffd593a26023fd1e5d9d5b3f62c2d44c3"
        }
    ],
    "content_hash": "07fe32b0ef3a127ee3a69233d56fef67213357a9e5dab41f096a04a74078394c",
    "schema_version": "1.0",
    "chunker_version": "table_chunker_v1",
    "public_chunk_id": "chk_d0720141f3990596007a97d721565286c6c2ee48b369cbdf74a4cb2fa42b"
}

with psycopg.connect("postgresql://postgres:@127.0.0.1:55433/evaluation") as conn:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE v2_knowledge_chunks
            SET content = %s, metadata = %s
            WHERE id = %s;
            """,
            (content, json.dumps(new_meta), chunk_row_id),
        )
        print("Updated rows:", cur.rowcount)
        conn.commit()

print("Successfully restored morisaki chunk in PostgreSQL!")
