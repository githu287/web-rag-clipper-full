import json
from pathlib import Path

import httpx

from backend.core.config import get_settings
from backend.repositories.milvus.impl import PyMilvusRepositoryImpl


root = Path("evaluation")
mapping_path = root / "private" / "dataset_placeholder_mapping.json"
credentials_path = root / "private" / "credentials.json"
mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
plugin_id = mapping["<PLUGIN_A_PID>"]
secret_entry = credentials[plugin_id]
plugin_secret = (
    secret_entry["plugin_secret"]
    if isinstance(secret_entry, dict)
    else secret_entry
)
headers = {
    "X-Plugin-ID": plugin_id,
    "X-Plugin-Secret": plugin_secret,
}
old_id = int(mapping["<DOC_A19>"])
source = (
    root
    / "datasets"
    / "source_docs"
    / "plugin-a"
    / "A19_langchain_chunking_strategies.md"
)

with httpx.Client(
    base_url="http://127.0.0.1:8000", timeout=180.0, trust_env=False
) as client:
    response = client.delete(f"/documents/{old_id}", headers=headers)
    if response.status_code not in {204, 404}:
        raise RuntimeError(
            f"delete old A19 failed: {response.status_code} {response.text[:500]}"
        )
    with source.open("rb") as stream:
        response = client.post(
            "/documents/upload",
            headers=headers,
            files={"file": (source.name, stream, "text/markdown")},
        )
    if response.status_code != 201:
        raise RuntimeError(
            f"upload A19 failed: {response.status_code} {response.text[:500]}"
        )
    document = response.json()

if document["status"] != "SUCCESS" or document["chunk_count"] <= 0:
    raise RuntimeError(f"unexpected A19 state: {document}")

new_id = int(document["id"])
chunk_count = int(document["chunk_count"])
chunk_ids = set(
    PyMilvusRepositoryImpl(get_settings()).query_page_chunks(new_id)
)
expected = {f"{new_id}_{index}" for index in range(chunk_count)}
if chunk_ids != expected:
    raise RuntimeError(
        f"A19 chunk mismatch: expected={sorted(expected)} actual={sorted(chunk_ids)}"
    )

for key in list(mapping):
    if key.startswith("<CHUNK_A19_"):
        del mapping[key]
mapping["<DOC_A19>"] = new_id
for number in range(1, chunk_count + 1):
    mapping[f"<CHUNK_A19_{number}>"] = f"{new_id}_{number - 1}"

temporary = mapping_path.with_suffix(".json.tmp")
temporary.write_text(
    json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8"
)
temporary.replace(mapping_path)
print(f"A19_old_document={old_id}")
print(f"A19_new_document={new_id}")
print(f"A19_chunk_count={chunk_count}")
