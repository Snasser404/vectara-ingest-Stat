"""
Canada Data Map - web app for exploring a Vectara corpus built by the statcan crawler.

Search the metadata catalog semantically (Vectara), then fetch the actual numbers
live from Statistics Canada's Web Data Service (WDS) API - nothing is stored locally.

Usage:
    pip install -r webapp/requirements.txt
    python webapp/app.py --config config/statcan.yaml --secrets secrets.toml --profile default
    # then open http://localhost:5000

The Vectara API key must have *query* permissions (a "QueryService & IndexService"
key works; an indexing-only key will return 403 on search).
"""
import argparse
import json
import logging
import os

import requests
import toml
import yaml
from flask import Flask, jsonify, render_template, request

WDS_BASE_URL = "https://www150.statcan.gc.ca/t1/wds/rest"

app = Flask(__name__)
cfg = {"customer_id": "", "corpus_id": "", "api_key": "", "endpoint": "api.vectara.io"}


def load_config(config_path: str, secrets_path: str, profile: str) -> None:
    if config_path and os.path.exists(config_path):
        with open(config_path) as f:
            y = yaml.safe_load(f) or {}
        vectara = y.get("vectara", {})
        cfg["customer_id"] = str(vectara.get("customer_id", cfg["customer_id"]))
        cfg["corpus_id"] = str(vectara.get("corpus_id", cfg["corpus_id"]))
        cfg["endpoint"] = vectara.get("endpoint", cfg["endpoint"])
    if secrets_path and os.path.exists(secrets_path):
        secrets = toml.load(secrets_path)
        section = secrets.get(profile, {})
        cfg["api_key"] = section.get("api_key", cfg["api_key"])
    # environment variables win, and are the fallback when no files are given
    cfg["customer_id"] = os.environ.get("VECTARA_CUSTOMER_ID", cfg["customer_id"])
    cfg["corpus_id"] = os.environ.get("VECTARA_CORPUS_ID", cfg["corpus_id"])
    cfg["api_key"] = os.environ.get("VECTARA_API_KEY", cfg["api_key"])


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search", methods=["POST"])
def search():
    body = request.get_json(force=True) or {}
    query = (body.get("query") or "").strip()
    if not query:
        return jsonify({"error": "empty query"}), 400
    num_results = min(int(body.get("num_results", 10)), 25)

    payload = {
        "query": [{
            "query": query,
            "numResults": num_results,
            "corpusKey": [{
                "customerId": cfg["customer_id"],
                "corpusId": cfg["corpus_id"],
            }],
        }]
    }
    response = requests.post(
        f"https://{cfg['endpoint']}/v1/query",
        headers={
            "Content-Type": "application/json",
            "customer-id": cfg["customer_id"],
            "x-api-key": cfg["api_key"],
        },
        data=json.dumps(payload),
        timeout=60,
    )
    if response.status_code != 200:
        logging.error(f"Vectara query failed: {response.status_code} {response.text}")
        return jsonify({"error": f"Vectara query failed ({response.status_code}). "
                                 "Check that your API key has query permissions."}), 502

    data = response.json()
    response_set = (data.get("responseSet") or [{}])[0]
    documents = response_set.get("document", [])
    results = []
    seen = set()
    for item in response_set.get("response", []):
        doc_index = item.get("documentIndex", 0)
        doc = documents[doc_index] if doc_index < len(documents) else {}
        doc_id = doc.get("id", "")
        if doc_id in seen:
            continue
        seen.add(doc_id)
        meta = {m["name"]: m["value"] for m in doc.get("metadata", []) if "name" in m}
        results.append({
            "id": doc_id,
            "score": item.get("score", 0),
            "snippet": item.get("text", ""),
            "title": meta.get("title", doc_id),
            "productId": meta.get("productId", ""),
            "url": meta.get("url", ""),
            "frequency": meta.get("frequency", ""),
            "subjects": meta.get("subjects", ""),
            "startDate": meta.get("startDate", ""),
            "endDate": meta.get("endDate", ""),
            "source": meta.get("source", ""),
        })
    return jsonify({"results": results})


@app.route("/api/cube/<int:product_id>")
def cube_metadata(product_id: int):
    """Proxy WDS getCubeMetadata (server-side, avoids browser CORS)."""
    response = requests.post(
        f"{WDS_BASE_URL}/getCubeMetadata",
        json=[{"productId": product_id}],
        timeout=60,
    )
    response.raise_for_status()
    results = response.json()
    if not results or results[0].get("status") != "SUCCESS":
        return jsonify({"error": "table not found in WDS"}), 404
    obj = results[0].get("object", {})
    dimensions = []
    for dim in obj.get("dimension", []):
        members = [{"id": m.get("memberId"), "name": m.get("memberNameEn", "")}
                   for m in dim.get("member", [])][:300]
        dimensions.append({"name": dim.get("dimensionNameEn", ""), "members": members})
    return jsonify({
        "productId": obj.get("productId"),
        "title": obj.get("cubeTitleEn", ""),
        "startDate": obj.get("cubeStartDate", ""),
        "endDate": obj.get("cubeEndDate", ""),
        "dimensions": dimensions,
    })


@app.route("/api/data/<int:product_id>")
def cube_data(product_id: int):
    """Fetch the latest N datapoints for one coordinate, live from WDS."""
    coordinate = request.args.get("coordinate", "")
    latest_n = min(int(request.args.get("latestN", 20)), 100)
    # a WDS coordinate is always 10 dot-separated member ids, padded with zeros
    parts = [p for p in coordinate.split(".") if p != ""]
    parts = (parts + ["0"] * 10)[:10]
    response = requests.post(
        f"{WDS_BASE_URL}/getDataFromCubePidCoordAndLatestNPeriods",
        json=[{"productId": product_id, "coordinate": ".".join(parts), "latestN": latest_n}],
        timeout=60,
    )
    response.raise_for_status()
    results = response.json()
    if not results or results[0].get("status") != "SUCCESS":
        return jsonify({"error": "no data for this combination"}), 404
    obj = results[0].get("object", {})
    points = [{"refPer": p.get("refPer", ""), "value": p.get("value")}
              for p in obj.get("vectorDataPoint", [])]
    return jsonify({"productId": product_id, "coordinate": ".".join(parts), "points": points})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Canada Data Map web app")
    parser.add_argument("--config", default="config/statcan.yaml", help="vectara-ingest config yaml")
    parser.add_argument("--secrets", default="secrets.toml", help="secrets.toml path")
    parser.add_argument("--profile", default="default", help="secrets.toml profile")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    load_config(args.config, args.secrets, args.profile)
    if not (cfg["customer_id"] and cfg["corpus_id"] and cfg["api_key"]):
        raise SystemExit("Missing Vectara credentials: provide --config/--secrets files "
                         "or set VECTARA_CUSTOMER_ID, VECTARA_CORPUS_ID, VECTARA_API_KEY")
    app.run(host="127.0.0.1", port=args.port, debug=False)
