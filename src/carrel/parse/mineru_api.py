"""MinerU cloud parser.

Flow (documented contract):

1. ``POST /api/v4/file-urls/batch`` to obtain pre-signed upload URLs.
2. ``PUT`` the PDF bytes to a URL. Do **not** set ``Content-Type``.
3. Poll ``GET /api/v4/extract-results/batch/{batch_id}`` until every file reaches
   ``done`` or ``failed``.
4. Download ``full_zip_url`` and read ``*content_list.json`` out of the archive.

The batch-upload path is the primary one: the single-file endpoint only accepts
a URL, and arXiv rejects direct remote fetches from outside its network.
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

import httpx
from carrel.runtime.retry import check_response, RetryPolicy, RetryableResponse, with_retry, classify, backoff, retry_after
from carrel.runtime.quota import MinerUQuota
from carrel.runtime.limits import ResourceUnavailable
from carrel.store.paths import atomic_write_json, read_json

from carrel.models import DocModel
from carrel.parse.base import (
    ParseLowQuality,
    ParseRemoteFailed,
    ParseUnavailable,
    blocks_from_content_list,
    page_count,
)
from carrel.parse.quality import gate

ProgressFn = Callable[[str, dict[str, Any]], None]


class MinerUApiParser:
    name = "mineru_api"

    def __init__(
        self,
        *,
        token: str,
        base_url: str = "https://mineru.net/api/v4",
        model_version: str = "vlm",
        poll_interval: float = 3.0,
        timeout_s: int = 900,
        download_max_mb: int = 60,
        runtime=None, data_dir=None, daily_pages_soft_limit=1000, over_limit="warn",
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.model_version = model_version
        self.poll_interval = poll_interval
        self.timeout_s = timeout_s
        self.download_max_mb = download_max_mb
        self.policy = runtime.policy if runtime else RetryPolicy()
        self.runtime = runtime
        self.resource = runtime.pool("mineru") if runtime else None
        self.poll_resource = runtime.pool('mineru:poll') if runtime else None
        self.quota = MinerUQuota(data_dir, daily_pages_soft_limit, over_limit,
                                runtime.stop_event if runtime else None) if data_dir else None

    # ------------------------------------------------------------------ api
    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _headers(self, *, json_body: bool = True) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _request(self, method: str, path: str, *, polling=False, deadline=None, **kw: Any) -> dict[str, Any]:
        if not self.enabled:
            raise ParseUnavailable("MINERU_TOKEN is not configured")
        def attempt():
            timeout = min(60, max(.1, deadline - time.monotonic())) if deadline else 60
            resp = httpx.request(method, f"{self.base_url}{path}", headers=self._headers(), timeout=timeout, **kw)
            check_response(resp)
            body = resp.json()
            if body.get('code') not in (0, None):
                message = str(body.get('msg') or body.get('message') or '')
                import re
                if re.search(r'busy|rate.?limit|too many|稍后|繁忙|限流', message, re.I):
                    raise RetryableResponse('MinerU busy: ' + message[:200])
                error = ParseRemoteFailed(f"MinerU error {body.get('code')}: {message[:200]}")
                error.retryable = False
                raise error
            return body
        try:
            # Polling never consumes an upload slot; the outer loop owns its deadline.
            return with_retry(attempt, RetryPolicy(1) if polling else self.policy,
                              resource=self.poll_resource if polling else self.resource, deadline=deadline)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise ParseUnavailable(f'MinerU denied access ({exc.response.status_code}); check token and service access') from exc
            error = ParseRemoteFailed(f'MinerU HTTP {exc.response.status_code}')
            error.retryable = classify(exc) == 'retry'
            raise error from exc
        except (httpx.HTTPError, RetryableResponse, ResourceUnavailable, ValueError) as exc:
            error = ParseRemoteFailed(f'MinerU request failed: {type(exc).__name__}')
            error.retryable = classify(exc) == 'retry'
            raise error from exc

    # ---------------------------------------------------------------- parse
    def parse(
        self,
        pdf_path: Path,
        *,
        out_dir: Path,
        data_id: str = "",
        progress: ProgressFn | None = None,
    ) -> DocModel:
        pdf_path = Path(pdf_path)
        if not pdf_path.is_file():
            raise ParseUnavailable(f"PDF not found: {pdf_path}")

        size_mb = pdf_path.stat().st_size / (1024 * 1024)
        if size_mb > self.download_max_mb:
            raise ParseUnavailable(
                f"PDF is {size_mb:.1f} MB, above the {self.download_max_mb} MB limit"
            )

        from carrel.ids import sha256_file
        digest = sha256_file(pdf_path)
        checkpoint = out_dir / 'upload.json'
        saved = read_json(checkpoint, {})
        if saved.get('sha256') == digest and saved.get('uploaded') and time.time() - saved.get('at', 0) < 12 * 3600:
            batch_id = saved['batch_id']
        else:
            if self.quota:
                self.quota.reserve(pdf_path, progress)
            _emit(progress, "uploading", {"size_mb": round(size_mb, 2)})
            batch_id, upload_url = self._request_upload_slot(pdf_path.name, data_id or pdf_path.stem)
            self._upload(upload_url, pdf_path)
            atomic_write_json(checkpoint, {'sha256': digest, 'batch_id': batch_id, 'uploaded': True, 'at': time.time()})

        _emit(progress, "parsing", {"batch_id": batch_id})
        try:
            result = self._poll(batch_id, progress=progress)
        except ParseRemoteFailed as exc:
            if getattr(exc, 'remote_terminal', False) or not getattr(exc, 'retryable', False):
                checkpoint.unlink(missing_ok=True)
            raise

        _emit(progress, "downloading", {"zip": result.get("full_zip_url", "")})
        content = self._fetch_content_list(result["full_zip_url"], out_dir)

        blocks = blocks_from_content_list(content)
        pages = page_count(blocks)
        doc = DocModel(
            backend=f"mineru_api_{self.model_version}",
            pages=pages,
            blocks=blocks,
            raw_ref={
                "batch_id": batch_id,
                "task_id": result.get("task_id", ""),
                "zip_path": str(out_dir / "result.zip"),
                "content_list": "content_list.json",
            },
        )
        if not blocks:
            raise ParseLowQuality("MinerU returned no usable content blocks")
        return gate(doc)

    # ------------------------------------------------------------ internals
    def _request_upload_slot(self, filename: str, data_id: str) -> tuple[str, str]:
        body = self._request(
            "POST",
            "/file-urls/batch",
            json={
                "files": [{"name": filename, "data_id": data_id[:128]}],
                "model_version": self.model_version,
                "enable_formula": True,
                "enable_table": True,
            },
        )
        data = body.get("data") or {}
        urls = data.get("file_urls") or []
        batch_id = data.get("batch_id") or ""
        if not urls or not batch_id:
            raise ParseRemoteFailed(f"upload slot response missing fields: {body}")
        return batch_id, urls[0]

    def _upload(self, url: str, pdf_path: Path) -> None:
        def attempt():
            with pdf_path.open('rb') as fh:
                resp = httpx.put(url, content=fh.read(), timeout=300)
            check_response(resp)
        try:
            with_retry(attempt, self.policy, resource=self.resource)
        except Exception as exc:
            error = ParseRemoteFailed(f'MinerU upload failed: {type(exc).__name__}')
            error.retryable = classify(exc) == 'retry'
            raise error from exc

    def _poll(self, batch_id: str, *, progress: ProgressFn | None) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout_s
        last_state = ""
        while time.monotonic() < deadline:
            if self.runtime:
                self.runtime.check_stopped()
            try:
                body = self._request("GET", f"/extract-results/batch/{batch_id}", polling=True, deadline=deadline)
            except ParseRemoteFailed as exc:
                if classify(exc) != 'retry':
                    raise
                _emit(progress, 'poll_retry', {'message': 'MinerU 暂时不可达，正在等待恢复'})
                hinted_delay = retry_after(getattr(exc.__cause__, 'response', None)) or 0
                self._wait(min(max(self.poll_interval, backoff(1, self.policy), hinted_delay),
                               max(0, deadline - time.monotonic())))
                continue
            items = (body.get("data") or {}).get("extract_result") or []
            if items:
                item = items[0]
                state = str(item.get("state") or "")
                if state != last_state:
                    last_state = state
                    _emit(progress, "parsing", {
                        "state": state,
                        "pages": (item.get("extract_progress") or {}).get("extracted_pages"),
                        "total": (item.get("extract_progress") or {}).get("total_pages"),
                    })
                if state == "done":
                    if not item.get("full_zip_url"):
                        raise ParseRemoteFailed("task finished without a result archive")
                    return item
                if state == "failed":
                    message = item.get("err_msg") or "MinerU task failed"
                    error = ParseRemoteFailed(message)
                    error.remote_terminal = True
                    import re
                    error.retryable = bool(re.search(r'busy|rate|timeout|繁忙|限流|超时', message, re.I))
                    raise error
            self._wait(min(self.poll_interval, max(0, deadline - time.monotonic())))
        error = ParseRemoteFailed(f"timed out after {self.timeout_s}s waiting for MinerU (last state: {last_state})")
        error.retryable = True
        raise error

    def _wait(self, seconds):
        if self.runtime:
            self.runtime.stop_event.wait(seconds)
            self.runtime.check_stopped()
        else:
            time.sleep(seconds)

    def _fetch_content_list(self, zip_url: str, out_dir: Path) -> list[dict[str, Any]]:
        out_dir.mkdir(parents=True, exist_ok=True)
        def attempt():
            resp = httpx.get(zip_url, timeout=300)
            check_response(resp)
            if not zipfile.is_zipfile(io.BytesIO(resp.content)):
                raise RetryableResponse('MinerU result is not a valid ZIP')
            return resp.content
        try:
            blob = with_retry(attempt, self.policy, resource=self.resource)
        except Exception as exc:
            error = ParseRemoteFailed(f'MinerU result download failed: {type(exc).__name__}')
            error.retryable = classify(exc) == 'retry'
            raise error from exc
        archive = out_dir / "result.zip"
        archive.write_bytes(blob)

        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                zf.extractall(out_dir / "extracted")
                names = [n for n in zf.namelist() if n.endswith("content_list.json")]
                if not names:
                    raise ParseRemoteFailed(
                        "archive has no content_list.json; files: "
                        + ", ".join(zf.namelist()[:8])
                    )
                payload = json.loads(zf.read(names[0]).decode("utf-8"))
        except zipfile.BadZipFile as exc:
            raise ParseRemoteFailed("result archive is not a valid zip") from exc

        if isinstance(payload, dict):
            for key in ("content_list", "items", "blocks"):
                if isinstance(payload.get(key), list):
                    return payload[key]
            raise ParseRemoteFailed("content_list.json has an unexpected shape")
        if not isinstance(payload, list):
            raise ParseRemoteFailed("content_list.json is not a list")
        return payload


def _emit(progress: ProgressFn | None, stage: str, payload: dict[str, Any]) -> None:
    if progress is not None:
        progress(stage, payload)
