import logging
import json
import time
from datetime import datetime, timedelta
from time import mktime
from typing import Any, Dict, List

from omegaconf import OmegaConf

from core.crawler import Crawler
from core.utils import create_session_with_retries

WDS_BASE_URL = "https://www150.statcan.gc.ca/t1/wds/rest"

# Crawler for Statistics Canada (statcan.gc.ca).
#
# Instead of downloading the (very large) data tables themselves, this crawler indexes
# the metadata "map" of every StatCan data table (cube) using the official
# Web Data Service (WDS) API: title, subjects, coverage period, frequency and
# dimensions. The result is a semantically searchable catalog of all Canadian
# official statistics that takes almost no storage; the actual numbers can be
# fetched on demand from the WDS API (see webapp/ for a UI that does exactly that).
#
# WDS API reference: https://www.statcan.gc.ca/en/developers/wds/user-guide
class StatcanCrawler(Crawler):

    def __init__(self, cfg: OmegaConf, endpoint: str, customer_id: str, corpus_id: int, api_key: str) -> None:
        super().__init__(cfg, endpoint, customer_id, corpus_id, api_key)
        c = self.cfg.statcan_crawler
        self.include_archived = c.get("include_archived", False)
        self.subject_filter = [str(s).lower() for s in c.get("subject_filter", [])]
        self.max_cubes = int(c.get("max_cubes", 0))
        self.fetch_dimensions = c.get("fetch_dimensions", True)
        self.index_data_snapshots = c.get("index_data_snapshots", False)
        self.snapshot_members = int(c.get("snapshot_members", 13))
        self.snapshot_periods = int(c.get("snapshot_periods", 8))
        self.metadata_batch_size = int(c.get("metadata_batch_size", 10))
        self.delay = 1.0 / float(c.get("num_per_second", 5))
        self.index_daily = c.get("index_daily", False)
        self.daily_rss_pages = c.get("daily_rss_pages",
                                     ["https://www150.statcan.gc.ca/n1/dai-quo/ssi/homepage/dq-rss-eng.xml"])
        self.daily_days_past = int(c.get("daily_days_past", 30))
        self.session = create_session_with_retries()

    def _wds_get(self, path: str) -> Any:
        response = self.session.get(f"{WDS_BASE_URL}/{path}", timeout=120)
        response.raise_for_status()
        return response.json()

    def _wds_post(self, path: str, payload: List[Dict[str, Any]]) -> Any:
        response = self.session.post(f"{WDS_BASE_URL}/{path}", json=payload, timeout=120)
        response.raise_for_status()
        return response.json()

    def _get_code_sets(self) -> Dict[str, Dict[str, str]]:
        """Fetch human-readable names for the numeric subject/survey/frequency codes."""
        code_sets: Dict[str, Dict[str, str]] = {"subject": {}, "survey": {}, "frequency": {}}
        try:
            data = self._wds_get("getCodeSets")
            obj = data.get("object", {}) if isinstance(data, dict) else {}
            for item in obj.get("subject", []):
                code_sets["subject"][str(item.get("subjectCode"))] = item.get("subjectEn", "")
            for item in obj.get("survey", []):
                code_sets["survey"][str(item.get("surveyCode"))] = item.get("surveyEn", "")
            for item in obj.get("frequency", []):
                code_sets["frequency"][str(item.get("frequencyCode"))] = item.get("frequencyDescEn", "")
        except Exception as e:
            logging.warning(f"Could not fetch WDS code sets ({e}); numeric codes will be used as-is")
        return code_sets

    def _is_archived(self, cube: Dict[str, Any]) -> bool:
        status_text = str(cube.get("archiveStatusEn", "")).upper()
        if status_text:
            return "ARCHIVED" in status_text
        return str(cube.get("archived", "")) == "1"   # WDS: "1" = archived, "2" = current

    def _get_dimensions(self, product_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
        """Fetch dimension names (and a sample of members) for a batch of cubes."""
        dimensions: Dict[int, List[Dict[str, Any]]] = {}
        try:
            results = self._wds_post("getCubeMetadata", [{"productId": pid} for pid in product_ids])
            for result in results:
                if result.get("status") != "SUCCESS":
                    continue
                obj = result.get("object", {})
                pid = int(obj.get("productId", 0))
                dims = []
                for dim in obj.get("dimension", []):
                    members = [(m.get("memberId"), m.get("memberNameEn", "")) for m in dim.get("member", [])]
                    dims.append({
                        "name": dim.get("dimensionNameEn", ""),
                        "num_members": len(members),
                        "members_sample": [name for _, name in members if name][:25],
                        "members": members[:max(self.snapshot_members, 1)],
                        "first_member_id": members[0][0] if members else None,
                    })
                if pid:
                    dimensions[pid] = dims
        except Exception as e:
            logging.warning(f"Error fetching cube metadata for batch starting at {product_ids[:1]}: {e}")
        time.sleep(self.delay)
        return dimensions

    def _get_snapshot_text(self, pid: int, dims: List[Dict[str, Any]]) -> str:
        """
        Fetch the latest reported values for one cube so real numbers are embedded
        alongside the metadata. We take the members of the first dimension (usually
        Geography, so e.g. one series per province) crossed with the first member of
        every other dimension, and pull the last few periods for each.
        """
        if not dims or not dims[0].get("members"):
            return ""
        base = [str(d.get("first_member_id") or 1) for d in dims]
        base = (base + ["0"] * 10)[:10]
        requests_payload = []
        member_names = {}
        for member_id, member_name in dims[0]["members"][:self.snapshot_members]:
            coordinate = [str(member_id)] + base[1:]
            coordinate_str = ".".join(coordinate)
            requests_payload.append({"productId": pid, "coordinate": coordinate_str,
                                     "latestN": self.snapshot_periods})
            member_names[coordinate_str] = member_name
        try:
            results = self._wds_post("getDataFromCubePidCoordAndLatestNPeriods", requests_payload)
        except Exception as e:
            logging.info(f"No data snapshot for cube {pid}: {e}")
            return ""
        time.sleep(self.delay)

        lines = []
        for result in results if isinstance(results, list) else []:
            if result.get("status") != "SUCCESS":
                continue
            obj = result.get("object", {})
            coordinate_str = str(obj.get("coordinate", ""))
            points = [p for p in obj.get("vectorDataPoint", []) if p.get("value") is not None]
            if not points:
                continue
            series = ", ".join(f"{p.get('refPer', '')}: {p.get('value')}" for p in points)
            name = member_names.get(coordinate_str, coordinate_str)
            lines.append(f"{name} — {series}.")
        if not lines:
            return ""
        return f"Latest reported values by {dims[0]['name']} (as of crawl date): " + " ".join(lines)

    def _cube_to_document(self, cube: Dict[str, Any], code_sets: Dict[str, Dict[str, str]],
                          dims: List[Dict[str, Any]], snapshot_text: str = "") -> Dict[str, Any]:
        pid = str(cube.get("productId", ""))
        title = cube.get("cubeTitleEn", "") or f"StatCan table {pid}"
        subjects = [code_sets["subject"].get(str(s), str(s)) for s in cube.get("subjectCode", []) or []]
        frequency = code_sets["frequency"].get(str(cube.get("frequencyCode", "")), str(cube.get("frequencyCode", "")))
        start_date = cube.get("cubeStartDate", "")
        end_date = cube.get("cubeEndDate", "")
        table_url = f"https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid={pid}01"

        overview = f"{title}."
        if subjects:
            overview += f" Subjects: {', '.join(subjects)}."
        if frequency:
            overview += f" Frequency: {frequency}."
        if start_date or end_date:
            overview += f" Data coverage: {start_date} to {end_date}."
        overview += f" Statistics Canada data table {pid}."

        sections = [{"title": "overview", "text": overview}]
        for dim in dims:
            text = f"Dimension: {dim['name']} ({dim['num_members']} members)."
            if dim["members_sample"]:
                text += " Includes: " + ", ".join(dim["members_sample"]) + "."
            sections.append({"title": f"dimension: {dim['name']}", "text": text})
        if snapshot_text:
            sections.append({"title": "latest values", "text": snapshot_text})

        metadata = {
            "source": "statcan",
            "url": table_url,
            "title": title,
            "productId": pid,
            "cansimId": cube.get("cansimId", ""),
            "startDate": start_date,
            "endDate": end_date,
            "frequency": frequency,
            "subjects": ", ".join(subjects),
            "archived": self._is_archived(cube),
        }
        return {
            "documentId": f"statcan-{pid}",
            "title": title,
            "metadataJson": json.dumps(metadata),
            "section": sections,
        }

    def _crawl_cubes(self) -> None:
        code_sets = self._get_code_sets()
        cubes = self._wds_get("getAllCubesListLite")
        logging.info(f"StatCan WDS reports {len(cubes)} data tables (cubes)")

        if not self.include_archived:
            cubes = [c for c in cubes if not self._is_archived(c)]
        if self.subject_filter:
            def matches(cube: Dict[str, Any]) -> bool:
                subjects = [code_sets["subject"].get(str(s), "") for s in cube.get("subjectCode", []) or []]
                haystack = (cube.get("cubeTitleEn", "") + " " + " ".join(subjects)).lower()
                return any(term in haystack for term in self.subject_filter)
            cubes = [c for c in cubes if matches(c)]
        if self.max_cubes > 0:
            cubes = cubes[:self.max_cubes]
        logging.info(f"Indexing metadata for {len(cubes)} cubes "
                     f"(include_archived={bool(self.include_archived)}, subject_filter={self.subject_filter})")

        count = 0
        for batch_start in range(0, len(cubes), self.metadata_batch_size):
            batch = cubes[batch_start:batch_start + self.metadata_batch_size]
            dims_by_pid: Dict[int, List[Dict[str, Any]]] = {}
            if self.fetch_dimensions:
                pids = [int(c.get("productId", 0)) for c in batch if c.get("productId")]
                dims_by_pid = self._get_dimensions(pids)
            for cube in batch:
                pid = int(cube.get("productId", 0))
                dims = dims_by_pid.get(pid, [])
                snapshot_text = ""
                if self.index_data_snapshots and dims:
                    snapshot_text = self._get_snapshot_text(pid, dims)
                document = self._cube_to_document(cube, code_sets, dims, snapshot_text)
                try:
                    if self.indexer.index_document(document):
                        count += 1
                    else:
                        logging.info(f"Error indexing cube {pid}")
                except Exception as e:
                    logging.info(f"Error during indexing of cube {pid}: {e}")
            if count and count % 500 < self.metadata_batch_size:
                logging.info(f"Indexed {count} cube metadata documents so far")
        logging.info(f"Done: indexed {count} of {len(cubes)} cube metadata documents")

    def _crawl_daily(self) -> None:
        """Index recent articles from 'The Daily' (StatCan's official release bulletin) via RSS."""
        import feedparser
        today = datetime.now().replace(microsecond=0)
        days_ago = today - timedelta(days=self.daily_days_past)
        urls = []
        for rss_page in self.daily_rss_pages:
            feed = feedparser.parse(rss_page)
            for entry in feed.entries:
                if "published_parsed" in entry:
                    entry_date = datetime.fromtimestamp(mktime(entry.published_parsed))
                    if entry_date < days_ago or entry_date > today:
                        continue
                urls.append((entry.link, entry.title))
        logging.info(f"Found {len(urls)} articles from The Daily in the last {self.daily_days_past} days")

        crawled = set()
        for url, title in urls:
            if url in crawled:
                continue
            metadata = {"source": "statcan-daily", "url": url, "title": title, "crawl_date": str(today)}
            try:
                if self.indexer.index_url(url, metadata=metadata):
                    crawled.add(url)
                    logging.info(f"Successfully indexed {url}")
                else:
                    logging.info(f"Error indexing {url}")
            except Exception as e:
                logging.info(f"Error while indexing {url}: {e}")
            time.sleep(self.delay)

    def crawl(self) -> None:
        self._crawl_cubes()
        if self.index_daily:
            self._crawl_daily()
