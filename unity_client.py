from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
import json

import requests

from config import UNITY_API_BASE_URL, UNITY_API_KEY, UNITY_ORG_ID, UNITY_PROJECT_ID


class InvalidArtifactDownloadError(RuntimeError):
    pass


def _auth_header_value(api_key: str) -> str:
    return f"Basic {api_key}"


def unity_headers() -> dict[str, str]:
    if not UNITY_API_KEY:
        raise RuntimeError("UNITY_API_KEY is not set")
    return {
        "Authorization": _auth_header_value(UNITY_API_KEY),
        "Accept": "application/json",
    }


def _normalize_unity_url(path_or_url: str) -> str:
    parsed = urlparse(path_or_url)
    if parsed.scheme and parsed.netloc:
        return path_or_url
    return urljoin(UNITY_API_BASE_URL.rstrip("/") + "/", path_or_url)


def unity_get(path: str, params: dict[str, Any] | None = None) -> requests.Response:
    url = _normalize_unity_url(path)
    response = requests.get(
        url,
        headers=unity_headers(),
        params=params,
        timeout=30,
        allow_redirects=True,
    )
    response.raise_for_status()
    return response


def _unwrap_list_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "buildtargets", "builds", "results", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def list_project_build_targets() -> list[dict[str, Any]]:
    path = f"orgs/{UNITY_ORG_ID}/projects/{UNITY_PROJECT_ID}/buildtargets"
    payload = unity_get(path).json()
    return _unwrap_list_payload(payload)


def list_builds(build_target_id: str) -> list[dict[str, Any]]:
    path = f"orgs/{UNITY_ORG_ID}/projects/{UNITY_PROJECT_ID}/buildtargets/{build_target_id}/builds"
    payload = unity_get(path).json()
    return _unwrap_list_payload(payload)


def get_build(build_target_id: str, build_number: int) -> dict[str, Any]:
    path = f"orgs/{UNITY_ORG_ID}/projects/{UNITY_PROJECT_ID}/buildtargets/{build_target_id}/builds/{build_number}"
    return unity_get(path).json()


def _extract_filename_from_href(href: str) -> str | None:
    parsed = urlparse(href)
    name = Path(parsed.path).name
    return name or None


def resolve_artifact_filenames(build: dict[str, Any]) -> list[str]:
    candidates: list[Any] = []

    for key in ("artifacts", "files", "downloads"):
        value = build.get(key)
        if isinstance(value, list):
            candidates.extend(value)

    project_version = build.get("projectVersion")
    if isinstance(project_version, dict):
        filename = project_version.get("filename")
        if filename:
            candidates.append(filename)

    links = build.get("links")
    if isinstance(links, dict):
        download_primary = links.get("download_primary")
        if isinstance(download_primary, dict):
            href = download_primary.get("href")
            if href:
                candidates.append(href)

        artifacts_link = links.get("artifacts")
        if isinstance(artifacts_link, list):
            candidates.extend(artifacts_link)

    filenames: list[str] = []
    for item in candidates:
        if isinstance(item, str):
            extracted = _extract_filename_from_href(item) if item.startswith("http") else item
            if extracted:
                filenames.append(extracted)
        elif isinstance(item, dict):
            for key in ("filename", "name", "path"):
                if item.get(key):
                    filenames.append(str(item[key]))
                    break

            href = item.get("href") or item.get("url")
            if href:
                extracted = _extract_filename_from_href(str(href))
                if extracted:
                    filenames.append(extracted)

    seen = set()
    unique = []
    for name in filenames:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return unique


def get_primary_download_url(build: dict[str, Any]) -> str | None:
    links = build.get("links")
    if isinstance(links, dict):
        download_primary = links.get("download_primary")
        if isinstance(download_primary, dict):
            href = download_primary.get("href")
            if href:
                return str(href)

    return None


def _looks_like_zip(content: bytes) -> bool:
    return content.startswith(b"PK\x03\x04") or content.startswith(b"PK\x05\x06") or content.startswith(b"PK\x07\x08")


def _write_debug_payload(target_file: Path, response: requests.Response) -> Path:
    debug_file = target_file.with_suffix(target_file.suffix + ".debug.txt")
    preview = response.text[:4000]
    debug_file.write_text(
        "\n".join(
            [
                f"url={response.url}",
                f"status_code={response.status_code}",
                f"content_type={response.headers.get('Content-Type', '')}",
                f"content_length={response.headers.get('Content-Length', '')}",
                "",
                preview,
            ]
        ),
        encoding="utf-8",
    )
    return debug_file


def _extract_signed_download_url(response: requests.Response) -> str | None:
    content_type = response.headers.get("Content-Type", "").lower()
    if "json" not in content_type:
        return None

    text = response.text.strip()
    if not text:
        return None

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None

    if isinstance(payload, str) and payload.startswith(("http://", "https://")):
        return payload

    if isinstance(payload, dict):
        for key in ("url", "href", "downloadUrl", "download_url", "signedUrl", "signed_url"):
            value = payload.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value

    return None


def _download_response(url: str, headers: dict[str, str] | None = None) -> requests.Response:
    response = requests.get(
        url,
        headers=headers,
        timeout=60,
        allow_redirects=True,
    )
    response.raise_for_status()
    return response


def _fetch_artifact_response(path_or_url: str) -> requests.Response:
    initial_response = _download_response(_normalize_unity_url(path_or_url), headers=unity_headers())

    if _looks_like_zip(initial_response.content):
        return initial_response

    signed_url = _extract_signed_download_url(initial_response)
    if not signed_url:
        return initial_response

    final_response = _download_response(signed_url, headers=None)
    return final_response


def download_artifact(
    build_target_id: str,
    build_number: int,
    filename: str,
    download_dir: Path,
    download_url: str | None = None,
) -> Path:
    if download_url:
        response = _fetch_artifact_response(download_url)
    else:
        path = (
            f"orgs/{UNITY_ORG_ID}/projects/{UNITY_PROJECT_ID}/buildtargets/{build_target_id}"
            f"/builds/{build_number}/download/{filename}"
        )
        response = _fetch_artifact_response(path)

    download_dir.mkdir(parents=True, exist_ok=True)
    safe_target = Path(filename).name
    target_file = download_dir / f"{build_target_id}_{build_number}_{safe_target}"

    content_type = response.headers.get("Content-Type", "")
    content_length = response.headers.get("Content-Length", "")

    if not _looks_like_zip(response.content):
        debug_file = _write_debug_payload(target_file, response)
        raise InvalidArtifactDownloadError(
            "Downloaded artifact is not a ZIP archive: "
            f"url={response.url} content_type={content_type!r} content_length={content_length!r} "
            f"debug_file={debug_file}"
        )

    with open(target_file, "wb") as f:
        f.write(response.content)

    return target_file