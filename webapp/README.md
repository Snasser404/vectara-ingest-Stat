# Canada Data Map — web app

Ask anything about Canada's official statistics and get a grounded, cited answer.

- **Ask a question** ("how has unemployment changed across provinces?") — the app retrieves
  the most relevant content from your Vectara corpus and uses Vectara's generative
  summarizer to produce an answer with `[n]` citations you can click.
- **Browse tables** — semantic search over the full StatCan catalog.
- **Drill in** to any cited table: pick a member per dimension (geography, category, …) and
  **fetch the numbers live** from StatCan's Web Data Service (WDS) API — chart + table of the
  latest datapoints. Nothing is downloaded or stored locally.

Answer quality depends on what the crawler ingested: enable `index_data_snapshots` (real
numbers) and `index_daily` (StatCan's own analysis articles) in `config/statcan.yaml` for
genuinely insightful answers rather than catalog descriptions.

## Prerequisites

1. Run the `statcan` crawler at least once so your corpus contains the catalog:

   ```bash
   sh run.sh config/statcan.yaml default
   ```

2. A Vectara API key with **query** permissions (an indexing-only key returns 403 on search).

## Run it

```bash
pip install -r webapp/requirements.txt
python webapp/app.py --config config/statcan.yaml --secrets secrets.toml --profile default
```

Then open http://localhost:5000.

Credentials can also come from environment variables instead of files:
`VECTARA_CUSTOMER_ID`, `VECTARA_CORPUS_ID`, `VECTARA_API_KEY`.

## How it works

```
browser ──> Flask (webapp/app.py)
              ├── /api/search          -> Vectara query API   (the "map")
              ├── /api/cube/<pid>      -> WDS getCubeMetadata (table structure)
              └── /api/data/<pid>      -> WDS getDataFromCubePidCoordAndLatestNPeriods (live numbers)
```

The WDS calls are proxied server-side to avoid browser CORS restrictions.
