# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Convert Feishu/Lark cloud documents to Markdown."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from openviking.parse.base import format_table_to_markdown
from openviking.utils.feishu_errors import raise_from_lark_response
from openviking_cli.utils.config.parser_config import FeishuConfig
from openviking_cli.utils.logger import get_logger

logger = get_logger(__name__)

_ALLOWED_FEISHU_HOSTS = ("feishu.cn", "larksuite.com", "larkoffice.com")


def _getattr_safe(obj, key: str, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def is_allowed_feishu_url(source_path: str) -> bool:
    parsed = urlparse(source_path)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    return any(
        hostname == allowed_host or hostname.endswith(f".{allowed_host}")
        for allowed_host in _ALLOWED_FEISHU_HOSTS
    )


@dataclass
class FeishuConvertOptions:
    feishu_access_token: Optional[str] = None
    include_placeholders: bool = True
    max_rows_per_sheet: int = 1000
    max_records_per_table: int = 1000


@dataclass
class FeishuConvertedDocument:
    doc_type: str
    token: str
    title: str
    markdown: str
    meta: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


class FeishuMarkdownConverter:
    """Fetch Feishu documents via lark-oapi and convert them to Markdown."""

    _SKIP_ATTRS = {"page", "table_cell", "quote_container", "grid", "grid_column"}

    _SPECIAL_BLOCK_HANDLERS = {
        "divider": "_handle_divider",
        "image": "_handle_image",
        "table": "_table_block_to_markdown",
        "sheet": "_embedded_sheet_to_markdown",
        "sub_page_list": "_sub_page_list_to_markdown",
    }

    _TEXT_FORMAT = {
        "bullet": "- {text}",
        "quote": "> {text}",
    }

    _BLOCK_TYPE_TO_ATTR = {
        1: "page",
        2: "text",
        3: "heading1",
        4: "heading2",
        5: "heading3",
        6: "heading4",
        7: "heading5",
        8: "heading6",
        9: "heading7",
        10: "heading8",
        11: "heading9",
        12: "bullet",
        13: "ordered",
        14: "code",
        15: "quote",
        17: "todo",
        18: "bitable",
        19: "callout",
        22: "divider",
        24: "file",
        27: "image",
        30: "sheet",
        31: "table",
        32: "table_cell",
        34: "quote_container",
    }

    _KNOWN_CONTENT_ATTRS = frozenset(
        {
            "page",
            "text",
            "heading1",
            "heading2",
            "heading3",
            "heading4",
            "heading5",
            "heading6",
            "heading7",
            "heading8",
            "heading9",
            "bullet",
            "ordered",
            "code",
            "quote",
            "todo",
            "callout",
            "divider",
            "image",
            "table",
            "table_cell",
            "quote_container",
            "sheet",
            "file",
            "bitable",
            "equation",
            "task",
            "grid",
            "grid_column",
            "iframe",
            "board",
            "chat_card",
            "diagram",
            "agenda",
            "agenda_item",
            "agenda_item_content",
            "agenda_item_title",
            "ai_template",
            "isv",
            "jira_issue",
            "link_preview",
            "meeting_notes_qa",
            "mindnote",
            "okr",
            "okr_key_result",
            "okr_objective",
            "okr_progress",
            "project",
            "reference_base",
            "reference_synced",
            "source_synced",
            "sub_page_list",
            "undefined",
            "view",
            "wiki_catalog",
        }
    )

    _PLACEHOLDER_ONLY_ATTRS = frozenset({"reference_synced", "source_synced", "bitable"})

    _DOC_TYPE_HANDLERS = {
        "docx": "_convert_docx",
        "sheets": "_convert_sheets",
        "base": "_convert_bitable",
    }

    _WIKI_TYPE_MAP = {"doc": "docx", "sheet": "sheets", "bitable": "base"}

    def __init__(self, config: Optional[FeishuConfig] = None):
        self._config = config
        self._client = None
        self._user_token_client = None
        self._active_warnings: List[str] = []
        self._active_wiki_context: Optional[Dict[str, str]] = None

    def _get_config(self) -> FeishuConfig:
        if self._config is None:
            from openviking_cli.utils.config import get_openviking_config

            self._config = get_openviking_config().feishu
        return self._config

    def _using_user_token(self, options: FeishuConvertOptions) -> bool:
        return bool(options.feishu_access_token)

    def _get_client(self, *, use_user_token: bool = False):
        cache_attr = "_user_token_client" if use_user_token else "_client"
        client = getattr(self, cache_attr)
        if client is None:
            try:
                import lark_oapi as lark
            except ImportError as exc:
                raise ImportError(
                    "lark-oapi is required for Feishu document parsing. "
                    "Install it with: pip install 'openviking[bot-feishu]'"
                ) from exc
            config = self._get_config()
            app_id = config.app_id or os.getenv("FEISHU_APP_ID", "")
            app_secret = config.app_secret or os.getenv("FEISHU_APP_SECRET", "")
            if (not app_id or not app_secret) and not use_user_token:
                raise ValueError(
                    "Feishu credentials not configured. Set FEISHU_APP_ID and "
                    "FEISHU_APP_SECRET environment variables, or configure in ov.conf."
                )
            domain = config.domain or "https://open.feishu.cn"
            builder = lark.Client.builder().domain(domain)
            if app_id and app_secret:
                builder = builder.app_id(app_id).app_secret(app_secret)
            if use_user_token:
                builder = builder.enable_set_token(True)
            client = builder.build()
            setattr(self, cache_attr, client)
        return client

    @staticmethod
    def _user_request_option(feishu_access_token: Optional[str]):
        if not feishu_access_token:
            return None
        from lark_oapi.core.model import RequestOption

        return RequestOption.builder().user_access_token(feishu_access_token).build()

    @staticmethod
    def parse_feishu_url(url: str) -> Tuple[str, str]:
        if not is_allowed_feishu_url(url):
            raise ValueError(f"Feishu host not allowed: {url}")
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.split("/") if p]
        if len(path_parts) < 2:
            raise ValueError(f"Cannot parse Feishu URL: {url}")
        return path_parts[0], path_parts[1]

    def convert_url(self, url: str, options: Optional[FeishuConvertOptions] = None) -> FeishuConvertedDocument:
        options = options or FeishuConvertOptions()
        config = self._get_config()
        if options.max_rows_per_sheet == 1000:
            options.max_rows_per_sheet = config.max_rows_per_sheet
        if options.max_records_per_table == 1000:
            options.max_records_per_table = config.max_records_per_table

        doc_type, token = self.parse_feishu_url(url)
        query = parse_qs(urlparse(url).query)
        meta: Dict[str, Any] = {"original_url": url}
        wiki_title: Optional[str] = None

        if doc_type == "wiki":
            real_type, real_token, wiki_title, wiki_context = self._resolve_wiki_node(
                token,
                options=options,
            )
            doc_type, token = real_type, real_token
            meta["wiki_resolved"] = True
            if wiki_context is not None:
                wiki_context["host"] = urlparse(url).hostname or "www.feishu.cn"
            self._active_wiki_context = wiki_context

        handler_name = self._DOC_TYPE_HANDLERS.get(doc_type)
        if not handler_name:
            raise ValueError(
                f"Unsupported Feishu document type: {doc_type}. "
                f"Supported: {list(self._DOC_TYPE_HANDLERS.keys())}"
            )

        if doc_type == "base":
            table_id = (query.get("table") or [None])[0]
            view_id = (query.get("view") or [None])[0]
            markdown, title, warnings = self._convert_bitable(
                token,
                options=options,
                table_id=table_id,
                view_id=view_id,
            )
            if table_id:
                meta["feishu_table_id"] = table_id
            if view_id:
                meta["feishu_view_id"] = view_id
        else:
            markdown, title, warnings = getattr(self, handler_name)(token, options=options)
        if wiki_title and (not title or title == "Untitled"):
            title = wiki_title

        meta["feishu_doc_type"] = doc_type
        meta["feishu_token"] = token
        try:
            return FeishuConvertedDocument(
                doc_type=doc_type,
                token=token,
                title=title,
                markdown=markdown,
                meta=meta,
                warnings=warnings,
            )
        finally:
            self._active_wiki_context = None

    def _resolve_wiki_node(
        self,
        token: str,
        *,
        options: FeishuConvertOptions,
    ) -> Tuple[str, str, Optional[str], Optional[Dict[str, str]]]:
        from lark_oapi.api.wiki.v2 import GetNodeSpaceRequest

        client = self._get_client(use_user_token=self._using_user_token(options))
        request = GetNodeSpaceRequest.builder().token(token).build()
        option = self._user_request_option(options.feishu_access_token)
        if option is None:
            response = client.wiki.v2.space.get_node(request)
        else:
            response = client.wiki.v2.space.get_node(request, option)
        if not response.success():
            raise_from_lark_response(
                response,
                operation=f"resolve wiki node {token}",
                resource=token,
                using_user_token=self._using_user_token(options),
            )
        node = response.data.node
        obj_type = node.obj_type or ""
        obj_token = node.obj_token or ""
        doc_type = self._WIKI_TYPE_MAP.get(obj_type, obj_type)
        wiki_context = {
            "space_id": node.space_id or "",
            "node_token": node.node_token or token,
        }
        return doc_type, obj_token, node.title, wiki_context

    def _convert_docx(
        self,
        document_id: str,
        *,
        options: FeishuConvertOptions,
    ) -> Tuple[str, str, List[str]]:
        self._active_warnings = []
        blocks = self._fetch_all_blocks(document_id, options=options)
        if not blocks:
            return "", "Untitled", list(self._active_warnings)

        block_map = {b.block_id: b for b in blocks}
        doc_title = "Untitled"
        for block in blocks:
            if block.page is not None and block.page.elements:
                doc_title = self._extract_text_from_elements(block.page.elements)
                break

        markdown_lines: List[str] = []
        ordered_counter: Dict[str, int] = {}
        for block in blocks:
            if block.page is not None:
                continue
            line = self.block_to_markdown(
                block,
                block_map,
                ordered_counter,
                document_id=document_id,
                options=options,
            )
            if line is not None:
                markdown_lines.append(line)

        markdown = "\n\n".join(markdown_lines)
        if doc_title and doc_title != "Untitled":
            markdown = f"# {doc_title}\n\n{markdown}"
        return markdown, doc_title, list(self._active_warnings)

    def _fetch_all_blocks(
        self,
        document_id: str,
        *,
        options: FeishuConvertOptions,
    ) -> list:
        from lark_oapi.api.docx.v1 import ListDocumentBlockRequest

        client = self._get_client(use_user_token=self._using_user_token(options))
        all_blocks = []
        page_token = None

        while True:
            builder = (
                ListDocumentBlockRequest.builder()
                .document_id(document_id)
                .page_size(500)
                .document_revision_id(-1)
            )
            if page_token:
                builder = builder.page_token(page_token)
            request = builder.build()
            option = self._user_request_option(options.feishu_access_token)
            if option is None:
                response = client.docx.v1.document_block.list(request)
            else:
                response = client.docx.v1.document_block.list(request, option)
            if not response.success():
                raise_from_lark_response(
                    response,
                    operation=f"fetch blocks for {document_id}",
                    resource=document_id,
                    using_user_token=self._using_user_token(options),
                )
            items = response.data.items or []
            all_blocks.extend(items)
            if not response.data.has_more:
                break
            page_token = response.data.page_token
        return all_blocks

    def _detect_block_attr(self, block) -> Optional[str]:
        block_type = getattr(block, "block_type", None)
        if block_type is not None:
            attr = self._BLOCK_TYPE_TO_ATTR.get(block_type)
            if attr:
                return attr
        for attr in self._KNOWN_CONTENT_ATTRS:
            if getattr(block, attr, None) is not None:
                return attr
        return None

    def _add_warning(self, message: str) -> None:
        if message not in self._active_warnings:
            self._active_warnings.append(message)

    def _unsupported_block_placeholder(self, block, attr: str) -> str:
        block_id = getattr(block, "block_id", "?")
        message = (
            f"Unsupported Feishu block preserved as placeholder: "
            f"{attr} (block_id={block_id})"
        )
        self._add_warning(message)
        return f"> {message}"

    def block_to_markdown(
        self,
        block,
        block_map: Dict,
        ordered_counter: Dict[str, int],
        document_id: str = "",
        options: Optional[FeishuConvertOptions] = None,
    ) -> Optional[str]:
        options = options or FeishuConvertOptions()
        attr = self._detect_block_attr(block)
        if attr is None:
            return None
        if attr in self._SKIP_ATTRS:
            return None

        if attr != "ordered":
            parent_id = block.parent_id or ""
            if parent_id in ordered_counter:
                del ordered_counter[parent_id]

        if options.include_placeholders and attr in self._PLACEHOLDER_ONLY_ATTRS:
            return self._unsupported_block_placeholder(block, attr)

        special_handler = self._SPECIAL_BLOCK_HANDLERS.get(attr)
        if special_handler:
            result = getattr(self, special_handler)(
                block,
                block_map,
                document_id=document_id,
                options=options,
            )
            if result is None and options.include_placeholders:
                return self._unsupported_block_placeholder(block, attr)
            return result

        content_obj = getattr(block, attr, None)
        if not content_obj or not hasattr(content_obj, "elements") or not content_obj.elements:
            if options.include_placeholders and attr in self._KNOWN_CONTENT_ATTRS:
                return self._unsupported_block_placeholder(block, attr)
            return None

        text = self._extract_text_from_elements(content_obj.elements)
        if not text:
            return None

        if attr.startswith("heading"):
            level = int(attr.replace("heading", "") or "1")
            return f"{'#' * level} {text}"

        if attr == "ordered":
            parent_id = block.parent_id or ""
            counter = ordered_counter.get(parent_id, 0) + 1
            ordered_counter[parent_id] = counter
            return f"{counter}. {text}"

        if attr == "code":
            lang = ""
            if hasattr(content_obj, "style") and content_obj.style:
                lang = str(getattr(content_obj.style, "language", "") or "")
            return f"```{lang}\n{text}\n```"

        if attr == "todo":
            done = False
            if hasattr(content_obj, "style") and content_obj.style:
                done = getattr(content_obj.style, "done", False)
            checkbox = "[x]" if done else "[ ]"
            return f"- {checkbox} {text}"

        fmt = self._TEXT_FORMAT.get(attr)
        if fmt:
            return fmt.format(text=text)
        return text

    @staticmethod
    def _handle_divider(block, block_map: Dict = None, **_) -> str:
        return "---"

    @staticmethod
    def _handle_image(block, block_map: Dict = None, **_) -> Optional[str]:
        image = block.image
        if not image:
            return None
        file_token = image.token or ""
        alt_text = getattr(image, "alt", "") or "image"
        return f"![{alt_text}](feishu://image/{file_token})"

    def _extract_block_text(self, block, attr_name: str) -> str:
        content_obj = getattr(block, attr_name, None)
        if content_obj and hasattr(content_obj, "elements") and content_obj.elements:
            return self._extract_text_from_elements(content_obj.elements)
        return ""

    def _extract_text_from_elements(self, elements) -> str:
        if not elements:
            return ""
        parts = []
        for element in elements:
            text_run = element.text_run
            if text_run:
                content = text_run.content or ""
                style = text_run.text_element_style
                content = self.apply_text_style(content, style)
                parts.append(content)
                continue
            mention_user = element.mention_user
            if mention_user:
                user_id = _getattr_safe(mention_user, "user_id", "user")
                parts.append(f"@{user_id}")
                continue
            mention_doc = element.mention_doc
            if mention_doc:
                title = _getattr_safe(mention_doc, "title", "document")
                url = _getattr_safe(mention_doc, "url", "")
                parts.append(f"[{title}]({url})" if url else str(title))
                continue
            equation = element.equation
            if equation:
                parts.append(f"${_getattr_safe(equation, 'content', '')}$")
                continue
        return "".join(parts)

    @staticmethod
    def apply_text_style(text: str, style) -> str:
        if not text or not style:
            return text
        if getattr(style, "inline_code", False):
            return f"`{text}`"
        link = getattr(style, "link", None)
        if link:
            url = _getattr_safe(link, "url", "")
            if url:
                text = f"[{text}]({url})"
        if getattr(style, "bold", False):
            text = f"**{text}**"
        if getattr(style, "italic", False):
            text = f"*{text}*"
        if getattr(style, "strikethrough", False):
            text = f"~~{text}~~"
        return text

    def _table_block_to_markdown(self, block, block_map: Dict, **_) -> Optional[str]:
        table = block.table
        children = block.children
        if not table or not children:
            return None
        prop = table.property
        if not prop:
            return None
        row_size = prop.row_size or 0
        col_size = prop.column_size or 0
        if not row_size or not col_size:
            return None
        rows = []
        for row_idx in range(row_size):
            row = []
            for col_idx in range(col_size):
                cell_idx = row_idx * col_size + col_idx
                if cell_idx < len(children):
                    cell_block = block_map.get(children[cell_idx])
                    row.append(self._extract_cell_text(cell_block, block_map))
                else:
                    row.append("")
            rows.append(row)
        return format_table_to_markdown(rows, has_header=True) if rows else None

    def _sub_page_list_to_markdown(
        self,
        block,
        block_map: Dict,
        *,
        options: Optional[FeishuConvertOptions] = None,
        **_,
    ) -> Optional[str]:
        context = self._active_wiki_context or {}
        space_id = context.get("space_id")
        node_token = context.get("node_token")
        host = context.get("host") or "www.feishu.cn"
        if not space_id or not node_token:
            return None

        children = self._list_wiki_child_nodes(
            space_id,
            node_token,
            options=options or FeishuConvertOptions(),
        )
        if not children:
            return "> No child pages found."

        lines = ["> Child pages:"]
        for child in children:
            title = getattr(child, "title", None) or getattr(child, "node_token", "Untitled")
            child_token = getattr(child, "node_token", "")
            obj_type = getattr(child, "obj_type", "") or "wiki"
            url = f"https://{host}/wiki/{child_token}" if child_token else ""
            suffix = f" ({obj_type})" if obj_type else ""
            lines.append(f"> - [{title}]({url}){suffix}" if url else f"> - {title}{suffix}")
        return "\n".join(lines)

    def _list_wiki_child_nodes(
        self,
        space_id: str,
        node_token: str,
        *,
        options: FeishuConvertOptions,
    ) -> List[Any]:
        from lark_oapi.api.wiki.v2 import ListSpaceNodeRequest

        client = self._get_client(use_user_token=self._using_user_token(options))
        option = self._user_request_option(options.feishu_access_token)
        all_nodes: List[Any] = []
        page_token = None
        while True:
            builder = (
                ListSpaceNodeRequest.builder()
                .space_id(space_id)
                .parent_node_token(node_token)
                .page_size(50)
            )
            if page_token:
                builder = builder.page_token(page_token)
            request = builder.build()
            response = (
                client.wiki.v2.space_node.list(request)
                if option is None
                else client.wiki.v2.space_node.list(request, option)
            )
            if not response.success():
                self._add_warning(
                    f"Failed to read child pages for wiki node {node_token}: "
                    f"code={response.code} msg={response.msg}"
                )
                return all_nodes
            all_nodes.extend(response.data.items or [])
            if not response.data.has_more:
                break
            page_token = response.data.page_token
        return all_nodes

    def _extract_cell_text(self, cell_block, block_map: Dict) -> str:
        if not cell_block or not cell_block.children:
            return ""
        texts = []
        for child_id in cell_block.children:
            child = block_map.get(child_id)
            if not child:
                continue
            attr = self._detect_block_attr(child)
            if attr:
                text = self._extract_block_text(child, attr)
                if text:
                    texts.append(text)
        return " ".join(texts)

    def _embedded_sheet_to_markdown(
        self,
        block,
        block_map: Dict = None,
        *,
        document_id: str = "",
        options: FeishuConvertOptions,
        **_,
    ) -> Optional[str]:
        import lark_oapi as lark

        client = self._get_client(use_user_token=self._using_user_token(options))
        block_id = block.block_id
        doc_id = document_id or block.parent_id
        raw_req = (
            lark.BaseRequest.builder()
            .http_method(lark.HttpMethod.GET)
            .uri(f"/open-apis/docx/v1/documents/{doc_id}/blocks/{block_id}")
            .token_types({lark.AccessTokenType.TENANT})
            .build()
        )
        option = self._user_request_option(options.feishu_access_token)
        raw_resp = client.request(raw_req) if option is None else client.request(raw_req, option)
        if not raw_resp.success():
            self._add_warning(
                f"Failed to read embedded sheet block {block_id}: "
                f"code={getattr(raw_resp, 'code', None)} msg={getattr(raw_resp, 'msg', None)}"
            )
            return None

        data = json.loads(raw_resp.raw.content)
        sheet_token = data.get("data", {}).get("block", {}).get("sheet", {}).get("token", "")
        if not sheet_token:
            return None

        parts = sheet_token.rsplit("_", 1)
        if len(parts) != 2:
            return None
        spreadsheet_token, sheet_id = parts
        try:
            rows = self._read_sheet_range(
                spreadsheet_token,
                sheet_id,
                max_rows=100,
                max_cols=26,
                options=options,
            )
            if rows:
                rows = self.trim_empty_columns(rows)
            if rows:
                return format_table_to_markdown(rows, has_header=True)
        except Exception as exc:
            self._add_warning(f"Failed to read embedded sheet {sheet_token}: {exc}")
            logger.warning("[FeishuConverter] Failed to read embedded sheet %s: %s", sheet_token, exc)
        return None

    @staticmethod
    def trim_empty_table(rows: List[List[str]]) -> List[List[str]]:
        if not rows:
            return rows
        rows = [row for row in rows if any(cell.strip() for cell in row)]
        if not rows:
            return []
        max_cols = max(len(row) for row in rows)
        last_col = 0
        for col in range(max_cols):
            for row in rows:
                if col < len(row) and row[col].strip():
                    last_col = col + 1
        if last_col == 0:
            return []
        return [row[:last_col] for row in rows]

    trim_empty_columns = trim_empty_table

    def _convert_sheets(
        self,
        token: str,
        *,
        options: FeishuConvertOptions,
    ) -> Tuple[str, str, List[str]]:
        from lark_oapi.api.sheets.v3 import GetSpreadsheetRequest, QuerySpreadsheetSheetRequest

        client = self._get_client(use_user_token=self._using_user_token(options))
        option = self._user_request_option(options.feishu_access_token)
        meta_request = GetSpreadsheetRequest.builder().spreadsheet_token(token).build()
        meta_response = (
            client.sheets.v3.spreadsheet.get(meta_request)
            if option is None
            else client.sheets.v3.spreadsheet.get(meta_request, option)
        )
        title = "Spreadsheet"
        if meta_response.success() and meta_response.data.spreadsheet:
            title = meta_response.data.spreadsheet.title or title

        sheets_request = QuerySpreadsheetSheetRequest.builder().spreadsheet_token(token).build()
        sheets_response = (
            client.sheets.v3.spreadsheet_sheet.query(sheets_request)
            if option is None
            else client.sheets.v3.spreadsheet_sheet.query(sheets_request, option)
        )
        if not sheets_response.success():
            raise_from_lark_response(
                sheets_response,
                operation=f"fetch sheets for {token}",
                resource=token,
                using_user_token=self._using_user_token(options),
            )

        sheets = sheets_response.data.sheets or []
        markdown_parts = [f"# {title}", f"**Sheets:** {len(sheets)}"]
        for sheet in sheets:
            sheet_id = sheet.sheet_id
            sheet_title = sheet.title or sheet_id
            row_count = sheet.grid_properties.row_count if sheet.grid_properties else 0
            col_count = sheet.grid_properties.column_count if sheet.grid_properties else 0
            parts = [f"## Sheet: {sheet_title}"]
            if row_count == 0 or col_count == 0:
                parts.append("*Empty sheet*")
                markdown_parts.append("\n\n".join(parts))
                continue
            parts.append(f"**Dimensions:** {row_count} rows x {col_count} columns")
            rows_to_read = min(row_count, options.max_rows_per_sheet)
            cell_data = self._read_sheet_range(
                token,
                sheet_id,
                rows_to_read,
                col_count,
                options=options,
            )
            cell_data = self.trim_empty_table(cell_data)
            if cell_data:
                parts.append(format_table_to_markdown(cell_data, has_header=True))
            if row_count > options.max_rows_per_sheet:
                parts.append(
                    f"\n*... {row_count - options.max_rows_per_sheet} more rows truncated ...*"
                )
            markdown_parts.append("\n\n".join(parts))
        return "\n\n".join(markdown_parts), title, []

    def _read_sheet_range(
        self,
        token: str,
        sheet_id: str,
        max_rows: int,
        max_cols: int,
        *,
        options: FeishuConvertOptions,
    ) -> List[List[str]]:
        import lark_oapi as lark

        client = self._get_client(use_user_token=self._using_user_token(options))
        end_col = self.col_number_to_letter(min(max_cols, 26))
        range_str = f"{sheet_id}!A1:{end_col}{max_rows}"
        request = (
            lark.BaseRequest.builder()
            .http_method(lark.HttpMethod.GET)
            .uri(f"/open-apis/sheets/v2/spreadsheets/{token}/values/{range_str}")
            .token_types({lark.AccessTokenType.TENANT})
            .build()
        )
        option = self._user_request_option(options.feishu_access_token)
        response = client.request(request) if option is None else client.request(request, option)
        if not response.success():
            raise_from_lark_response(
                response,
                operation=f"read sheet range {range_str}",
                resource=token,
                using_user_token=self._using_user_token(options),
            )
        data = json.loads(response.raw.content)
        values = data.get("data", {}).get("valueRange", {}).get("values", [])
        return [[str(cell) if cell is not None else "" for cell in row] for row in values]

    @staticmethod
    def col_number_to_letter(n: int) -> str:
        return chr(ord("A") + n - 1) if 1 <= n <= 26 else "Z"

    def _fetch_bitable_app_title(
        self,
        app_token: str,
        *,
        options: FeishuConvertOptions,
    ) -> Optional[str]:
        from lark_oapi.api.bitable.v1 import GetAppRequest

        client = self._get_client(use_user_token=self._using_user_token(options))
        option = self._user_request_option(options.feishu_access_token)
        request = GetAppRequest.builder().app_token(app_token).build()
        response = (
            client.bitable.v1.app.get(request)
            if option is None
            else client.bitable.v1.app.get(request, option)
        )
        if not response.success():
            return None
        app = getattr(response.data, "app", None)
        name = getattr(app, "name", None) if app is not None else None
        if isinstance(name, str) and name.strip():
            return name.strip()
        return None

    def _fetch_bitable_view_name(
        self,
        app_token: str,
        table_id: str,
        view_id: str,
        *,
        options: FeishuConvertOptions,
    ) -> Optional[str]:
        from lark_oapi.api.bitable.v1 import GetAppTableViewRequest

        client = self._get_client(use_user_token=self._using_user_token(options))
        option = self._user_request_option(options.feishu_access_token)
        request = (
            GetAppTableViewRequest.builder()
            .app_token(app_token)
            .table_id(table_id)
            .view_id(view_id)
            .build()
        )
        response = (
            client.bitable.v1.app_table_view.get(request)
            if option is None
            else client.bitable.v1.app_table_view.get(request, option)
        )
        if not response.success():
            self._add_warning(
                f"Failed to read bitable view name for {view_id}; using view id in title."
            )
            return None
        view = getattr(response.data, "view", None)
        name = getattr(view, "view_name", None) or getattr(view, "name", None)
        return name.strip() if isinstance(name, str) and name.strip() else None

    def _convert_bitable(
        self,
        app_token: str,
        *,
        options: FeishuConvertOptions,
        table_id: Optional[str] = None,
        view_id: Optional[str] = None,
    ) -> Tuple[str, str, List[str]]:
        from lark_oapi.api.bitable.v1 import (
            ListAppTableFieldRequest,
            ListAppTableRecordRequest,
            ListAppTableRequest,
        )

        client = self._get_client(use_user_token=self._using_user_token(options))
        option = self._user_request_option(options.feishu_access_token)
        tables_request = ListAppTableRequest.builder().app_token(app_token).build()
        tables_response = (
            client.bitable.v1.app_table.list(tables_request)
            if option is None
            else client.bitable.v1.app_table.list(tables_request, option)
        )
        if not tables_response.success():
            raise_from_lark_response(
                tables_response,
                operation=f"list bitable tables for {app_token}",
                resource=app_token,
                using_user_token=self._using_user_token(options),
            )

        tables = tables_response.data.items or []
        title = self._fetch_bitable_app_title(app_token, options=options)
        if not title:
            title = f"Bitable ({len(tables)} tables)"
            self._add_warning(
                f"Failed to read bitable app title for {app_token}; using fallback title {title!r}."
            )
        if view_id:
            view_name = (
                self._fetch_bitable_view_name(app_token, table_id, view_id, options=options)
                if table_id
                else None
            )
            title = f"{title} - {view_name or view_id}"
        markdown_parts = [f"# {title}"]
        for table in tables:
            table_id = table.table_id
            table_name = table.name or table_id
            fields_request = (
                ListAppTableFieldRequest.builder().app_token(app_token).table_id(table_id).build()
            )
            fields_response = (
                client.bitable.v1.app_table_field.list(fields_request)
                if option is None
                else client.bitable.v1.app_table_field.list(fields_request, option)
            )
            field_names: List[str] = []
            if fields_response.success() and fields_response.data.items:
                field_names = [field.field_name for field in fields_response.data.items]

            all_records: list = []
            page_token = None
            while len(all_records) < options.max_records_per_table:
                remaining = options.max_records_per_table - len(all_records)
                page_size = min(remaining, 500)
                builder = (
                    ListAppTableRecordRequest.builder()
                    .app_token(app_token)
                    .table_id(table_id)
                    .page_size(page_size)
                )
                if page_token:
                    builder = builder.page_token(page_token)
                records_response = (
                    client.bitable.v1.app_table_record.list(builder.build())
                    if option is None
                    else client.bitable.v1.app_table_record.list(builder.build(), option)
                )
                if not records_response.success():
                    break
                items = records_response.data.items or []
                all_records.extend(items)
                if not records_response.data.has_more:
                    break
                page_token = records_response.data.page_token

            parts = [f"## {table_name}", f"**Records:** {len(all_records)}"]
            if field_names and all_records:
                rows = [field_names]
                for record in all_records:
                    fields = record.fields or {}
                    row = [
                        self.format_bitable_field(fields.get(field_name, ""))
                        for field_name in field_names
                    ]
                    rows.append(row)
                parts.append(format_table_to_markdown(rows, has_header=True))
            if len(all_records) >= options.max_records_per_table:
                parts.append(f"\n*... records truncated at {options.max_records_per_table} ...*")
            markdown_parts.append("\n\n".join(parts))
        return "\n\n".join(markdown_parts), title, []

    @staticmethod
    def format_bitable_field(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, list):
            texts = []
            for item in value:
                if isinstance(item, dict):
                    texts.append(item.get("text", item.get("name", str(item))))
                else:
                    texts.append(str(item))
            return ", ".join(texts)
        if isinstance(value, dict):
            return value.get("text", value.get("name", str(value)))
        return str(value)
