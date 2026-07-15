# Canada Data Map — web app

A small web application for exploring a Vectara corpus built by the `statcan` crawler.

- **Search** all of Statistics Canada's data tables by meaning (semantic search over the
  metadata catalog in your Vectara corpus).
- **Drill in** to any table: pick a member for each dimension (geography, category, …).
- **Fetch the numbers live** from StatCan's Web Data Service (WDS) API — chart + table of the
  latest datapoints. Nothing is downloaded or stored locally.

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
