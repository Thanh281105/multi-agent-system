import json
import psycopg

new_text = (
    "National-library factual note. The National Library of Vietnam's February 2024 "
    "bibliography, PDF page 177 (viewer index 176), entry 2217, identifies "
    "*Những giấc mơ ở hiệu sách Morisaki* as a work by Yagisawa Satoshi, "
    "translated by Trần Quỳnh Anh from the Japanese title 森崎書店の日々. "
    "This source supports the work only."
)

chunk_id = "row_64f4dfa4fb133884da62121c7143c5b9b460eb997324e11e2c19dbbb1883"
print("New text length:", len(new_text))

with psycopg.connect("postgresql://postgres:@127.0.0.1:55433/evaluation") as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT metadata FROM v2_knowledge_chunks WHERE id = %s;", (chunk_id,))
        meta = cur.fetchone()[0]
        meta["spans"][0]["start_char"] = 0
        meta["spans"][0]["end_char"] = len(new_text)
        cur.execute(
            "UPDATE v2_knowledge_chunks SET content = %s, metadata = %s WHERE id = %s;",
            (new_text, json.dumps(meta), chunk_id),
        )
        conn.commit()

print("Successfully updated chunk in PostgreSQL!")
