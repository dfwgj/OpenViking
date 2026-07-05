# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
Feishu/Lark cloud document parser for OpenViking.

Compatibility wrapper around FeishuMarkdownConverter + MarkdownParser.
Production server imports use FeishuAccessor instead.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from openviking.parse.base import NodeType, ParseResult, ResourceNode, create_parse_result
from openviking.parse.feishu.converter import (
    FeishuConvertOptions,
    FeishuMarkdownConverter,
    is_allowed_feishu_url,
)
from openviking.parse.parsers.base_parser import BaseParser
from openviking_cli.exceptions import OpenVikingError
from openviking_cli.utils.config.parser_config import FeishuConfig
from openviking_cli.utils.logger import get_logger

logger = get_logger(__name__)


class FeishuParser(BaseParser):
    """Parser for Feishu/Lark cloud documents."""

    _SKIP_ATTRS = FeishuMarkdownConverter._SKIP_ATTRS
    _SPECIAL_BLOCK_HANDLERS = FeishuMarkdownConverter._SPECIAL_BLOCK_HANDLERS
    _TEXT_FORMAT = FeishuMarkdownConverter._TEXT_FORMAT
    _BLOCK_TYPE_TO_ATTR = FeishuMarkdownConverter._BLOCK_TYPE_TO_ATTR
    _KNOWN_CONTENT_ATTRS = FeishuMarkdownConverter._KNOWN_CONTENT_ATTRS
    _DOC_TYPE_HANDLERS = FeishuMarkdownConverter._DOC_TYPE_HANDLERS
    _WIKI_TYPE_MAP = FeishuMarkdownConverter._WIKI_TYPE_MAP

    def __init__(self, config: Optional[FeishuConfig] = None):
        self._converter = FeishuMarkdownConverter(config=config)
        self._markdown_parser = None

    @property
    def supported_extensions(self) -> List[str]:
        return []

    @property
    def _client(self):
        return self._converter._client

    @_client.setter
    def _client(self, value):
        self._converter._client = value

    def _get_config(self) -> FeishuConfig:
        return self._converter._get_config()

    def _get_markdown_parser(self):
        if self._markdown_parser is None:
            from openviking.parse.parsers.markdown import MarkdownParser

            self._markdown_parser = MarkdownParser(config=self._get_config())
        return self._markdown_parser

    def _get_client(self):
        return self._converter._get_client()

    @staticmethod
    def _parse_feishu_url(url: str) -> Tuple[str, str]:
        return FeishuMarkdownConverter.parse_feishu_url(url)

    async def parse(self, source: Union[str, Path], instruction: str = "", **kwargs) -> ParseResult:
        url = str(source)
        start_time = time.time()
        try:
            converted = await asyncio.to_thread(
                self._converter.convert_url,
                url,
                FeishuConvertOptions(),
            )
            md_parser = self._get_markdown_parser()
            result = await md_parser.parse_content(
                converted.markdown,
                source_path=url,
                instruction=instruction,
                **kwargs,
            )
            result.source_format = f"feishu_{converted.doc_type}"
            result.parser_name = "FeishuParser"
            result.parse_time = time.time() - start_time
            result.meta.update(converted.meta)
            if converted.warnings:
                result.warnings = list(result.warnings or []) + converted.warnings
            return result
        except OpenVikingError:
            raise
        except Exception as exc:
            logger.error(f"[FeishuParser] Failed to parse {url}: {exc}")
            return create_parse_result(
                root=ResourceNode(type=NodeType.ROOT),
                source_path=url,
                source_format="feishu",
                parser_name="FeishuParser",
                parse_time=time.time() - start_time,
                warnings=[f"Feishu parse failed: {exc}"],
            )

    async def parse_content(
        self,
        content: str,
        source_path: Optional[str] = None,
        instruction: str = "",
        **kwargs,
    ) -> ParseResult:
        if source_path and is_allowed_feishu_url(source_path):
            return await self.parse(source_path, instruction=instruction, **kwargs)
        raise NotImplementedError("FeishuParser requires a Feishu URL. Use parse() instead.")

    def _resolve_wiki_node(self, token: str) -> Tuple[str, str, Optional[str]]:
        return self._converter._resolve_wiki_node(token, options=FeishuConvertOptions())

    def _parse_docx(self, document_id: str) -> Tuple[str, str]:
        markdown, title, _warnings = self._converter._convert_docx(
            document_id,
            options=FeishuConvertOptions(include_placeholders=False),
        )
        return markdown, title

    def _fetch_all_blocks(self, document_id: str) -> list:
        return self._converter._fetch_all_blocks(document_id, options=FeishuConvertOptions())

    def _detect_block_attr(self, block) -> Optional[str]:
        return self._converter._detect_block_attr(block)

    def _block_to_markdown(
        self,
        block,
        block_map: Dict,
        ordered_counter: Dict[str, int],
        document_id: str = "",
    ) -> Optional[str]:
        return self._converter.block_to_markdown(
            block,
            block_map,
            ordered_counter,
            document_id=document_id,
            options=FeishuConvertOptions(include_placeholders=False),
        )

    @staticmethod
    def _handle_divider(block, block_map: Dict = None, **_) -> str:
        return FeishuMarkdownConverter._handle_divider(block, block_map)

    @staticmethod
    def _handle_image(block, block_map: Dict = None, **_) -> Optional[str]:
        return FeishuMarkdownConverter._handle_image(block, block_map)

    def _extract_block_text(self, block, attr_name: str) -> str:
        return self._converter._extract_block_text(block, attr_name)

    def _extract_text_from_elements(self, elements) -> str:
        return self._converter._extract_text_from_elements(elements)

    @staticmethod
    def _apply_text_style(text: str, style) -> str:
        return FeishuMarkdownConverter.apply_text_style(text, style)

    def _table_block_to_markdown(self, block, block_map: Dict, **_) -> Optional[str]:
        return self._converter._table_block_to_markdown(block, block_map)

    def _extract_cell_text(self, cell_block, block_map: Dict) -> str:
        return self._converter._extract_cell_text(cell_block, block_map)

    def _embedded_sheet_to_markdown(
        self,
        block,
        block_map: Dict = None,
        *,
        document_id: str = "",
        **_,
    ) -> Optional[str]:
        return self._converter._embedded_sheet_to_markdown(
            block,
            block_map,
            document_id=document_id,
            options=FeishuConvertOptions(include_placeholders=False),
        )

    @staticmethod
    def _trim_empty_columns(rows: List[List[str]]) -> List[List[str]]:
        return FeishuMarkdownConverter.trim_empty_columns(rows)

    def _parse_sheets(self, token: str) -> Tuple[str, str]:
        markdown, title, _warnings = self._converter._convert_sheets(
            token,
            options=FeishuConvertOptions(),
        )
        return markdown, title

    def _read_sheet_range(
        self,
        token: str,
        sheet_id: str,
        max_rows: int,
        max_cols: int,
    ) -> List[List[str]]:
        return self._converter._read_sheet_range(
            token,
            sheet_id,
            max_rows,
            max_cols,
            options=FeishuConvertOptions(),
        )

    @staticmethod
    def _col_number_to_letter(n: int) -> str:
        return FeishuMarkdownConverter.col_number_to_letter(n)

    def _parse_bitable(self, app_token: str) -> Tuple[str, str]:
        markdown, title, _warnings = self._converter._convert_bitable(
            app_token,
            options=FeishuConvertOptions(),
        )
        return markdown, title

    @staticmethod
    def _format_bitable_field(value: Any) -> str:
        return FeishuMarkdownConverter.format_bitable_field(value)
