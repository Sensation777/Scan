#!/usr/bin/env python3
"""One-off scanner for Job-Room's public job-search API.

The script searches recent Swiss job advertisements with multilingual terms,
fetches full details, and writes deterministic JSON/CSV outputs under results/.
It uses only the public API used by the Job-Room web frontend.
"""

from __future__ import annotations

import csv
import html
import json
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

BASE_URL = "https://www.job-room.ch"
SEARCH_URL = BASE_URL + "/jobadservice/api/jobAdvertisements/_search"
DETAIL_URL = BASE_URL + "/jobadservice/api/jobAdvertisements/{job_id}"
RESULTS_DIR = Path("results")
ONLINE_SINCE_DAYS = 30
PAGE_SIZE = 100
MAX_PAGES_PER_QUERY = 5
REQUEST_TIMEOUT = 40

# Search title families and content families in English, German, and French.
QUERIES = [
    "machine learning",
    "artificial intelligence",
    "data scientist",
    "research engineer",
    "applied scientist",
    "NLP",
    "natural language processing",
    "information retrieval",
    "search engineer",
    "semantic search",
    "knowledge graph",
    "RAG",
    "retrieval augmented generation",
    "reranking",
    "data engineer",
    "Python engineer",
    "maschinelles Lernen",
    "künstliche Intelligenz",
    "Datenwissenschaft",
    "Forschungsingenieur",
    "Informationsretrieval",
    "semantische Suche",
    "Wissensgraph",
    "intelligence artificielle",
    "apprentissage automatique",
    "science des données",
    "ingénieur de recherche",
    "traitement du langage naturel",
    "recherche d'information",
    "recherche sémantique",
    "graphe de connaissances",
]

RELEVANCE_TERMS = {
    "retrieval": 7,
    "information retrieval": 9,
    "search": 4,
    "semantic search": 8,
    "ranking": 7,
    "reranking": 9,
    "re-ranking": 9,
    "relevance": 6,
    "embedding": 5,
    "vector search": 7,
    "vector database": 5,
    "rag": 6,
    "retrieval-augmented": 8,
    "knowledge graph": 8,
    "graph rag": 9,
    "graphrag": 9,
    "ontology": 5,
    "sparql": 7,
    "natural language": 5,
    "nlp": 5,
    "language model": 4,
    "llm": 3,
    "evaluation": 5,
    "benchmark": 4,
    "information extraction": 6,
    "document understanding": 6,
    "multimodal": 6,
    "machine learning": 4,
    "data pipeline": 4,
    "decision support": 4,
    "wissensgraph": 8,
    "semantische suche": 8,
    "informationsretrieval": 9,
    "traitement du langage naturel": 6,
    "recherche sémantique": 8,
    "graphe de connaissances": 8,
}

NEGATIVE_TITLE_TERMS = [
    "senior", "staff", "principal", "lead ", "head of", "director", "manager",
    "consultant", "sales", "commercial", "medical", "radiology", "clinical",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-CH,en;q=0.9,de;q=0.7,fr;q=0.6",
    "Content-Type": "application/json",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/job-search",
}


def strip_html(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def description_list(job_content: dict[str, Any]) -> list[dict[str, Any]]:
    value = job_content.get("jobDescriptions") or []
    return value if isinstance(value, list) else []


def pick_description(job_content: dict[str, Any]) -> tuple[str, str, str]:
    descs = description_list(job_content)
    for preferred in ("en", "de", "fr", "it"):
        for item in descs:
            if isinstance(item, dict) and str(item.get("languageIsoCode", "")).lower() == preferred:
                return (
                    strip_html(item.get("title")),
                    strip_html(item.get("description")),
                    preferred,
                )
    if descs and isinstance(descs[0], dict):
        first = descs[0]
        return (
            strip_html(first.get("title")),
            strip_html(first.get("description")),
            str(first.get("languageIsoCode") or ""),
        )
    return "", "", ""


def normalize_job(raw: dict[str, Any], matched_queries: list[str]) -> dict[str, Any]:
    wrapper = raw.get("jobAdvertisement") if isinstance(raw, dict) else None
    job = wrapper if isinstance(wrapper, dict) else raw
    content = job.get("jobContent") or {}
    if not isinstance(content, dict):
        content = {}

    title, description, description_language = pick_description(content)
    company = content.get("company") or content.get("employer") or {}
    if not isinstance(company, dict):
        company = {}
    location = content.get("location") or {}
    if not isinstance(location, dict):
        location = {}
    employment = content.get("employment") or {}
    if not isinstance(employment, dict):
        employment = {}
    publication = job.get("publication") or {}
    if not isinstance(publication, dict):
        publication = {}
    apply_channel = content.get("applyChannel") or {}
    if not isinstance(apply_channel, dict):
        apply_channel = {}

    languages = content.get("languageSkills") or []
    if not isinstance(languages, list):
        languages = []

    job_id = str(job.get("id") or job.get("stellennummerEgov") or "")
    external_url = str(content.get("externalUrl") or "").strip()
    form_url = str(apply_channel.get("formUrl") or "").strip()
    email = str(apply_channel.get("emailAddress") or "").strip()
    detail_url = f"{BASE_URL}/job-search/detail/{job_id}" if job_id else ""

    text = f"{title} {description}".lower()
    score = sum(weight for term, weight in RELEVANCE_TERMS.items() if term in text)
    title_lower = title.lower()
    negative_flags = [term.strip() for term in NEGATIVE_TITLE_TERMS if term in title_lower]

    return {
        "id": job_id,
        "stellennummer_egov": job.get("stellennummerEgov"),
        "status": job.get("status"),
        "source_system": job.get("sourceSystem"),
        "title": title,
        "company": str(company.get("name") or "").strip(),
        "city": str(location.get("city") or location.get("communalName") or "").strip(),
        "postal_code": str(location.get("postalCode") or location.get("zipCode") or "").strip(),
        "canton": str(location.get("cantonCode") or "").strip(),
        "country": str(location.get("countryIsoCode") or "CH").strip(),
        "description_language": description_language,
        "language_skills": languages,
        "description": description,
        "publication_start": publication.get("startDate") or job.get("createdTime"),
        "publication_end": publication.get("endDate"),
        "employment_start": employment.get("startDate"),
        "employment_end": employment.get("endDate"),
        "permanent": employment.get("permanent"),
        "immediately": employment.get("immediately"),
        "workload_min": employment.get("workloadPercentageMin"),
        "workload_max": employment.get("workloadPercentageMax"),
        "work_forms": employment.get("workForms") or [],
        "reporting_obligation": bool(job.get("reportingObligation")),
        "company_website": company.get("website"),
        "apply_form_url": form_url,
        "apply_email": email,
        "external_url": external_url,
        "jobroom_detail_url": detail_url,
        "best_application_url": form_url or external_url or (f"mailto:{email}" if email else detail_url),
        "matched_queries": sorted(set(matched_queries)),
        "relevance_score": score,
        "negative_title_flags": negative_flags,
    }


def unpack_search_response(response: requests.Response) -> tuple[list[dict[str, Any]], int | None]:
    payload = response.json()
    total_header = response.headers.get("X-Total-Count")
    total = int(total_header) if total_header and total_header.isdigit() else None

    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)], total
    if isinstance(payload, dict):
        for key in ("content", "jobs", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                if total is None:
                    candidate_total = payload.get("totalElements") or payload.get("total")
                    total = candidate_total if isinstance(candidate_total, int) else None
                return [x for x in value if isinstance(x, dict)], total
    return [], total


def search_query(session: requests.Session, query: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    reported_total: int | None = None
    error = ""

    body = {
        "workloadPercentageMin": 20,
        "workloadPercentageMax": 100,
        "permanent": None,
        "companyName": None,
        "onlineSince": ONLINE_SINCE_DAYS,
        "displayRestricted": False,
        "professionCodes": [],
        "keywords": [query],
        "communalCodes": [],
        "cantonCodes": [],
    }

    for page in range(MAX_PAGES_PER_QUERY):
        try:
            response = session.post(
                SEARCH_URL,
                params={"sort": "date_desc", "_ng": "ZW4=", "page": page, "size": PAGE_SIZE},
                json=body,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            page_rows, total = unpack_search_response(response)
            if reported_total is None:
                reported_total = total
            rows.extend(page_rows)
            if len(page_rows) < PAGE_SIZE:
                break
            time.sleep(0.15)
        except Exception as exc:  # Keep other queries running and record exact failure.
            error = f"{type(exc).__name__}: {exc}"
            break

    return rows, {
        "query": query,
        "reported_total": reported_total,
        "downloaded_rows": len(rows),
        "error": error,
    }


def fetch_detail(session: requests.Session, job_id: str) -> tuple[dict[str, Any] | None, str]:
    try:
        response = session.get(DETAIL_URL.format(job_id=job_id), timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
        return (payload if isinstance(payload, dict) else None), ""
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def write_csv(path: Path, jobs: list[dict[str, Any]]) -> None:
    fields = [
        "relevance_score", "title", "company", "city", "canton", "publication_start",
        "publication_end", "permanent", "workload_min", "workload_max", "status",
        "description_language", "negative_title_flags", "matched_queries",
        "best_application_url", "jobroom_detail_url", "id",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for job in jobs:
            row = {field: job.get(field) for field in fields}
            row["negative_title_flags"] = ",".join(job.get("negative_title_flags") or [])
            row["matched_queries"] = " | ".join(job.get("matched_queries") or [])
            writer.writerow(row)


def main() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update(HEADERS)

    raw_by_id: dict[str, dict[str, Any]] = {}
    matched_by_id: defaultdict[str, set[str]] = defaultdict(set)
    query_reports: list[dict[str, Any]] = []

    for index, query in enumerate(QUERIES, start=1):
        print(f"[{index}/{len(QUERIES)}] Searching: {query}", flush=True)
        rows, report = search_query(session, query)
        query_reports.append(report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
        for item in rows:
            wrapped = item.get("jobAdvertisement") if isinstance(item, dict) else None
            job = wrapped if isinstance(wrapped, dict) else item
            job_id = str(job.get("id") or job.get("stellennummerEgov") or "") if isinstance(job, dict) else ""
            if not job_id:
                continue
            raw_by_id.setdefault(job_id, item)
            matched_by_id[job_id].add(query)
        time.sleep(0.15)

    detailed_jobs: list[dict[str, Any]] = []
    detail_errors: dict[str, str] = {}
    total_unique = len(raw_by_id)
    print(f"Unique search results: {total_unique}", flush=True)

    for index, (job_id, summary) in enumerate(raw_by_id.items(), start=1):
        detail, error = fetch_detail(session, job_id)
        if error:
            detail_errors[job_id] = error
        normalized = normalize_job(detail or summary, sorted(matched_by_id[job_id]))
        detailed_jobs.append(normalized)
        if index % 50 == 0 or index == total_unique:
            print(f"Fetched details: {index}/{total_unique}; errors={len(detail_errors)}", flush=True)
        time.sleep(0.08)

    detailed_jobs.sort(
        key=lambda x: (
            -int(x.get("relevance_score") or 0),
            str(x.get("publication_start") or ""),
            str(x.get("title") or ""),
        ),
        reverse=False,
    )
    # Correct date ordering within identical relevance bands.
    detailed_jobs = sorted(
        detailed_jobs,
        key=lambda x: (int(x.get("relevance_score") or 0), str(x.get("publication_start") or "")),
        reverse=True,
    )

    shortlist = [
        job for job in detailed_jobs
        if int(job.get("relevance_score") or 0) >= 4
        and not job.get("negative_title_flags")
    ]

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "api": {
            "search_url": SEARCH_URL,
            "detail_url_template": DETAIL_URL,
            "online_since_days": ONLINE_SINCE_DAYS,
            "page_size": PAGE_SIZE,
            "max_pages_per_query": MAX_PAGES_PER_QUERY,
        },
        "query_count": len(QUERIES),
        "queries": query_reports,
        "downloaded_rows_before_dedup": sum(int(x["downloaded_rows"]) for x in query_reports),
        "unique_job_count": len(detailed_jobs),
        "detail_error_count": len(detail_errors),
        "shortlist_count": len(shortlist),
        "status_counts": dict(Counter(str(job.get("status") or "UNKNOWN") for job in detailed_jobs)),
        "language_counts": dict(Counter(str(job.get("description_language") or "unknown") for job in detailed_jobs)),
        "detail_errors": detail_errors,
    }

    (RESULTS_DIR / "scan_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (RESULTS_DIR / "jobs_detailed.json").write_text(
        json.dumps(detailed_jobs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (RESULTS_DIR / "shortlist.json").write_text(
        json.dumps(shortlist, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_csv(RESULTS_DIR / "shortlist.csv", shortlist)

    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
