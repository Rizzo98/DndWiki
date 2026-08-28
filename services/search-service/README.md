# search-service

Full-text search over published wiki content (Meilisearch).

## Responsibilities

- Listens to `wiki.published` / `wiki.updated` / `wiki.archived` and keeps the
  Meilisearch index `wiki_pages` in sync (documents are visibility-filtered
  before indexing: only `published` + `public` pages are searchable by players).
- `GET /api/search?q=&campaign_id=` — typo-tolerant search for the web UI.

## Owns

- Meilisearch index `wiki_pages`

## Local dev

```bash
pip install -e libs/python/dnd_common
pip install -e services/search-service
python -m app.workers.indexer
uvicorn app.main:app --reload --port 8008
```

## TODO

- [ ] Indexer worker in `app/workers/indexer.py`
- [ ] Filtering rules: players only search their own campaign
- [ ] Synonyms (e.g. "warrior" ↔ "fighter") per campaign later
