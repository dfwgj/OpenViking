# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
Feishu/Lark Accessor.

Fetches Feishu/Lark cloud documents using the lark-oapi SDK and writes local
Markdown for the two-layer import pipeline.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union
from urllib.parse import urlparse

from openviking.parse.feishu.converter import FeishuConvertOptions, FeishuMarkdownConverter
from openviking.utils.feishu_errors import build_feishu_error_details
from openviking.utils.feishu_naming import feishu_document_names
from openviking_cli.utils.logger import get_logger

from .base import DataAccessor, LocalResource, SourceType

logger = get_logger(__name__)

_FEISHU_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(feishu://image/([^)]+)\)")


@dataclass
class FeishuDocument:
    """Result from fetching a Feishu document."""

    doc_type: str
    token: str
    markdown_content: str
    title: str
    meta: Dict[str, Any]
    warnings: list[str] | None = None

    def __post_init__(self) -> None:
        if self.warnings is None:
            self.warnings = []


class FeishuAccessor(DataAccessor):
    """Accessor for Feishu/Lark cloud documents."""

    PRIORITY = 100

    def __init__(self):
        self._converter = FeishuMarkdownConverter()

    @property
    def priority(self) -> int:
        return self.PRIORITY

    def can_handle(self, source: Union[str, Path], **kwargs) -> bool:
        source_str = str(source)
        if not source_str.startswith(("http://", "https://")):
            return False
        return self._is_feishu_url(source_str)

    async def access(self, source: Union[str, Path], **kwargs) -> LocalResource:
        source_str = str(source)
        feishu_access_token = kwargs.get("feishu_access_token")

        try:
            doc = await self._fetch_document(
                source_str,
                feishu_access_token=feishu_access_token,
            )

            markdown_content, downloaded_images = self._resolve_image_refs(
                doc.markdown_content,
                feishu_access_token=feishu_access_token,
            )

            names = feishu_document_names(doc.title)

            meta = {
                "feishu_doc_type": doc.doc_type,
                "feishu_token": doc.token,
                "feishu_title": names.title,
                "original_filename": names.title,
                "feishu_resource_segment": names.folder_segment,
                "feishu_markdown_stem": names.markdown_stem,
                **doc.meta,
            }
            if doc.warnings:
                meta["warnings"] = doc.warnings

            if downloaded_images:
                temp_dir = Path(tempfile.mkdtemp(prefix="ov_feishu_"))
                markdown_path = temp_dir / "document.md"
                markdown_path.write_text(markdown_content, encoding="utf-8")
                for rel_path, image_bytes in downloaded_images.items():
                    image_path = temp_dir / rel_path
                    image_path.parent.mkdir(parents=True, exist_ok=True)
                    image_path.write_bytes(image_bytes)
                meta["_cleanup_path"] = str(temp_dir)
                local_path = markdown_path
            else:
                temp_file = tempfile.NamedTemporaryFile(
                    mode="w",
                    suffix=".md",
                    prefix="ov_feishu_",
                    delete=False,
                    encoding="utf-8",
                )
                temp_file.write(markdown_content)
                temp_file.close()
                local_path = Path(temp_file.name)

            return LocalResource(
                path=local_path,
                source_type=SourceType.FEISHU,
                original_source=source_str,
                meta=meta,
                is_temporary=True,
            )
        except Exception as exc:
            logger.error(f"[FeishuAccessor] Failed to access {source}: {exc}", exc_info=True)
            raise

    async def _fetch_document(
        self,
        url: str,
        *,
        feishu_access_token: Optional[str] = None,
    ) -> FeishuDocument:
        import asyncio

        options = FeishuConvertOptions(feishu_access_token=feishu_access_token)
        converted = await asyncio.to_thread(self._converter.convert_url, url, options)
        return FeishuDocument(
            doc_type=converted.doc_type,
            token=converted.token,
            markdown_content=converted.markdown,
            title=converted.title,
            meta=converted.meta,
            warnings=converted.warnings,
        )

    @staticmethod
    def _is_feishu_url(url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        path = parsed.path
        is_feishu_domain = any(
            host == allowed_host or host.endswith(f".{allowed_host}")
            for allowed_host in ("feishu.cn", "larksuite.com", "larkoffice.com")
        )
        has_doc_path = any(
            path == f"/{doc_type}" or path.startswith(f"/{doc_type}/")
            for doc_type in ("docx", "wiki", "sheets", "base")
        )
        return is_feishu_domain and has_doc_path

    @staticmethod
    def _image_filename(file_token: str) -> str:
        safe_token = re.sub(r"[^A-Za-z0-9_.-]+", "_", file_token).strip("._")
        return f"{safe_token or 'image'}.png"

    def _download_image(
        self,
        file_token: str,
        *,
        feishu_access_token: Optional[str] = None,
    ) -> Optional[bytes]:
        import lark_oapi as lark

        client = self._converter._get_client(use_user_token=bool(feishu_access_token))
        raw_req = (
            lark.BaseRequest.builder()
            .http_method(lark.HttpMethod.GET)
            .uri(f"/open-apis/drive/v1/medias/{file_token}/download")
            .token_types({lark.AccessTokenType.TENANT})
            .build()
        )
        option = FeishuMarkdownConverter._user_request_option(feishu_access_token)

        try:
            raw_resp = client.request(raw_req) if option is None else client.request(raw_req, option)
        except Exception as exc:
            logger.warning("[FeishuAccessor] Error downloading image %s: %s", file_token, exc)
            return None

        if not raw_resp.success():
            raw = getattr(raw_resp, "raw", None)
            http_status = getattr(raw, "status_code", None)
            details = build_feishu_error_details(
                response=raw_resp,
                operation=f"download image {file_token}",
                resource=file_token,
                using_user_token=bool(feishu_access_token),
            )
            detail = details.get("hint") or getattr(raw_resp, "msg", "") or f"HTTP {http_status}"
            if http_status == 403 and "Missing Feishu API scopes" not in detail:
                detail = f"{detail} (may require scope docs:document.media:download)"
            logger.warning(
                "[FeishuAccessor] Failed to download image %s: code=%s, http=%s, msg=%s, hint=%s",
                file_token,
                details.get("feishu_code"),
                http_status,
                getattr(raw_resp, "msg", None),
                detail,
            )
            return None

        raw = getattr(raw_resp, "raw", None)
        content = getattr(raw, "content", None)
        if not content:
            logger.warning("[FeishuAccessor] Empty image response for %s", file_token)
            return None
        return content

    def _resolve_image_refs(
        self,
        markdown: str,
        *,
        feishu_access_token: Optional[str] = None,
    ) -> Tuple[str, Dict[str, bytes]]:
        config = self._converter._get_config()
        if not getattr(config, "download_images", True):
            return markdown, {}

        matches = list(_FEISHU_IMAGE_RE.finditer(markdown))
        if not matches:
            return markdown, {}

        token_to_rel_path: Dict[str, str] = {}
        downloaded_images: Dict[str, bytes] = {}
        for match in matches:
            file_token = match.group(2)
            if file_token in token_to_rel_path:
                continue
            image_bytes = self._download_image(
                file_token,
                feishu_access_token=feishu_access_token,
            )
            if image_bytes is None:
                continue
            rel_path = f"images/{self._image_filename(file_token)}"
            token_to_rel_path[file_token] = rel_path
            downloaded_images[rel_path] = image_bytes

        if not downloaded_images:
            return markdown, {}

        def _replace(match: re.Match[str]) -> str:
            alt_text = match.group(1)
            file_token = match.group(2)
            rel_path = token_to_rel_path.get(file_token)
            if not rel_path:
                return match.group(0)
            return f"![{alt_text}]({rel_path})"

        return _FEISHU_IMAGE_RE.sub(_replace, markdown), downloaded_images
