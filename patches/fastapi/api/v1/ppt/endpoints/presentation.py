import asyncio
from datetime import datetime
import json
import math
import os
import random
import re
import traceback
from copy import deepcopy
from typing import Annotated, Any, Dict, List, Literal, Optional, Tuple
import dirtyjson
from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Path
from fastapi.responses import StreamingResponse
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from constants.presentation import DEFAULT_TEMPLATES
from enums.webhook_event import WebhookEvent
from models.api_error_model import APIErrorModel
from models.generate_presentation_request import GeneratePresentationRequest
from models.presentation_and_path import PresentationPathAndEditPath
from models.presentation_from_template import EditPresentationRequest
from models.presentation_outline_model import (
    PresentationOutlineModel,
    SlideOutlineModel,
)
from enums.tone import Tone
from enums.verbosity import Verbosity
from models.pptx_models import PptxPresentationModel
from models.presentation_layout import PresentationLayoutModel
from models.presentation_structure_model import PresentationStructureModel
from models.presentation_with_slides import (
    PresentationWithSlides,
)
from models.sql.template import TemplateModel

from services.documents_loader import DocumentsLoader
from services.llm_client import LLMClient
from services.webhook_service import WebhookService
from utils.get_layout_by_name import get_layout_by_name
from services.image_generation_service import ImageGenerationService
from utils.dict_utils import deep_update
from utils.export_utils import export_presentation
from utils.llm_calls.generate_presentation_outlines import generate_ppt_outline
from models.sql.slide import SlideModel
from models.sse_response import SSECompleteResponse, SSEErrorResponse, SSEResponse

from services.database import get_async_session
from services.temp_file_service import TEMP_FILE_SERVICE
from services.concurrent_service import CONCURRENT_SERVICE
from models.sql.presentation import PresentationModel
from services.pptx_presentation_creator import PptxPresentationCreator
from models.sql.async_presentation_generation_status import (
    AsyncPresentationGenerationTaskModel,
)
from utils.asset_directory_utils import get_exports_directory, get_images_directory
from utils.llm_calls.generate_presentation_structure import (
    generate_presentation_structure,
)
from utils.llm_provider import get_model
from utils.llm_calls.generate_slide_content import (
    get_slide_content_from_type_and_outline,
)
from utils.ppt_utils import (
    get_presentation_title_from_outlines,
    select_toc_or_list_slide_layout_index,
)
from utils.process_slides import (
    process_slide_add_placeholder_assets,
    process_slide_and_fetch_assets,
)
from models.llm_message import LLMSystemMessage, LLMUserMessage
import uuid


PRESENTATION_ROUTER = APIRouter(prefix="/presentation", tags=["Presentation"])


TITLE_KEYS = {"title", "heading", "headline", "name", "label", "sideheading"}
BODY_KEYS = {
    "description",
    "content",
    "body",
    "statement",
    "subheading",
    "subtitle",
    "paragraph",
    "text",
    "tagline",
    "sideparagraph",
}
METRIC_VALUE_KEYS = {"value", "number", "amount", "count", "score", "percentage"}
METRIC_SYMBOL_KEYS = {"symbol", "numbersymbol", "suffix", "unit"}
METRIC_LABEL_KEYS = {"label", "subtitle", "caption", "name"}
SKIP_TEXT_KEYS = {
    "website",
    "pagenumber",
    "symboltext",
    "__image_url__",
    "__image_prompt__",
    "__icon_url__",
    "__icon_query__",
}
VISUAL_ASSET_KEYS = {
    "image",
    "backgroundimage",
    "picture",
    "photo",
    "media",
    "illustration",
    "thumbnail",
    "icon",
}
PREFERRED_VERBATIM_LAYOUTS = {
    "general": ["general:basic-info-slide", "general:bullet-with-icons-slide"],
    "modern": ["modern:image-and-description", "modern:bullet-with-icons"],
    "standard": [
        "standard:header-title-card-slide",
        "standard:split-left-strip-header-title-subtitle-cards-slide",
    ],
    "swift": ["swift:simple-bullet-points-layout", "swift:icon-bullet-list-description-slide"],
}
IMPORT_SAFE_LAYOUT_IDS = {
    "general": {
        "general:intro-slide",
        "general:general-intro-slide",
        "general:basic-info-slide",
        "general:numbered-bullets-slide",
        "general:table-info-slide",
        "general:metrics-slide",
        "general:chart-with-bullets-slide",
    },
    "modern": {
        "modern:intro-slide",
        "modern:intro-pitchdeck-slide",
        "modern:bullet-with-icons",
        "modern:image-and-description",
        "modern:chart-or-table-with-description",
        "modern:chart-with-metrics",
        "modern:metrics-with-description-image",
    },
    "standard": {
        "standard:intro-slide",
        "standard:header-title-card-slide",
        "standard:split-left-strip-header-title-subtitle-cards-slide",
        "standard:chart-left-text-right-layout",
        "standard:visual-metrics",
        "standard:metrics-description-layout",
        "standard:header-bullets-image-split-slide",
    },
    "swift": {
        "swift:intro-slide-layout",
        "swift:simple-bullet-points-layout",
        "swift:icon-bullet-list-description-slide",
        "swift:timeline",
        "swift:Timeline",
        "swift:tableorChart",
        "swift:MetricsNumbers",
    },
    "neo-modern": {
        "neo-modern:title-description-bullet-list",
        "neo-modern:title-description-table",
        "neo-modern:title-two-column-numbered-list",
        "neo-modern:title-description-image-right",
        "neo-modern:title-kpi-snapshot-grid",
        "neo-modern:title-description-dual-metrics-grid",
        "neo-modern:title-description-metrics-chart",
        "neo-modern:title-dual-comparison-charts",
        "neo-modern:title-subtitles-chart",
        "neo-modern:title-horizontal-alternating-timeline",
        "neo-modern:title-description-icon-timeline",
    },
    "neo-standard": {
        "neo-standard:title-description-bullet-list",
        "neo-standard:title-description-table",
        "neo-standard:title-description-image-right",
        "neo-standard:title-kpi-grid",
        "neo-standard:title-metrics-chart",
        "neo-standard:title-badge-chart",
        "neo-standard:title-dual-charts-comparison",
        "neo-standard:title-description-timeline",
    },
    "neo-general": {
        "neo-general:title-description-with-table",
        "neo-general:title-description-three-columns-table",
        "neo-general:numbered-bullets-slide",
        "neo-general:title-with-full-width-chart",
        "neo-general:title-metrics-with-chart",
        "neo-general:layout-text-block-with-metric-cards",
        "neo-general:performance-grid-snapshot-slide",
        "neo-general:timeline-alternating-cards-slide",
        "neo-general:chart-with-bullets-slide",
    },
    "neo-swift": {
        "neo-swift:title-description-bullet-list",
        "neo-swift:title-description-data-table",
        "neo-swift:title-description-three-column-table",
        "neo-swift:title-description-image-right",
        "neo-swift:title-centered-chart",
        "neo-swift:title-chart-metrics-sidebar",
        "neo-swift:title-description-eight-metrics-grid",
        "neo-swift:title-three-by-three-metrics-grid",
        "neo-swift:title-tagline-description-numbered-steps",
    },
}
IMPORT_SAFE_LAYOUT_FAMILIES = {
    "general": {
        "cover": ["general:intro-slide"],
        "dense-text": ["general:basic-info-slide"],
        "bullet": ["general:numbered-bullets-slide", "general:basic-info-slide"],
        "table": ["general:table-info-slide", "general:basic-info-slide"],
        "chart": ["general:chart-with-bullets-slide", "general:metrics-slide"],
        "kpi": ["general:metrics-slide", "general:metrics-with-image-slide"],
        "comparison": ["general:table-info-slide", "general:numbered-bullets-slide"],
        "process": ["general:numbered-bullets-slide", "general:basic-info-slide"],
        "roadmap": ["general:numbered-bullets-slide", "general:basic-info-slide"],
    },
    "modern": {
        "cover": ["modern:intro-slide"],
        "dense-text": ["modern:image-and-description", "modern:bullet-with-icons"],
        "bullet": ["modern:bullet-with-icons", "modern:image-and-description"],
        "table": ["modern:chart-or-table-with-description", "modern:image-and-description"],
        "chart": ["modern:chart-or-table-with-description", "modern:chart-with-metrics"],
        "kpi": ["modern:chart-with-metrics", "modern:metrics-with-description-image"],
        "comparison": ["modern:chart-or-table-with-description", "modern:bullet-with-icons"],
        "process": ["modern:bullet-with-icons", "modern:image-and-description"],
        "roadmap": ["modern:bullet-with-icons", "modern:image-and-description"],
    },
    "standard": {
        "cover": ["standard:intro-slide"],
        "dense-text": ["standard:header-title-card-slide"],
        "bullet": [
            "standard:split-left-strip-header-title-subtitle-cards-slide",
            "standard:header-title-card-slide",
        ],
        "table": ["standard:chart-left-text-right-layout", "standard:header-title-card-slide"],
        "chart": ["standard:chart-left-text-right-layout", "standard:visual-metrics"],
        "kpi": ["standard:visual-metrics", "standard:metrics-description-layout"],
        "comparison": [
            "standard:chart-left-text-right-layout",
            "standard:split-left-strip-header-title-subtitle-cards-slide",
        ],
        "process": [
            "standard:split-left-strip-header-title-subtitle-cards-slide",
            "standard:header-title-card-slide",
        ],
        "roadmap": [
            "standard:split-left-strip-header-title-subtitle-cards-slide",
            "standard:header-title-card-slide",
        ],
    },
    "swift": {
        "cover": ["swift:intro-slide-layout"],
        "dense-text": ["swift:simple-bullet-points-layout"],
        "bullet": [
            "swift:simple-bullet-points-layout",
            "swift:icon-bullet-list-description-slide",
        ],
        "table": ["swift:tableorChart", "swift:simple-bullet-points-layout"],
        "chart": ["swift:tableorChart", "swift:MetricsNumbers"],
        "kpi": ["swift:MetricsNumbers", "swift:simple-bullet-points-layout"],
        "comparison": ["swift:tableorChart", "swift:simple-bullet-points-layout"],
        "process": ["swift:Timeline", "swift:icon-bullet-list-description-slide"],
        "roadmap": ["swift:Timeline", "swift:icon-bullet-list-description-slide"],
    },
    "neo-modern": {
        "cover": ["neo-modern:title-description-image-right"],
        "dense-text": ["neo-modern:title-description-bullet-list"],
        "bullet": [
            "neo-modern:title-two-column-numbered-list",
            "neo-modern:title-description-bullet-list",
        ],
        "table": ["neo-modern:title-description-table"],
        "chart": [
            "neo-modern:title-description-metrics-chart",
            "neo-modern:title-subtitles-chart",
            "neo-modern:title-dual-comparison-charts",
        ],
        "kpi": [
            "neo-modern:title-kpi-snapshot-grid",
            "neo-modern:title-description-dual-metrics-grid",
        ],
        "comparison": [
            "neo-modern:title-dual-comparison-cards",
            "neo-modern:title-dual-comparison-charts",
            "neo-modern:title-description-table",
        ],
        "process": [
            "neo-modern:title-horizontal-alternating-timeline",
            "neo-modern:title-description-icon-timeline",
            "neo-modern:title-two-column-numbered-list",
        ],
        "roadmap": [
            "neo-modern:title-horizontal-alternating-timeline",
            "neo-modern:title-description-icon-timeline",
            "neo-modern:title-two-column-numbered-list",
        ],
    },
    "neo-standard": {
        "cover": ["neo-standard:title-description-image-right"],
        "dense-text": ["neo-standard:title-description-bullet-list"],
        "bullet": ["neo-standard:title-description-bullet-list"],
        "table": ["neo-standard:title-description-table"],
        "chart": ["neo-standard:title-metrics-chart", "neo-standard:title-badge-chart"],
        "kpi": ["neo-standard:title-kpi-grid", "neo-standard:title-metrics-chart"],
        "comparison": [
            "neo-standard:title-dual-comparison-cards",
            "neo-standard:title-dual-charts-comparison",
            "neo-standard:title-description-table",
        ],
        "process": ["neo-standard:title-description-timeline", "neo-standard:title-description-bullet-list"],
        "roadmap": ["neo-standard:title-description-timeline", "neo-standard:title-description-bullet-list"],
    },
    "neo-general": {
        "cover": ["neo-general:numbered-bullets-slide"],
        "dense-text": ["neo-general:numbered-bullets-slide"],
        "bullet": ["neo-general:numbered-bullets-slide"],
        "table": ["neo-general:title-description-with-table"],
        "chart": ["neo-general:title-with-full-width-chart", "neo-general:title-metrics-with-chart"],
        "kpi": [
            "neo-general:layout-text-block-with-metric-cards",
            "neo-general:performance-grid-snapshot-slide",
        ],
        "comparison": [
            "neo-general:title-description-three-columns-table",
            "neo-general:title-three-column-risk-constraints-slide-layout",
        ],
        "process": ["neo-general:timeline-alternating-cards-slide", "neo-general:numbered-bullets-slide"],
        "roadmap": ["neo-general:timeline-alternating-cards-slide", "neo-general:numbered-bullets-slide"],
    },
    "neo-swift": {
        "cover": ["neo-swift:title-description-image-right"],
        "dense-text": ["neo-swift:title-description-bullet-list"],
        "bullet": [
            "neo-swift:title-description-bullet-list",
            "neo-swift:title-tagline-description-numbered-steps",
        ],
        "table": ["neo-swift:title-description-data-table"],
        "chart": ["neo-swift:title-centered-chart", "neo-swift:title-chart-metrics-sidebar"],
        "kpi": [
            "neo-swift:title-description-eight-metrics-grid",
            "neo-swift:title-three-by-three-metrics-grid",
            "neo-swift:title-label-description-cascading-stats",
        ],
        "comparison": [
            "neo-swift:title-dual-comparison-blocks-numbered",
            "neo-swift:title-description-three-column-table",
        ],
        "process": ["neo-swift:title-tagline-description-numbered-steps", "neo-swift:title-description-bullet-list"],
        "roadmap": ["neo-swift:title-tagline-description-numbered-steps", "neo-swift:title-description-bullet-list"],
    },
}
DATA_LAYOUT_KEYWORDS = {
    "chart",
    "graph",
    "metric",
    "data",
    "table",
    "stats",
}
COMPACT_VISUAL_LAYOUT_KEYWORDS = {
    "card",
    "cards",
    "grid",
    "icon",
    "icons",
    "metric",
    "stats",
    "timeline",
    "process",
    "step",
}
TEXT_HEAVY_LAYOUT_KEYWORDS = {
    "text",
    "content",
    "description",
    "paragraph",
    "article",
    "info",
    "split",
    "basic",
}
TABLE_LAYOUT_KEYWORDS = {"table", "matrix", "comparison"}
CHART_LAYOUT_KEYWORDS = {"chart", "graph", "plot", "donut", "bar", "line", "pie"}
KPI_LAYOUT_KEYWORDS = {"kpi", "metric", "metrics", "stat", "stats", "snapshot"}
RISKY_VERBATIM_LAYOUT_KEYWORDS = {
    "chart",
    "graph",
    "kpi",
    "metric",
    "snapshot",
    "radial",
    "circle",
    "donut",
    "team",
    "contact",
    "quote",
    "image",
    "icon",
    "grid",
    "cards",
    "comparison",
}
BULLET_MARKER_PATTERN = r"^\s*(?:\d{1,2}[\).]\s+|[-*\u2022]\s*)"
BULLET_PREFIX_RE = re.compile(BULLET_MARKER_PATTERN)
MOJIBAKE_MARKERS = (
    "Ð",
    "Ñ",
    "Â",
    "â",
    "Р°",
    "Рµ",
    "Рё",
    "Рѕ",
    "Рґ",
    "Р»",
    "Рј",
    "РЅ",
    "Рї",
    "СЃ",
    "С‚",
    "СЊ",
    "СЌ",
    "вЂ",
)
PER_SLIDE_LLM_TIMEOUT_SECONDS = 25
ROADMAP_HEADING_RE = re.compile(
    r"^\s*(?:(?:этап|шаг|stage|step|phase)\s*\d+|итого|total|20\d{2}(?:\s*[-–—]\s*20\d{2})?)",
    re.IGNORECASE,
)
ROADMAP_TITLE_RE = re.compile(
    r"(дорожн|roadmap|план|этап|шаг|stage|phase|timeline|202\d|203\d)",
    re.IGNORECASE,
)
MOJIBAKE_MARKERS = MOJIBAKE_MARKERS + (
    "Ð",
    "Ñ",
    "Â",
    "Ã",
    "â€",
    "â€“",
    "â€”",
)
ROADMAP_HEADING_RE = re.compile(
    r"^\s*(?:(?:этап|шаг|stage|step|phase)\s*\d+|итого|total|20\d{2}(?:\s*[-–—]\s*20\d{2})?)",
    re.IGNORECASE,
)
ROADMAP_TITLE_RE = re.compile(
    r"(дорожн|roadmap|план|этап|шаг|stage|phase|timeline|202\d|203\d)",
    re.IGNORECASE,
)


def _mojibake_score(value: str) -> int:
    return sum(value.count(marker) for marker in MOJIBAKE_MARKERS)


def _cyrillic_score(value: str) -> int:
    return len(re.findall(r"[\u0400-\u04FF]", value or ""))


def _fix_mojibake_text(value: str) -> str:
    if not value or _mojibake_score(value) == 0:
        return value

    candidates = {value}
    frontier = {value}
    for _ in range(3):
        next_frontier = set()
        for candidate in frontier:
            for encoding in ("latin1", "cp1252", "cp1251"):
                try:
                    fixed = candidate.encode(encoding).decode("utf-8")
                except Exception:
                    continue
                if fixed not in candidates:
                    candidates.add(fixed)
                    next_frontier.add(fixed)
        frontier = next_frontier
        if not frontier:
            break

    def fix_segment(match: re.Match) -> str:
        segment = match.group(0)
        segment_candidates = {segment}
        for encoding in ("latin1", "cp1252", "cp1251"):
            try:
                segment_candidates.add(segment.encode(encoding).decode("utf-8"))
            except Exception:
                continue
        return max(
            segment_candidates,
            key=lambda candidate: (
                _cyrillic_score(candidate) - _mojibake_score(candidate) * 4,
                -_mojibake_score(candidate),
            ),
        )

    try:
        candidates.add(re.sub(r"[\u0080-\u00FF]+", fix_segment, value))
    except Exception:
        pass

    def score(candidate: str) -> tuple[int, int]:
        return (_cyrillic_score(candidate) - _mojibake_score(candidate) * 4, -_mojibake_score(candidate))

    return max(candidates, key=score)


def _clean_verbatim_text(value: str) -> str:
    value = value or ""
    value = "\n".join(_fix_mojibake_text(line) for line in value.splitlines())
    value = value.replace("\u00a0", " ")
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{4,}", "\n\n\n", value)
    return value.strip()


def _normalized_key(key: str) -> str:
    return key.replace("_", "").replace("-", "").lower()


def _schema_type(schema: Any) -> Optional[str]:
    if not isinstance(schema, dict):
        return None

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        return next((item for item in schema_type if item != "null"), None)
    if schema_type:
        return schema_type

    for union_key in ("anyOf", "oneOf", "allOf"):
        union_schemas = schema.get(union_key)
        if isinstance(union_schemas, list):
            for union_schema in union_schemas:
                union_type = _schema_type(union_schema)
                if union_type:
                    return union_type
    return None


def _schema_default(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return None
    if "default" in schema:
        return deepcopy(schema["default"])
    if "anyOf" in schema and schema["anyOf"]:
        return _schema_default(schema["anyOf"][0])

    schema_type = _schema_type(schema)
    if schema_type == "object":
        return {
            key: _schema_default(value_schema)
            for key, value_schema in schema.get("properties", {}).items()
        }
    if schema_type == "array":
        min_items = schema.get("minItems", 0)
        default_items = []
        item_schema = schema.get("items", {})
        for _ in range(min_items):
            default_items.append(_schema_default(item_schema))
        return default_items
    if schema_type == "string":
        return ""
    if schema_type == "number" or schema_type == "integer":
        return 0
    if schema_type == "boolean":
        return False
    return None


def _stringify_content_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(
            part for part in (_stringify_content_value(item) for item in value) if part
        )
    if isinstance(value, dict):
        for preferred_key in ("text", "content", "description", "title", "label", "name"):
            preferred_value = value.get(preferred_key)
            if isinstance(preferred_value, str):
                return preferred_value
        return "\n".join(
            part for part in (_stringify_content_value(item) for item in value.values()) if part
        )
    return str(value)


def _coerce_value_to_schema(schema: Any, value: Any) -> Any:
    schema_type = _schema_type(schema)

    if schema_type == "object":
        value_dict = value if isinstance(value, dict) else {}
        return {
            key: _coerce_value_to_schema(value_schema, value_dict.get(key))
            for key, value_schema in (schema.get("properties", {}) or {}).items()
        }

    if schema_type == "array":
        item_schema = schema.get("items", {}) if isinstance(schema, dict) else {}
        raw_items = value if isinstance(value, list) else []
        if not raw_items:
            return []
        return [_coerce_value_to_schema(item_schema, item) for item in raw_items]

    if schema_type == "string":
        return _stringify_content_value(value)

    if schema_type == "integer":
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    if schema_type == "number":
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0

    if schema_type == "boolean":
        return bool(value)

    return _stringify_content_value(value)


def _coerce_content_to_schema(schema: Any, content: dict) -> dict:
    if not isinstance(schema, dict):
        return content

    coerced = content.copy()
    for key, value_schema in (schema.get("properties", {}) or {}).items():
        coerced[key] = _coerce_value_to_schema(value_schema, coerced.get(key))
    return coerced


def _is_text_key(key: str) -> bool:
    normalized = _normalized_key(key)
    return normalized not in SKIP_TEXT_KEYS and (
        normalized in TITLE_KEYS or normalized in BODY_KEYS
    )


def _is_title_key(key: str) -> bool:
    return _normalized_key(key) in TITLE_KEYS


def _is_body_key(key: str) -> bool:
    return _normalized_key(key) in BODY_KEYS


def _is_metric_value_key(key: str) -> bool:
    return _normalized_key(key) in METRIC_VALUE_KEYS


def _is_metric_symbol_key(key: str) -> bool:
    return _normalized_key(key) in METRIC_SYMBOL_KEYS


def _is_metric_label_key(key: str) -> bool:
    return _normalized_key(key) in METRIC_LABEL_KEYS


def _layout_has_data_fields(slide_layout) -> bool:
    summary_text = _layout_summary_text(slide_layout)
    if any(keyword in summary_text for keyword in DATA_LAYOUT_KEYWORDS):
        return True
    properties = (slide_layout.json_schema or {}).get("properties", {})
    return "chart" in properties


def _non_empty_lines(text: str) -> List[str]:
    return [line.strip() for line in _clean_verbatim_text(text).splitlines() if line.strip()]


def _text_has_table_data(text: str) -> bool:
    lines = _non_empty_lines(text)
    return any("|" in line or "\t" in line for line in lines)


def _text_has_roadmap_data(text: str) -> bool:
    lines = _non_empty_lines(text)
    if not lines:
        return False

    joined = "\n".join(lines).lower()
    title = lines[0].lower()
    stage_lines = [line for line in lines if ROADMAP_HEADING_RE.match(line)]
    has_title_signal = bool(ROADMAP_TITLE_RE.search(title) or ROADMAP_TITLE_RE.search(joined))
    has_years = len(re.findall(r"20\d{2}", joined)) >= 2
    return len(stage_lines) >= 2 or (has_title_signal and has_years)

    joined = "\n".join(lines).lower()
    title = lines[0].lower()
    stage_lines = [
        line
        for line in lines
        if re.match(
            r"^\s*(?:(этап|шаг|stage|step|phase)\s*\d+|итого|total|20\d{2}(?:\s*[–-]\s*20\d{2})?)",
            line,
            re.IGNORECASE,
        )
    ]
    has_title_signal = any(
        keyword in title or keyword in joined
        for keyword in ("дорожн", "roadmap", "timeline", "этап", "phase")
    )
    has_years = len(re.findall(r"20\d{2}", joined)) >= 2
    return len(stage_lines) >= 2 or (has_title_signal and has_years)


def _numeric_line_count(text: str) -> int:
    return len([line for line in _non_empty_lines(text) if re.search(r"\d", line)])


def _short_metric_lines(text: str) -> List[str]:
    lines = _non_empty_lines(text)
    metric_lines = []
    for line in lines:
        if not re.search(r"\d", line):
            continue
        if "|" in line or "\t" in line:
            continue
        if len(line) <= 95:
            metric_lines.append(line)
    return metric_lines


def _text_has_kpi_data(text: str) -> bool:
    metric_lines = _short_metric_lines(text)
    total_lines = max(len(_non_empty_lines(text)), 1)
    return len(metric_lines) >= 3 and len(metric_lines) / total_lines >= 0.35


def _text_has_chart_data(text: str) -> bool:
    if _text_has_table_data(text):
        return False
    lines = _non_empty_lines(text)
    if len(lines) < 3 or len(text or "") >= 900:
        return False
    metric_lines = _short_metric_lines(text)
    if len(metric_lines) < 3:
        return False
    year_lines = sum(1 for line in lines if re.search(r"\b(?:19|20)\d{2}\b", line))
    dated_lines = sum(1 for line in lines if re.search(r"\b(?:q[1-4]|квартал|янв|фев|мар|апр|май|июн|июл|авг|сен|окт|ноя|дек)\b", line.lower()))
    category_lines = sum(
        1
        for line in lines
        if re.search(r"[:\-]\s*[-+]?\d", line) or line.count("|") >= 1 or line.count(";") >= 1
    )
    return (
        len(metric_lines) >= 4
        and (
            year_lines >= 2
            or dated_lines >= 2
            or category_lines >= 2
        )
    )


def _text_has_structured_data(text: str) -> bool:
    lines = _non_empty_lines(text)
    metric_lines = [line for line in lines if re.search(r"\d", line)]
    table_lines = [line for line in lines if "|" in line or "\t" in line]
    return len(metric_lines) >= 2 or len(table_lines) >= 1


def _text_has_comparison_data(text: str) -> bool:
    lines = _non_empty_lines(text)
    joined = "\n".join(lines).lower()
    comparison_terms = {
        "vs",
        "versus",
        "compare",
        "comparison",
        "contrast",
        "сравн",
        "против",
    }
    return _text_has_table_data(text) and len(lines) >= 3 or any(term in joined for term in comparison_terms)


def _text_has_process_data(text: str) -> bool:
    lines = _non_empty_lines(text)
    joined = "\n".join(lines).lower()
    step_lines = [
        line
        for line in lines
        if re.match(r"^\s*(?:\d{1,2}[\).]\s+|[-*\u2022]\s+|(?:step|phase|этап|шаг)\s*\d+)", line, re.IGNORECASE)
    ]
    return len(step_lines) >= 3 or any(term in joined for term in ("process", "workflow", "pipeline", "процесс", "этап", "шаг"))


def _detect_numbered_structure(text: str) -> Dict[str, int]:
    lines = _non_empty_lines(text)
    numbered = 0
    bullets = 0
    dash_bullets = 0
    for line in lines:
        if re.match(r"^\s*\d{1,2}[\).]\s+", line):
            numbered += 1
        elif re.match(r"^\s*(?:[-\u2022])\s+", line):
            bullets += 1
            dash_bullets += 1
    return {
        "numbered_count": numbered,
        "bullet_count": bullets,
        "dash_bullet_count": dash_bullets,
        "structured_count": numbered + bullets,
    }


def _is_short_section_divider(slide_text: str) -> bool:
    lines = _non_empty_lines(slide_text)
    if not lines:
        return False
    title, body, _ = _split_slide_text(slide_text)
    numbered = _detect_numbered_structure(slide_text)
    return (
        len(slide_text or "") <= 200
        and len(lines) <= 3
        and numbered["structured_count"] < 2
        and len(body) <= 140
        and not _text_has_table_data(slide_text)
        and not _text_has_chart_data(slide_text)
        and not _text_has_kpi_data(slide_text)
        and len(title) <= 120
    )


def _is_intro_candidate(slide_text: str, slide_meta: Optional[Dict[str, Any]] = None) -> bool:
    slide_meta = slide_meta or {}
    source_slide_index = int(slide_meta.get("source_slide_index", slide_meta.get("source_index", -1)) or -1)
    density = _verbatim_density(slide_text)
    if source_slide_index == 0 and density == "normal":
        return True
    return _is_short_section_divider(slide_text)


def _classify_verbatim_slide(slide_text: str) -> str:
    text = _clean_verbatim_text(slide_text)
    lines = _non_empty_lines(text)
    title, body, body_lines = _split_slide_text(text) if "_split_slide_text" in globals() else (lines[0] if lines else "", "", lines[1:])
    density = _slide_text_density(text) if "_slide_text_density" in globals() else len(text)
    numbered = _detect_numbered_structure(text)

    if not lines:
        return "dense_text"
    if _text_has_roadmap_data(text):
        return "timeline"
    if _text_has_table_data(text):
        return "comparison" if _text_has_comparison_data(text) else "table"
    if _text_has_kpi_data(text) and density < 950:
        return "kpi"
    if _text_has_chart_data(text):
        return "chart"
    if _text_has_process_data(text):
        return "process"
    if numbered["numbered_count"] >= 3:
        return "bullets"
    if len(lines) <= 2 and len(body) < 180:
        return "cover"
    looks_like_list = "_looks_like_list_slide" in globals() and _looks_like_list_slide(body)
    if looks_like_list:
        return "bullets"
    if len(body_lines) >= 4 and density < 760:
        return "bullets"
    return "dense_text"


def _renderer_family_from_classifier(family: str) -> str:
    return {
        "dense_text": "dense-text",
        "bullets": "bullet",
        "timeline": "roadmap",
        "process": "roadmap",
    }.get(family, family)


def _layout_has_schema_key(slide_layout, keywords: set[str]) -> bool:
    schema_text = json.dumps(slide_layout.json_schema or {}, ensure_ascii=False).lower()
    return any(keyword in schema_text for keyword in keywords)


def _schema_has_visual_asset_field(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False

    properties = schema.get("properties")
    if isinstance(properties, dict):
        for key, value_schema in properties.items():
            normalized_key = _normalized_key(key)
            if normalized_key in VISUAL_ASSET_KEYS or normalized_key.endswith("image"):
                return True
            if _schema_has_visual_asset_field(value_schema):
                return True

    for union_key in ("anyOf", "oneOf", "allOf"):
        union_schemas = schema.get(union_key)
        if isinstance(union_schemas, list) and any(
            _schema_has_visual_asset_field(union_schema)
            for union_schema in union_schemas
        ):
            return True

    return _schema_has_visual_asset_field(schema.get("items"))


def _layout_is_table_layout(slide_layout) -> bool:
    search_text = _layout_search_text(slide_layout)
    return any(keyword in search_text for keyword in TABLE_LAYOUT_KEYWORDS) or _layout_has_schema_key(
        slide_layout, {"table"}
    )


def _layout_is_chart_layout(slide_layout) -> bool:
    search_text = _layout_search_text(slide_layout)
    return any(keyword in search_text for keyword in CHART_LAYOUT_KEYWORDS) or _layout_has_schema_key(
        slide_layout, {"chart"}
    )


def _layout_is_kpi_layout(slide_layout) -> bool:
    search_text = _layout_search_text(slide_layout)
    return any(keyword in search_text for keyword in KPI_LAYOUT_KEYWORDS) or _layout_has_schema_key(
        slide_layout, {"kpicards", "metrics", "toplabel", "bottomlabel"}
    )


def _is_import_safe_layout(layout_name: str, slide_layout) -> bool:
    safe_ids = IMPORT_SAFE_LAYOUT_IDS.get(layout_name, set())
    layout_id = str(getattr(slide_layout, "id", "") or "")
    if safe_ids:
        return layout_id in safe_ids

    search_text = _layout_search_text(slide_layout)
    return not any(keyword in search_text for keyword in RISKY_VERBATIM_LAYOUT_KEYWORDS)


def _filter_import_safe_layout(layout: PresentationLayoutModel) -> PresentationLayoutModel:
    safe_slides = [
        slide_layout
        for slide_layout in layout.slides
        if _is_import_safe_layout(layout.name, slide_layout)
    ]
    if not safe_slides:
        safe_slides = layout.slides

    return PresentationLayoutModel(
        name=layout.name,
        ordered=layout.ordered,
        slides=safe_slides,
    )


def _score_layout_for_verbatim(slide_layout) -> int:
    schema = slide_layout.json_schema or {}
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    search_text = _layout_search_text(slide_layout)
    score = 0
    if any(keyword in search_text for keyword in {"quote", "testimonial", "team", "people", "contact"}):
        score -= 600
    for key, value in properties.items():
        normalized = _normalized_key(key)
        if _is_title_key(key):
            score += 40
        if _is_body_key(key):
            score += 40
        if isinstance(value, dict) and _schema_type(value) == "array":
            score += 15
        if "image" in normalized or "icon" in normalized or "chart" in normalized:
            score -= 10
    return score


def _layout_summary_text(slide_layout) -> str:
    return " ".join(
        [
            str(getattr(slide_layout, "id", "") or ""),
            str(getattr(slide_layout, "name", "") or ""),
            str(getattr(slide_layout, "description", "") or ""),
        ]
    ).lower()


def _layout_search_text(slide_layout) -> str:
    return " ".join(
        [
            _layout_summary_text(slide_layout),
            json.dumps(slide_layout.json_schema or {}, ensure_ascii=False),
        ]
    ).lower()


def _slide_layout_keywords(slide_text: str) -> List[str]:
    slide_text = _clean_verbatim_text(slide_text)
    lines = _non_empty_lines(slide_text)
    body = "\n".join(lines[1:])
    total_chars = len(slide_text or "")
    family = _classify_verbatim_slide(slide_text)
    numbered = _detect_numbered_structure(slide_text)
    if family == "timeline":
        return ["timeline", "roadmap", "process", "step", "steps", "phase", "cards", "numbered"]
    if family == "process":
        return ["process", "step", "steps", "timeline", "numbered", "cards"]
    if family == "table":
        return ["table", "comparison", "matrix", "content", "description"]
    if family == "comparison":
        return ["comparison", "table", "matrix", "dual", "two", "cards"]
    if family == "chart":
        return ["chart", "graph", "metric", "data", "stats"]
    if family == "kpi":
        return ["kpi", "metric", "stats", "data"]
    if numbered["numbered_count"] >= 3:
        return ["numbered", "bullet", "list", "steps", "process", "cards", "grid"]
    if total_chars > 900 or len(lines) > 10:
        return ["text", "content", "description", "paragraph", "article", "info", "split", "basic"]
    if total_chars > 650:
        return ["text", "content", "description", "paragraph", "info", "split", "basic"]
    if _text_has_structured_data(slide_text):
        return ["table", "content", "description", "metric", "data"]
    if len(lines) <= 2 and len(body) < 180:
        return ["title", "intro", "cover", "hero", "section", "image"]
    if len(lines) >= 4:
        return ["bullet", "list", "timeline", "process", "step", "cards", "grid"]
    if len(slide_text or "") > 420:
        return ["text", "content", "description", "paragraph", "info"]
    return ["content", "info", "split", "card", "image"]


def _infer_verbatim_slide_family(slide_text: str) -> str:
    return _renderer_family_from_classifier(_classify_verbatim_slide(slide_text))


def _preferred_verbatim_layout_ids(layout_name: str, family: str) -> List[str]:
    family_ids = IMPORT_SAFE_LAYOUT_FAMILIES.get(layout_name, {})
    ids = family_ids.get(family)
    if ids:
        return ids
    fallback_ids = IMPORT_SAFE_LAYOUT_IDS.get(layout_name, set())
    return sorted(fallback_ids)


def _preferred_verbatim_layout_indexes(
    layout: PresentationLayoutModel,
    slide_text: str,
    slide_meta: Optional[Dict[str, Any]] = None,
) -> List[int]:
    family = _infer_verbatim_slide_family(slide_text)
    if _is_intro_candidate(slide_text, slide_meta):
        intro_indexes = [
            index
            for index, slide_layout in enumerate(layout.slides)
            if any(
                keyword in _layout_search_text(slide_layout)
                for keyword in {"intro", "cover", "hero", "statement", "section"}
            )
            and not any(
                keyword in _layout_search_text(slide_layout)
                for keyword in {"quote", "team", "contact"}
            )
        ]
        if intro_indexes:
            return intro_indexes
    preferred_ids = _preferred_verbatim_layout_ids(layout.name, family)
    indexes = [
        index
        for index, slide_layout in enumerate(layout.slides)
        if slide_layout.id in preferred_ids
    ]
    if indexes:
        return indexes

    if family == "table":
        return [
            index
            for index, slide_layout in enumerate(layout.slides)
            if _layout_is_table_layout(slide_layout)
        ]
    if family == "chart":
        return [
            index
            for index, slide_layout in enumerate(layout.slides)
            if _layout_is_chart_layout(slide_layout)
        ]
    if family == "kpi":
        return [
            index
            for index, slide_layout in enumerate(layout.slides)
            if _layout_is_kpi_layout(slide_layout)
        ]
    if family == "comparison":
        return [
            index
            for index, slide_layout in enumerate(layout.slides)
            if _layout_is_table_layout(slide_layout)
            or "comparison" in _layout_search_text(slide_layout)
            or "dual" in _layout_search_text(slide_layout)
        ]
    if family == "cover":
        return [
            index
            for index, slide_layout in enumerate(layout.slides)
            if any(
                keyword in _layout_search_text(slide_layout)
                for keyword in {"intro", "cover", "hero", "statement", "section"}
            )
            and not any(
                keyword in _layout_search_text(slide_layout)
                for keyword in {"quote", "team", "contact"}
            )
        ]
    if family == "bullet":
        return [
            index
            for index, slide_layout in enumerate(layout.slides)
            if not _layout_unsuitable_for_verbatim(slide_layout, slide_text)
            and _layout_body_slot_count(slide_layout) <= 3
        ]
    if family == "roadmap":
        return [
            index
            for index, slide_layout in enumerate(layout.slides)
            if any(
                keyword in _layout_search_text(slide_layout)
                for keyword in {"timeline", "roadmap", "process", "step", "steps", "numbered"}
            )
            and not _layout_unsuitable_for_verbatim(slide_layout, slide_text)
        ]
    if family == "process":
        return [
            index
            for index, slide_layout in enumerate(layout.slides)
            if any(
                keyword in _layout_search_text(slide_layout)
                for keyword in {"process", "step", "steps", "timeline", "numbered"}
            )
            and not _layout_unsuitable_for_verbatim(slide_layout, slide_text)
        ]
    return [
        index
        for index, slide_layout in enumerate(layout.slides)
        if not _layout_unsuitable_for_verbatim(slide_layout, slide_text)
        and _layout_text_capacity_score(slide_layout) >= 2
        and _layout_body_slot_count(slide_layout) <= 2
    ]


def _layout_text_capacity_score(slide_layout) -> int:
    schema = slide_layout.json_schema or {}
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    score = 0

    for key, value_schema in properties.items():
        if not isinstance(value_schema, dict):
            continue

        value_type = _schema_type(value_schema)
        if value_type == "string" and _is_body_key(key):
            score += 2
        elif value_type == "array":
            score += 1
            item_properties = (
                value_schema.get("items", {}).get("properties", {})
                if isinstance(value_schema.get("items"), dict)
                else {}
            )
            if any(_is_body_key(item_key) for item_key in item_properties):
                score += 1
        elif value_type == "object":
            nested_properties = value_schema.get("properties", {})
            if any(_is_body_key(nested_key) for nested_key in nested_properties):
                score += 1

    return score


def _layout_body_slot_count(slide_layout) -> int:
    schema = slide_layout.json_schema or {}
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    slot_count = 0

    for key, value_schema in properties.items():
        if not isinstance(value_schema, dict):
            continue

        value_type = _schema_type(value_schema)
        if value_type == "string" and _is_body_key(key):
            slot_count += 1
        elif value_type == "array":
            slot_count += max(1, value_schema.get("maxItems", 1))
        elif value_type == "object":
            nested_properties = value_schema.get("properties", {})
            slot_count += sum(
                1
                for nested_key, nested_schema in nested_properties.items()
                if isinstance(nested_schema, dict)
                and _schema_type(nested_schema) == "string"
                and _is_body_key(nested_key)
            )

    return slot_count


def _largest_section_ratio(slide_text: str) -> float:
    _, body, body_lines = _split_slide_text(slide_text)
    if not body_lines:
        return 1.0

    parts = _split_dense_paragraphs(body)
    if not parts:
        return 1.0

    largest = max(len(part) for part in parts)
    total = max(sum(len(part) for part in parts), 1)
    return largest / total


def _layout_has_left_right_split(slide_layout) -> bool:
    search_text = _layout_search_text(slide_layout)
    return any(
        keyword in search_text
        for keyword in {"split", "two-column", "two column", "left", "right", "dual"}
    )


def _layout_has_card_stack(slide_layout) -> bool:
    search_text = _layout_search_text(slide_layout)
    return any(keyword in search_text for keyword in {"card", "cards", "grid", "list"})


def _layout_balance_penalty(slide_layout, slide_text: str) -> int:
    density = _slide_text_density(slide_text)
    largest_ratio = _largest_section_ratio(slide_text)
    body_slot_count = _layout_body_slot_count(slide_layout)
    family = _infer_verbatim_slide_family(slide_text)
    penalty = 0

    if density > 900 and _layout_has_left_right_split(slide_layout) and body_slot_count >= 4:
        penalty += 90
    if density > 900 and _layout_has_card_stack(slide_layout) and body_slot_count >= 4:
        penalty += 70
    if largest_ratio > 0.58 and _layout_has_left_right_split(slide_layout):
        penalty += 110
    if largest_ratio > 0.7 and _layout_has_card_stack(slide_layout):
        penalty += 90
    if body_slot_count >= 5 and density > 760:
        penalty += 60
    if family == "dense-text" and _layout_has_left_right_split(slide_layout):
        penalty += 140
    if family == "dense-text" and _layout_has_card_stack(slide_layout):
        penalty += 120
    if family == "dense-text" and body_slot_count >= 3:
        penalty += 120
    if family == "bullet" and body_slot_count > 4:
        penalty += 80
    if family == "cover" and body_slot_count >= 2:
        penalty += 120

    return penalty


def _slide_text_density(slide_text: str) -> int:
    cleaned = _clean_verbatim_text(slide_text)
    lines = _non_empty_lines(cleaned)
    return len(cleaned or "") + (len(lines) * 35)


def _choose_verbatim_layout_index_for_slide(
    layout: PresentationLayoutModel,
    slide_text: str,
    previous_indexes: Optional[List[int]] = None,
    slide_meta: Optional[Dict[str, Any]] = None,
) -> int:
    slide_text = _clean_verbatim_text(slide_text)
    previous_indexes = previous_indexes or []
    keywords = _slide_layout_keywords(slide_text)
    family = _infer_verbatim_slide_family(slide_text)
    preferred_indexes = set(_preferred_verbatim_layout_indexes(layout, slide_text, slide_meta))
    allow_data_layout = _text_has_structured_data(slide_text)
    has_table_data = _text_has_table_data(slide_text)
    has_chart_data = _text_has_chart_data(slide_text)
    has_kpi_data = _text_has_kpi_data(slide_text)
    density = _slide_text_density(slide_text)
    numbered = _detect_numbered_structure(slide_text)
    intro_candidate = _is_intro_candidate(slide_text, slide_meta)

    scored_indexes = []
    for index, slide_layout in enumerate(layout.slides):
        summary_text = _layout_summary_text(slide_layout)
        search_text = _layout_search_text(slide_layout)
        score = _score_layout_for_verbatim(slide_layout)
        if any(keyword in search_text for keyword in {"quote", "testimonial", "team", "people", "contact"}):
            score -= 900
        score += sum(120 for keyword in keywords if keyword in summary_text)
        score += sum(30 for keyword in keywords if keyword in search_text)
        score += _layout_text_capacity_score(slide_layout) * 18
        if preferred_indexes:
            score += 320 if index in preferred_indexes else -240
        if intro_candidate:
            if any(keyword in search_text for keyword in {"intro", "cover", "hero", "statement", "section"}):
                score += 260
            if any(keyword in search_text for keyword in {"quote", "image", "team", "contact"}):
                score -= 260
        if numbered["numbered_count"] >= 3:
            if any(keyword in search_text for keyword in {"numbered", "bullet", "list", "step", "process", "timeline"}):
                score += 240
            if "basic" in search_text or "description" in search_text:
                score -= 120

        has_data_fields = _layout_has_data_fields(slide_layout)
        if has_data_fields and allow_data_layout:
            score += 35
        elif has_data_fields and not allow_data_layout:
            score -= 120

        is_table_layout = _layout_is_table_layout(slide_layout)
        is_chart_layout = _layout_is_chart_layout(slide_layout)
        is_kpi_layout = _layout_is_kpi_layout(slide_layout)
        is_compact_visual = any(keyword in search_text for keyword in COMPACT_VISUAL_LAYOUT_KEYWORDS)
        is_text_heavy = any(keyword in search_text for keyword in TEXT_HEAVY_LAYOUT_KEYWORDS)
        score -= _layout_balance_penalty(slide_layout, slide_text)

        if has_table_data and is_table_layout:
            score += 220
        if has_table_data and is_chart_layout and not is_table_layout:
            score -= 260
        if has_chart_data and is_chart_layout and density < 1000:
            score += 220
        if has_chart_data and not is_chart_layout and not is_kpi_layout:
            score -= 45
        if is_chart_layout and not has_chart_data and not has_table_data:
            score -= 240
        if is_kpi_layout and not has_kpi_data:
            score -= 260
        if has_kpi_data and is_kpi_layout and density < 900:
            score += 160
        if family == "comparison" and (
            "comparison" in search_text or "dual" in search_text or is_table_layout
        ):
            score += 150
        if family == "process" and any(
            keyword in search_text for keyword in {"process", "step", "steps", "timeline", "numbered"}
        ):
            score += 150

        if density > 850 and is_compact_visual:
            score -= 130
        if density > 850 and is_text_heavy:
            score += 90
        if density > 850 and (is_chart_layout or is_kpi_layout):
            score -= 170
        if density > 1200 and _layout_text_capacity_score(slide_layout) < 2:
            score -= 90
        if family == "dense-text" and _layout_body_slot_count(slide_layout) > 2:
            score -= 180
        if family == "dense-text" and _layout_has_left_right_split(slide_layout):
            score -= 220
        if family == "bullet" and _layout_body_slot_count(slide_layout) < 1:
            score -= 120
        if family == "cover" and _layout_text_capacity_score(slide_layout) > 2:
            score -= 140

        if previous_indexes and index == previous_indexes[-1]:
            score -= 45
        if len(previous_indexes) >= 2 and index == previous_indexes[-1] == previous_indexes[-2]:
            score -= 420
        score -= previous_indexes.count(index) * 15
        scored_indexes.append((score, index))

    scored_indexes.sort(reverse=True)
    return scored_indexes[0][1] if scored_indexes else _choose_verbatim_layout_index(layout)


def _choose_verbatim_layout_index(layout: PresentationLayoutModel) -> int:
    preferred_ids = PREFERRED_VERBATIM_LAYOUTS.get(layout.name, [])
    for preferred_id in preferred_ids:
        for index, slide_layout in enumerate(layout.slides):
            if slide_layout.id == preferred_id:
                return index
    return max(
        range(len(layout.slides)),
        key=lambda index: _score_layout_for_verbatim(layout.slides[index]),
    )


def _layout_unsuitable_for_verbatim(slide_layout, slide_text: str) -> bool:
    density = _slide_text_density(slide_text)
    largest_ratio = _largest_section_ratio(slide_text)
    body_slot_count = _layout_body_slot_count(slide_layout)
    family = _infer_verbatim_slide_family(slide_text)
    search_text = _layout_search_text(slide_layout)

    if any(keyword in search_text for keyword in {"quote", "testimonial", "team", "people", "contact"}):
        return True

    if _layout_is_chart_layout(slide_layout):
        if _text_has_table_data(slide_text) and not _layout_is_table_layout(slide_layout):
            return True
        if not _text_has_chart_data(slide_text) and not _layout_is_table_layout(slide_layout):
            return True
        if density > 950:
            return True

    if _layout_is_kpi_layout(slide_layout):
        if not _text_has_kpi_data(slide_text):
            return True
        if density > 950:
            return True

    if family == "cover":
        if body_slot_count >= 2:
            return True
        if _layout_has_card_stack(slide_layout):
            return True
    if family == "dense-text":
        if body_slot_count > 2:
            return True
        if _layout_has_left_right_split(slide_layout):
            return True
        if _layout_has_card_stack(slide_layout):
            return True
    if family == "bullet":
        if body_slot_count == 0:
            return True
        if _layout_has_left_right_split(slide_layout) and largest_ratio > 0.45:
            return True
    if density > 900 and _layout_has_left_right_split(slide_layout) and body_slot_count >= 4:
        return True
    if largest_ratio > 0.6 and _layout_has_left_right_split(slide_layout):
        return True
    if density > 780 and _layout_has_card_stack(slide_layout) and body_slot_count >= 5:
        return True

    return False


def _sanitize_verbatim_structure(
    structure: PresentationStructureModel,
    presentation_outline_model: PresentationOutlineModel,
    layout: PresentationLayoutModel,
) -> PresentationStructureModel:
    total_slide_layouts = len(layout.slides)
    planned_indexes: List[int] = []

    for index, outline in enumerate(presentation_outline_model.slides):
        deterministic_index = _choose_verbatim_layout_index_for_slide(
            layout,
            outline.content,
            planned_indexes,
        )
        planned_index = structure.slides[index] if index < len(structure.slides) else deterministic_index
        if planned_index < 0 or planned_index >= total_slide_layouts:
            planned_index = deterministic_index

        selected_layout = layout.slides[planned_index]
        if _layout_has_data_fields(selected_layout) and not _text_has_structured_data(outline.content):
            planned_index = deterministic_index
        elif _layout_unsuitable_for_verbatim(selected_layout, outline.content):
            planned_index = deterministic_index
        elif planned_indexes and planned_index == planned_indexes[-1]:
            planned_index = deterministic_index

        if _layout_unsuitable_for_verbatim(layout.slides[planned_index], outline.content):
            planned_index = _choose_verbatim_layout_index_for_slide(
                layout,
                outline.content,
                planned_indexes,
            )

        planned_indexes.append(planned_index)

    if not planned_indexes:
        planned_indexes = [
            _choose_verbatim_layout_index_for_slide(layout, outline.content, planned_indexes)
            for outline in presentation_outline_model.slides
        ]

    structure.slides = planned_indexes
    return structure


def _deterministic_verbatim_structure(
    presentation_outline_model: PresentationOutlineModel,
    layout: PresentationLayoutModel,
) -> PresentationStructureModel:
    planned_indexes: List[int] = []
    for outline in presentation_outline_model.slides:
        planned_indexes.append(
            _choose_verbatim_layout_index_for_slide(
                layout,
                outline.content,
                planned_indexes,
            )
        )
    return PresentationStructureModel(slides=planned_indexes)


def _layout_catalog_for_planner(
    layout: PresentationLayoutModel,
    allowed_indexes: Optional[set[int]] = None,
) -> List[Dict[str, Any]]:
    catalog = []
    for index, slide_layout in enumerate(layout.slides):
        if allowed_indexes is not None and index not in allowed_indexes:
            continue
        catalog.append(
            {
                "index": index,
                "id": slide_layout.id,
                "name": getattr(slide_layout, "name", "") or "",
                "description": (getattr(slide_layout, "description", "") or "")[:240],
                "has_table": _layout_is_table_layout(slide_layout),
                "has_chart": _layout_is_chart_layout(slide_layout),
                "has_kpi": _layout_is_kpi_layout(slide_layout),
                "text_capacity": _layout_text_capacity_score(slide_layout),
                "body_slots": _layout_body_slot_count(slide_layout),
            }
        )
    return catalog


def _slide_catalog_item(index: int, slide_text: str) -> Dict[str, Any]:
    text = _clean_verbatim_text(slide_text)
    return {
        "index": index,
        "family": _classify_verbatim_slide(text),
        "chars": len(text),
        "lines": len(_non_empty_lines(text)),
        "numeric_lines": _numeric_line_count(text),
        "preview": text[:900],
    }


def _extract_single_planner_index(
    response: Any,
    n_layouts: int,
    allowed_indexes: Optional[set[int]] = None,
) -> Optional[int]:
    if not isinstance(response, dict):
        return None
    raw_index = response.get("layout_index")
    if isinstance(raw_index, bool):
        return None
    if isinstance(raw_index, int):
        layout_index = raw_index
    elif isinstance(raw_index, float) and raw_index.is_integer():
        layout_index = int(raw_index)
    elif isinstance(raw_index, str) and re.fullmatch(r"\d+", raw_index.strip()):
        layout_index = int(raw_index.strip())
    else:
        return None
    if layout_index < 0 or layout_index >= n_layouts:
        return None
    if allowed_indexes is not None and layout_index not in allowed_indexes:
        return None
    return layout_index


def _is_complex_verbatim_slide(slide_text: str) -> bool:
    text = _clean_verbatim_text(slide_text)
    family = _classify_verbatim_slide(text)
    density = _slide_text_density(text)
    lines = _non_empty_lines(text)
    largest_ratio = _largest_section_ratio(text)

    if family == "cover":
        return density >= 120 or len(lines) > 2
    if family in {"timeline", "table", "kpi", "chart", "process", "bullets", "comparison"}:
        return True
    if family == "dense_text":
        return density >= 420 or len(lines) >= 4 or largest_ratio >= 0.38
    return density >= 260 or len(lines) >= 3


def _candidate_layout_indexes_for_slide(
    layout: PresentationLayoutModel,
    slide_text: str,
    previous_indexes: Optional[List[int]] = None,
    limit: int = 6,
) -> List[int]:
    previous_indexes = previous_indexes or []
    deterministic_index = _choose_verbatim_layout_index_for_slide(
        layout,
        slide_text,
        previous_indexes,
    )
    preferred_indexes = _preferred_verbatim_layout_indexes(layout, slide_text)
    scored_indexes: List[tuple[int, int]] = []
    for index, slide_layout in enumerate(layout.slides):
        if _layout_unsuitable_for_verbatim(slide_layout, slide_text):
            continue
        score = _score_layout_for_verbatim(slide_layout)
        score += _layout_text_capacity_score(slide_layout) * 18
        score -= _layout_balance_penalty(slide_layout, slide_text)
        if index == deterministic_index:
            score += 500
        if index in preferred_indexes:
            score += 220
        if previous_indexes and index == previous_indexes[-1]:
            score -= 60
        scored_indexes.append((score, index))

    scored_indexes.sort(reverse=True)
    ordered = [deterministic_index]
    for _, index in scored_indexes:
        if index not in ordered:
            ordered.append(index)
        if len(ordered) >= limit:
            break
    return ordered


async def _plan_complex_verbatim_slide(
    slide_text: str,
    slide_index: int,
    layout: PresentationLayoutModel,
    previous_indexes: Optional[List[int]] = None,
    instructions: Optional[str] = None,
) -> Optional[int]:
    previous_indexes = previous_indexes or []
    deterministic_index = _choose_verbatim_layout_index_for_slide(
        layout,
        slide_text,
        previous_indexes,
    )
    candidate_indexes = _candidate_layout_indexes_for_slide(
        layout,
        slide_text,
        previous_indexes,
    )
    allowed_indexes = set(candidate_indexes)
    if len(candidate_indexes) <= 1:
        return deterministic_index

    response_schema = {
        "type": "object",
        "properties": {
            "family": {"type": "string"},
            "layout_index": {"type": "integer"},
            "reason": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["family", "layout_index", "reason", "confidence"],
    }
    planner_payload = {
        "contract": "Return only one JSON object. Never rewrite slide text.",
        "canvas": "16:9 1280x720",
        "slide": _slide_catalog_item(slide_index, slide_text),
        "rules": [
            "Prefer layouts that visibly fill the slide without large empty decorative zones.",
            "Use chart layouts only for clear numeric series.",
            "Use KPI layouts only for short metric-heavy slides.",
            "Use dense text layouts for long academic text.",
            "Avoid repeating the same layout as the previous slide if a safe alternative exists.",
        ],
        "candidate_layouts": _layout_catalog_for_planner(layout, allowed_indexes),
        "deterministic_default": deterministic_index,
        "previous_layout_indexes": previous_indexes[-2:],
        "user_instructions": instructions or "",
    }

    try:
        print(
            "prepare_presentation_verbatim: per-slide LLM planner request "
            f"slide={slide_index} deterministic={deterministic_index} candidates={candidate_indexes}"
        )
        response = await asyncio.wait_for(
            LLMClient().generate_structured(
                model=get_model(),
                messages=[
                    LLMSystemMessage(
                        content=(
                            "You are a presentation slide layout planner. "
                            "Pick exactly one layout_index from candidate_layouts. "
                            "Do not rewrite or summarize slide text."
                        )
                    ),
                    LLMUserMessage(content=json.dumps(planner_payload, ensure_ascii=False)),
                ],
                response_format=response_schema,
                strict=True,
                max_tokens=260,
            ),
            timeout=PER_SLIDE_LLM_TIMEOUT_SECONDS,
        )
        selected_index = _extract_single_planner_index(
            response,
            len(layout.slides),
            allowed_indexes,
        )
        print(
            "prepare_presentation_verbatim: per-slide LLM planner response "
            f"slide={slide_index} selected={selected_index}"
        )
        return selected_index
    except Exception as exc:
        print(f"prepare_presentation_verbatim: per-slide LLM planner unavailable for slide {slide_index}: {exc}")
        return None


def _extract_planner_indexes(
    response: Any,
    n_slides: int,
    n_layouts: int,
) -> Optional[List[int]]:
    if not isinstance(response, dict):
        return None
    raw_slides = response.get("slides")
    if not isinstance(raw_slides, list) or len(raw_slides) < n_slides:
        return None

    indexes: List[int] = []
    for item in raw_slides[:n_slides]:
        raw_index = item.get("layout_index") if isinstance(item, dict) else item
        if isinstance(raw_index, bool):
            return None
        if isinstance(raw_index, int):
            layout_index = raw_index
        elif isinstance(raw_index, float) and raw_index.is_integer():
            layout_index = int(raw_index)
        elif isinstance(raw_index, str) and re.fullmatch(r"\d+", raw_index.strip()):
            layout_index = int(raw_index.strip())
        else:
            return None
        if layout_index < 0 or layout_index >= n_layouts:
            return None
        indexes.append(layout_index)
    return indexes


async def _generate_verbatim_presentation_structure(
    presentation_outline_model: PresentationOutlineModel,
    layout: PresentationLayoutModel,
    instructions: Optional[str] = None,
) -> PresentationStructureModel:
    if layout.ordered:
        return layout.to_presentation_structure()

    deterministic = _deterministic_verbatim_structure(presentation_outline_model, layout)
    planned_indexes: List[int] = []
    for slide_index, outline in enumerate(presentation_outline_model.slides):
        slide_text = _clean_verbatim_text(outline.content)
        deterministic_index = deterministic.slides[slide_index]
        planned_index = deterministic_index

        if _is_complex_verbatim_slide(slide_text):
            llm_index = await _plan_complex_verbatim_slide(
                slide_text=slide_text,
                slide_index=slide_index,
                layout=layout,
                previous_indexes=planned_indexes,
                instructions=instructions,
            )
            if llm_index is not None:
                planned_index = llm_index
                print(
                    "prepare_presentation_verbatim: using llm-planned layout "
                    f"slide={slide_index} layout_index={planned_index}"
                )
            else:
                print(
                    "prepare_presentation_verbatim: falling back to deterministic layout "
                    f"slide={slide_index} layout_index={deterministic_index}"
                )
        else:
            print(
                "prepare_presentation_verbatim: skipping llm for simple slide "
                f"slide={slide_index} layout_index={deterministic_index}"
            )

        planned_indexes.append(planned_index)

    structure = PresentationStructureModel(slides=planned_indexes)
    return _sanitize_verbatim_structure(structure, presentation_outline_model, layout)


def _extract_metric_parts(line: str) -> tuple[str, str, str, Optional[float]]:
    heading, body = _split_bullet_line(line)
    source = body or heading or line
    match = re.search(r"[-+]?\d[\d\s.,]*", source)
    if not match:
        return heading or source, source, "", None

    raw_number = match.group(0).strip()
    symbol = "%"
    if "%" not in source:
        symbol = ""
    normalized_number = raw_number.replace(" ", "").replace(",", ".")
    try:
        numeric_value = float(normalized_number)
    except ValueError:
        numeric_value = None

    label = heading or source.replace(raw_number, "").replace("%", "").strip(" :-")
    return label or source, raw_number, symbol, numeric_value


def _split_slide_text(text: str) -> tuple[str, str, List[str]]:
    lines = _non_empty_lines(text)
    if not lines:
        return "Untitled slide", "", []

    title = lines[0]
    body_lines = lines[1:]
    if len(title) > 140 and not body_lines:
        body_lines = [title]
        title = title[:137].rstrip() + "..."
    elif len(title) > 180:
        body_lines = [title, *body_lines]
        title = title[:137].rstrip() + "..."
    body = "\n".join(body_lines).strip()
    return title, body, body_lines


def _split_dense_paragraphs(text: str) -> List[str]:
    parts = [part.strip() for part in re.split(r"\n{2,}", text or "") if part.strip()]
    if parts:
        return parts
    return _non_empty_lines(text)


def _looks_like_list_slide(text: str) -> bool:
    lines = _non_empty_lines(text)
    list_lines = [
        line
        for line in lines
        if re.match(r"^(\d+[\).\s]|[-*•])", line)
    ]
    return len(list_lines) >= 4


def _chunk_list_lines(lines: List[str], max_chunk_lines: int = 4) -> List[List[str]]:
    chunks: List[List[str]] = []
    current: List[str] = []
    for line in lines:
        if len(current) >= max_chunk_lines and re.match(r"^(\d+[\).\s]|[-*•])", line):
            chunks.append(current)
            current = []
        current.append(line)
    if current:
        chunks.append(current)
    return chunks


def _chunk_text_parts(parts: List[str], max_chars: int = 520) -> List[List[str]]:
    chunks: List[List[str]] = []
    current: List[str] = []
    current_length = 0

    for part in parts:
        part_length = len(part)
        if current and current_length + part_length > max_chars:
            chunks.append(current)
            current = []
            current_length = 0
        current.append(part)
        current_length += part_length + 2

    if current:
        chunks.append(current)
    return chunks


def _chunk_list_lines_for_import(lines: List[str], max_chunk_lines: int = 4) -> List[List[str]]:
    chunks: List[List[str]] = []
    current: List[str] = []
    current_chars = 0
    for line in lines:
        line_length = len(line.strip())
        starts_new_item = bool(re.match(r"^(\d+[\).\s]|[-*вЂў])", line))
        if current and (
            (len(current) >= max_chunk_lines and starts_new_item)
            or (current_chars + line_length > 320 and starts_new_item)
        ):
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(line)
        current_chars += line_length
    if current:
        chunks.append(current)
    return chunks


def _looks_like_list_slide(text: str) -> bool:
    lines = _non_empty_lines(text)
    list_lines = [line for line in lines if BULLET_PREFIX_RE.match(line)]
    return len(list_lines) >= 4


def _chunk_list_lines_for_import(lines: List[str], max_chunk_lines: int = 4) -> List[List[str]]:
    chunks: List[List[str]] = []
    current: List[str] = []
    current_chars = 0
    for line in lines:
        line_length = len(line.strip())
        starts_new_item = bool(BULLET_PREFIX_RE.match(line))
        if current and (
            (len(current) >= max_chunk_lines and starts_new_item)
            or (current_chars + line_length > 320 and starts_new_item)
        ):
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(line)
        current_chars += line_length
    if current:
        chunks.append(current)
    return chunks


def _split_table_slide_text(text: str, max_rows_per_slide: int = 12) -> List[str]:
    lines = _non_empty_lines(text)
    if not lines:
        return [text]

    first_line_is_table = "|" in lines[0] or "\t" in lines[0]
    title = "" if first_line_is_table else lines[0]
    body_lines = lines if first_line_is_table else lines[1:]
    intro_lines = [line for line in body_lines if "|" not in line and "\t" not in line]
    table_lines = [line for line in body_lines if "|" in line or "\t" in line]

    if len(table_lines) <= max_rows_per_slide:
        return [text]

    header = table_lines[0]
    data_rows = table_lines[1:]
    rows_per_chunk = max(max_rows_per_slide - 1, 1)
    chunks: List[str] = []

    for chunk_index in range(0, len(data_rows), rows_per_chunk):
        chunk_rows = data_rows[chunk_index: chunk_index + rows_per_chunk]
        chunk_lines: List[str] = []
        if title:
            chunk_lines.extend([title, ""])
        if intro_lines and chunk_index == 0:
            chunk_lines.extend([*intro_lines, ""])
        chunk_lines.extend([header, *chunk_rows])
        chunks.append("\n".join(chunk_lines).strip())

    return chunks or [text]


def _split_verbatim_slide_text(text: str) -> List[str]:
    text = _clean_verbatim_text(text)
    if _text_has_table_data(text):
        return _split_table_slide_text(text)
    if _text_has_chart_data(text) and len(_non_empty_lines(text)) <= 8:
        return [text]

    title, body, body_lines = _split_slide_text(text)
    numbered = _detect_numbered_structure(text)
    roadmap_stage_count = len([line for line in _non_empty_lines(text) if ROADMAP_HEADING_RE.match(line)])
    if _text_has_roadmap_data(text):
        if roadmap_stage_count <= 3 and len(_non_empty_lines(text)) <= 14:
            return [text]
        chunks = _chunk_list_lines_for_import(body_lines, max_chunk_lines=3)
        normalized_chunks = [
            "\n".join([title, "", *chunk]).strip()
            for chunk in chunks
            if chunk
        ]
        return normalized_chunks or [text]
    if numbered["numbered_count"] >= 4 and len(text) > 600:
        chunks = _chunk_list_lines_for_import(body_lines, max_chunk_lines=3)
        normalized_chunks = [
            "\n".join([title, "", *chunk]).strip()
            for chunk in chunks
            if chunk
        ]
        if len(normalized_chunks) >= 2:
            return normalized_chunks

    density = _slide_text_density(text)
    if density < 1700 and len(body_lines) <= 14:
        return [text]

    chunks: List[List[str]]
    if _looks_like_list_slide(body):
        chunks = _chunk_list_lines_for_import(body_lines, max_chunk_lines=7)
    else:
        parts = _split_dense_paragraphs(body)
        chunks = _chunk_text_parts(parts, max_chars=850 if density < 2300 else 720)

    normalized_chunks = [
        "\n".join([title, "", *chunk]).strip()
        for chunk in chunks
        if chunk
    ]
    return normalized_chunks or [text]


def _build_hybrid_verbatim_outline_items(
    outlines: List[SlideOutlineModel],
    pptx_slides: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    pptx_slides = pptx_slides or []
    for source_index, outline in enumerate(outlines):
        source_text = _clean_verbatim_text(outline.content)
        source_meta = (
            pptx_slides[source_index]
            if source_index < len(pptx_slides) and isinstance(pptx_slides[source_index], dict)
            else {}
        )
        split_chunks = _split_verbatim_slide_text(source_text)
        total_parts = len(split_chunks)
        for part_index, chunk in enumerate(split_chunks):
            items.append(
                {
                    "content": chunk,
                    "source_text": source_text,
                    "source_index": source_index,
                    "source_slide_number": int(source_meta.get("slide_number") or source_index + 1),
                    "part_index": part_index,
                    "part_count": total_parts,
                    "source_meta": source_meta,
                }
            )
    return items


def _split_bullet_line(line: str) -> tuple[str, str]:
    cleaned = line.strip().lstrip("-•*0123456789. )\t").strip()
    if ":" in cleaned:
        heading, body = cleaned.split(":", 1)
        return heading.strip(), body.strip()
    return cleaned, ""


def _split_bullet_line(line: str) -> tuple[str, str]:
    cleaned = BULLET_PREFIX_RE.sub("", line.strip()).strip()
    if ":" in cleaned:
        heading, body = cleaned.split(":", 1)
        return heading.strip(), body.strip()
    return cleaned, ""


def _text_field_keys(properties: dict) -> List[str]:
    return [
        key
        for key, value_schema in properties.items()
        if isinstance(value_schema, dict)
        and _schema_type(value_schema) == "string"
        and (_is_body_key(key) or _is_text_key(key))
    ]


def _split_text_for_fields(text: str, field_count: int) -> List[str]:
    if field_count <= 1:
        return [text]

    paragraphs = [part.strip() for part in re.split(r"\n{2,}", text or "") if part.strip()]
    if len(paragraphs) < field_count:
        paragraphs = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not paragraphs:
        return [""] * field_count

    chunks = [""] * field_count
    for index, paragraph in enumerate(paragraphs):
        target = min(index * field_count // len(paragraphs), field_count - 1)
        chunks[target] = f"{chunks[target]}\n{paragraph}".strip()
    return chunks


def _split_table_row(line: str) -> List[str]:
    separator = "|" if "|" in line else "\t"
    return [_clean_verbatim_text(cell) for cell in line.split(separator) if cell.strip()]


def _table_rows_from_slide_meta(slide_meta: Optional[Dict[str, Any]]) -> List[List[str]]:
    if not isinstance(slide_meta, dict):
        return []

    rows: List[List[str]] = []
    for block in slide_meta.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        block_rows = block.get("table_rows")
        if isinstance(block_rows, list):
            for row in block_rows:
                if isinstance(row, list):
                    cells = [_clean_verbatim_text(str(cell)) for cell in row if str(cell).strip()]
                    if cells:
                        rows.append(cells)
        elif block.get("kind") == "table":
            rows.extend(_table_rows_from_text(_stringify_content_value(block.get("text"))))
    return rows


def _table_rows_from_text(text: str) -> List[List[str]]:
    table_lines = [
        line
        for line in _non_empty_lines(text)
        if "|" in line or "\t" in line
    ]
    rows = [_split_table_row(line) for line in table_lines]
    return [row for row in rows if row]


def _extract_table_from_text(
    text: str,
    table_schema: dict,
    slide_meta: Optional[Dict[str, Any]] = None,
) -> dict:
    fallback = {"columns": [], "rows": []}
    rows = _table_rows_from_slide_meta(slide_meta) or _table_rows_from_text(text)
    if not rows:
        return fallback

    max_columns = (
        table_schema.get("properties", {})
        .get("columns", {})
        .get("maxItems", 3)
    )
    max_rows = (
        table_schema.get("properties", {})
        .get("rows", {})
        .get("maxItems", 3)
    )
    columns = rows[0][:max_columns]
    data_rows = rows[1:] if len(rows) > 1 else rows
    normalized_rows = [
        (row[:max_columns] + [""] * max(0, len(columns) - len(row[:max_columns])))
        for row in data_rows[:max_rows]
    ]
    return {
        "columns": columns,
        "rows": normalized_rows,
    }


def _parse_numeric_value_from_text(value: Any) -> Optional[float]:
    source = _clean_verbatim_text(_stringify_content_value(value))
    match = re.search(r"[-+]?\d[\d\s.,]*", source)
    if not match:
        return None
    normalized = match.group(0).replace(" ", "").replace(",", ".")
    try:
        return float(normalized)
    except ValueError:
        return None


def _numeric_points_from_text(
    text: str,
    slide_meta: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    points: List[Dict[str, Any]] = []

    if isinstance(slide_meta, dict):
        for item in slide_meta.get("numeric_series") or []:
            if not isinstance(item, dict):
                continue
            value = _parse_numeric_value_from_text(item.get("value"))
            label = _clean_verbatim_text(
                _stringify_content_value(item.get("label") or item.get("raw"))
            )
            if value is not None and label:
                points.append({"label": label[:64], "value": value, "raw": label})

    rows = _table_rows_from_slide_meta(slide_meta) or _table_rows_from_text(text)
    for row in rows[1:] if len(rows) > 1 else rows:
        if len(row) < 2:
            continue
        numeric_cell = next((cell for cell in row[1:] if _parse_numeric_value_from_text(cell) is not None), None)
        if numeric_cell is None:
            continue
        value = _parse_numeric_value_from_text(numeric_cell)
        label = _clean_verbatim_text(row[0])
        if value is not None and label:
            points.append({"label": label[:64], "value": value, "raw": " | ".join(row)})

    for line in _short_metric_lines(text):
        label, number, symbol, numeric_value = _extract_metric_parts(line)
        if numeric_value is None:
            continue
        display_label = _clean_verbatim_text(label or line)
        points.append(
            {
                "label": display_label[:64],
                "value": numeric_value,
                "raw": line,
                "displayValue": f"{number}{symbol}".strip(),
            }
        )

    unique_points: List[Dict[str, Any]] = []
    seen = set()
    for point in points:
        key = (point.get("label"), point.get("value"))
        if key in seen:
            continue
        seen.add(key)
        unique_points.append(point)
    return unique_points[:12]


def _fill_chart_point(item_schema: dict, point: Dict[str, Any], index: int) -> Any:
    item_type = _schema_type(item_schema)
    if item_type in {"number", "integer"}:
        return point["value"]
    if item_type == "string":
        return point["label"]

    item = _schema_default(item_schema)
    if not isinstance(item, dict):
        item = {}
    for key, value_schema in (item_schema.get("properties", {}) or {}).items():
        value_type = _schema_type(value_schema)
        normalized_key = _normalized_key(key)
        if value_type == "string":
            if normalized_key in {"label", "name", "category", "x", "axislabel"} or _is_title_key(key):
                item[key] = point["label"]
            elif normalized_key in {"displayvalue", "formattedvalue", "raw"}:
                item[key] = _stringify_content_value(point.get("displayValue") or point.get("raw"))
            else:
                item[key] = point["label"]
        elif value_type in {"number", "integer"}:
            if normalized_key in {"index", "order"}:
                item[key] = index + 1
            else:
                item[key] = point["value"]
        elif value_type == "boolean":
            item[key] = index == 0
    return item


def _fill_chart_array_items(items_schema: dict, points: List[Dict[str, Any]], array_key: str) -> list:
    max_items = items_schema.get("maxItems") or len(points)
    item_schema = items_schema.get("items", {})
    normalized_key = _normalized_key(array_key)
    selected_points = points[:max_items]

    if "series" in normalized_key or "dataset" in normalized_key:
        item_schema = item_schema if isinstance(item_schema, dict) else {}
        series_item = _schema_default(item_schema)
        if not isinstance(series_item, dict):
            series_item = {}
        properties = item_schema.get("properties", {}) if isinstance(item_schema, dict) else {}
        for key, value_schema in properties.items():
            value_type = _schema_type(value_schema)
            normalized_child_key = _normalized_key(key)
            if value_type == "string":
                series_item[key] = "Value"
            elif value_type == "array":
                if "label" in normalized_child_key or "categor" in normalized_child_key:
                    series_item[key] = [point["label"] for point in selected_points]
                else:
                    series_item[key] = [point["value"] for point in selected_points]
        return [series_item] if series_item else [{"name": "Value", "values": [point["value"] for point in selected_points]}]

    return [
        _fill_chart_point(item_schema, point, index)
        for index, point in enumerate(selected_points)
    ]


def _extract_chart_from_text(
    text: str,
    chart_schema: dict,
    slide_meta: Optional[Dict[str, Any]] = None,
) -> dict:
    points = _numeric_points_from_text(text, slide_meta)
    fallback = {
        "title": _split_slide_text(text)[0],
        "type": "bar",
        "categories": [point["label"] for point in points],
        "series": [{"name": "Value", "values": [point["value"] for point in points]}],
        "data": [
            {"label": point["label"], "value": point["value"]}
            for point in points
        ],
        "showLabels": True,
    }
    if not isinstance(chart_schema, dict) or not chart_schema.get("properties"):
        return fallback

    chart = _schema_default(chart_schema)
    if not isinstance(chart, dict):
        chart = {}

    title = _split_slide_text(text)[0]
    labels = [point["label"] for point in points]
    values = [point["value"] for point in points]
    for key, value_schema in (chart_schema.get("properties", {}) or {}).items():
        value_type = _schema_type(value_schema)
        normalized_key = _normalized_key(key)
        if value_type == "string":
            if normalized_key in {"type", "charttype", "kind", "variant"}:
                chart[key] = "bar"
            elif _is_title_key(key) or normalized_key in {"caption", "name"}:
                chart[key] = title
            elif "axis" in normalized_key and "x" in normalized_key:
                chart[key] = "Category"
            elif "axis" in normalized_key and "y" in normalized_key:
                chart[key] = "Value"
        elif value_type == "array":
            if "categor" in normalized_key or "label" in normalized_key:
                chart[key] = labels
            elif "value" in normalized_key:
                chart[key] = values
            elif "series" in normalized_key or "dataset" in normalized_key or "data" in normalized_key or "point" in normalized_key:
                chart[key] = _fill_chart_array_items(value_schema, points, key)
        elif value_type == "boolean":
            chart[key] = True
        elif value_type in {"number", "integer"}:
            chart[key] = max(values) if values else 0

    for key, value in fallback.items():
        chart.setdefault(key, value)
    return chart


def _extract_amount_hint(line: str) -> str:
    match = re.search(r"\b\d[\d\s.,]*(?:\s*(?:млн|тыс|руб|₽|%|k|m|bn))\b", line, re.IGNORECASE)
    return match.group(0).strip() if match else ""


def _extract_date_hint(line: str) -> str:
    match = re.search(r"20\d{2}\s*[–—-]\s*20\d{2}|20\d{2}|Q[1-4]\s*20\d{2}", line, re.IGNORECASE)
    return match.group(0).strip() if match else ""


def _parse_timeline_items(text: str) -> List[Dict[str, Any]]:
    title, body, body_lines = _split_slide_text(text)
    lines = body_lines or _non_empty_lines(body or title)
    items: List[Dict[str, Any]] = []

    for line in lines:
        cleaned = _clean_verbatim_text(line)
        if re.match(r"^\s*\d{1,2}\s*$", cleaned):
            continue
        if re.match(r"^\s*(?:итого|total)\s*:?\s*$", cleaned, re.IGNORECASE):
            continue
        if re.match(r"^\s*20\d{2}\s*[:\-–—]\s*", cleaned):
            continue
        is_new_stage = bool(ROADMAP_HEADING_RE.match(cleaned) or _extract_date_hint(cleaned))
        order_match = re.match(r"^\s*(?:этап|шаг|stage|step|phase)\s*(\d+)", cleaned, re.IGNORECASE)
        if is_new_stage or not items:
            heading, details = _split_bullet_line(cleaned)
            items.append(
                {
                    "marker": _extract_date_hint(cleaned) or f"Step {len(items) + 1}",
                    "heading": heading or cleaned,
                    "details": [details] if details else [],
                    "amount": _extract_amount_hint(cleaned),
                    "dateHint": _extract_date_hint(cleaned),
                    "order": int(order_match.group(1)) if order_match else 9999,
                }
            )
        else:
            items[-1]["details"].append(cleaned)

    items.sort(key=lambda item: item.get("order", 9999))
    return items[:8]


def _fill_timeline_array_items(items_schema: dict, slide_text: str, array_key: str) -> list:
    timeline_items = _parse_timeline_items(slide_text)
    if not timeline_items:
        return []

    max_items = items_schema.get("maxItems") or min(len(timeline_items), 8)
    item_schema = items_schema.get("items", {})
    item_properties = item_schema.get("properties", {}) if isinstance(item_schema, dict) else {}
    items = []
    for index, source_item in enumerate(timeline_items[:max_items]):
        item = _schema_default(item_schema)
        if not isinstance(item, dict):
            item = {}
        for key, value_schema in item_properties.items():
            value_type = _schema_type(value_schema)
            normalized_key = _normalized_key(key)
            details_text = "\n".join(source_item.get("details") or [])
            if value_type == "string":
                if normalized_key in {"marker", "step", "phase", "stage", "date", "year", "period"}:
                    item[key] = source_item.get("marker") or source_item.get("dateHint") or f"{index + 1}"
                elif _is_title_key(key) or normalized_key in {"heading", "label", "name"}:
                    item[key] = source_item.get("heading") or f"Step {index + 1}"
                elif normalized_key in {"amount", "budget", "metric"}:
                    item[key] = source_item.get("amount") or ""
                elif _is_body_key(key) or _is_text_key(key):
                    item[key] = details_text or source_item.get("heading") or ""
            elif value_type == "array":
                item[key] = source_item.get("details") or []
            elif value_type in {"number", "integer"}:
                item[key] = index + 1
            elif value_type == "boolean":
                item[key] = index == 0
        items.append(item)
    return items


def _fill_kpi_items(items_schema: dict, slide_text: str) -> list:
    metric_lines = _short_metric_lines(slide_text)
    if not metric_lines:
        return []

    max_items = items_schema.get("maxItems") or min(len(metric_lines), 8)
    item_schema = items_schema.get("items", {})
    item_properties = item_schema.get("properties", {}) if isinstance(item_schema, dict) else {}
    items = []

    for index, line in enumerate(metric_lines[:max_items]):
        label, number, symbol, numeric_value = _extract_metric_parts(line)
        item = _schema_default(item_schema)
        if not isinstance(item, dict):
            item = {}
        for key, value_schema in item_properties.items():
            value_type = _schema_type(value_schema)
            normalized_key = _normalized_key(key)
            if value_type == "string":
                if normalized_key in {"toplabel", "value", "number", "amount", "count"}:
                    item[key] = number or label
                elif normalized_key in {"topsuffix", "suffix", "unit", "symbol"}:
                    item[key] = symbol
                elif normalized_key in {"bottomlabel", "label", "caption", "name"}:
                    item[key] = label or line
                elif _is_text_key(key):
                    item[key] = line
            elif value_type in {"number", "integer"}:
                item[key] = numeric_value if numeric_value is not None else 0
            elif value_type == "boolean":
                item[key] = index == 0 and normalized_key == "ishighlighted"
        items.append(item)

    return items


def _split_numbered_verbatim_sections(lines: List[str]) -> List[Dict[str, str]]:
    sections: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    for raw_line in lines:
        line = _clean_verbatim_text(raw_line)
        if not line:
            continue

        if re.match(BULLET_MARKER_PATTERN, line):
            heading, details = _split_bullet_line(line)
            current = {
                "heading": heading or line,
                "details": [details] if details else [],
            }
            sections.append(current)
            continue

        if current:
            current["details"].append(line)

    grouped = []
    for section in sections:
        heading = _clean_verbatim_text(section.get("heading", ""))
        details = [
            _clean_verbatim_text(detail)
            for detail in section.get("details", [])
            if _clean_verbatim_text(detail)
        ]
        if heading and (details or len(sections) >= 2):
            grouped.append({"heading": heading, "body": "\n".join(details)})

    return grouped


def _fill_array_items(
    items_schema: dict,
    lines: List[str],
    array_key: str = "",
    slide_text: str = "",
) -> list:
    normalized_array_key = _normalized_key(array_key)
    source_text = slide_text or "\n".join(lines)

    if "kpi" in normalized_array_key or "metric" in normalized_array_key:
        metric_items = _fill_kpi_items(items_schema, source_text)
        if metric_items:
            return metric_items

    if any(token in normalized_array_key for token in ("timeline", "roadmap", "stage", "phase", "step", "process")):
        timeline_items = _fill_timeline_array_items(items_schema, source_text, array_key)
        if timeline_items:
            return timeline_items

    if any(token in normalized_array_key for token in ("chart", "graph", "series", "dataset", "datapoint", "points")):
        points = _numeric_points_from_text(source_text)
        if points:
            return _fill_chart_array_items(items_schema, points, array_key)

    max_items = items_schema.get("maxItems") or min(max(len(lines), 1), 6)
    item_schema = items_schema.get("items", {})
    item_type = _schema_type(item_schema)
    item_properties = item_schema.get("properties", {}) if isinstance(item_schema, dict) else {}
    numbered_sections = _split_numbered_verbatim_sections(lines)
    selected_sections: List[Dict[str, str]] = []
    overflow_lines: List[str] = []

    if len(numbered_sections) >= 2:
        selected_sections = numbered_sections[:max_items]
        selected_lines = [
            "\n".join([section["heading"], section["body"]]).strip()
            for section in selected_sections
        ]
        overflow_sections = numbered_sections[max_items:]
        if overflow_sections and selected_lines:
            selected_lines[-1] = "\n".join(
                [
                    selected_lines[-1],
                    *[
                        "\n".join([section["heading"], section["body"]]).strip()
                        for section in overflow_sections
                    ],
                ]
            ).strip()
    else:
        selected_lines = lines[:max_items] if lines else [""]
        overflow_lines = lines[max_items:] if lines and len(lines) > max_items else []
    items = []

    for line_index, line in enumerate(selected_lines):
        if overflow_lines and line_index == len(selected_lines) - 1:
            line = "\n".join([line, *overflow_lines])
        if item_type == "string":
            items.append(line)
            continue

        if selected_sections:
            section = selected_sections[min(line_index, len(selected_sections) - 1)]
            heading = section["heading"]
            body = section["body"]
        else:
            heading, body = _split_bullet_line(line)
        metric_label, metric_number, metric_symbol, metric_value = _extract_metric_parts(line)
        item = _schema_default(item_schema)
        if not isinstance(item, dict):
            item = {}
        for key, value_schema in item_properties.items():
            if not isinstance(value_schema, dict):
                continue
            value_type = _schema_type(value_schema)
            if value_type == "string":
                if _is_title_key(key):
                    item[key] = heading or body or line
                elif _is_metric_value_key(key) and metric_number:
                    item[key] = metric_number
                elif _is_metric_symbol_key(key):
                    item[key] = metric_symbol
                elif _is_metric_label_key(key):
                    item[key] = metric_label or heading or body or line
                elif _is_body_key(key) or _is_text_key(key):
                    item[key] = body or heading or line
            elif value_type in {"number", "integer"} and _is_metric_value_key(key):
                item[key] = metric_value if metric_value is not None else item.get(key, 0)
            elif value_type == "object":
                item.setdefault(key, _schema_default(value_schema))
        items.append(item)

    return items


def _verbatim_density(slide_text: str) -> str:
    length = len(slide_text or "")
    line_count = len([line for line in (slide_text or "").splitlines() if line.strip()])
    if length > 900 or line_count > 10:
        return "dense"
    if length > 520 or line_count > 6:
        return "medium"
    return "normal"


_IMAGE_SLOT_KEYS = {"__image_url__", "__image_prompt__"}
_ICON_SLOT_KEYS = {"__icon_url__", "__icon_query__"}


def _is_image_generation_disabled() -> bool:
    flag = os.getenv("DISABLE_IMAGE_GENERATION", "")
    return str(flag).strip().lower() in {"1", "true", "yes", "on"}


def _strip_image_fields_from_content(value: Any) -> Any:
    """Recursively clear image/icon URL slots so nothing remains pointing at
    unsplash/S3 defaults when DISABLE_IMAGE_GENERATION is on."""
    if isinstance(value, dict):
        keys = set(value.keys())
        if keys & _IMAGE_SLOT_KEYS:
            return {"__image_url__": "", "__image_prompt__": ""}
        if keys & _ICON_SLOT_KEYS:
            return {"__icon_url__": "", "__icon_query__": ""}
        return {k: _strip_image_fields_from_content(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_image_fields_from_content(item) for item in value]
    return value


def _clear_schema_string_defaults(schema: Any, content: Any) -> None:
    """Zero out string fields whose current value still equals the Zod schema
    default (Winston Churchill quote, unsplash prompts, Market Comparison etc.).
    Runs in-place. Arrays/objects are walked recursively."""
    if not isinstance(schema, dict) or not isinstance(content, dict):
        return
    properties = schema.get("properties") or {}
    for key, sub_schema in properties.items():
        if not isinstance(sub_schema, dict):
            continue
        sub_type = _schema_type(sub_schema)
        default = sub_schema.get("default")
        current = content.get(key)
        if sub_type == "string":
            if isinstance(default, str) and default and current == default:
                content[key] = ""
        elif sub_type == "object" and isinstance(current, dict):
            _clear_schema_string_defaults(sub_schema, current)
        elif sub_type == "array" and isinstance(current, list):
            item_schema = sub_schema.get("items") or {}
            for item in current:
                if isinstance(item, dict):
                    _clear_schema_string_defaults(item_schema, item)


def _count_text_slots(schema: Any) -> int:
    if not isinstance(schema, dict):
        return 0
    properties = schema.get("properties") or {}
    count = 0
    for key, sub_schema in properties.items():
        if not isinstance(sub_schema, dict):
            continue
        sub_type = _schema_type(sub_schema)
        if sub_type == "string" and (_is_title_key(key) or _is_body_key(key) or _is_text_key(key)):
            count += 1
        elif sub_type == "object":
            count += _count_text_slots(sub_schema)
        elif sub_type == "array":
            item_schema = sub_schema.get("items") or {}
            if isinstance(item_schema, dict):
                count += max(1, _count_text_slots(item_schema))
    return count


def _count_filled_text_slots(schema: Any, content: Any) -> int:
    if not isinstance(schema, dict) or not isinstance(content, dict):
        return 0
    properties = schema.get("properties") or {}
    count = 0
    for key, sub_schema in properties.items():
        if not isinstance(sub_schema, dict):
            continue
        sub_type = _schema_type(sub_schema)
        current = content.get(key)
        if sub_type == "string" and (_is_title_key(key) or _is_body_key(key) or _is_text_key(key)):
            if isinstance(current, str) and current.strip():
                count += 1
        elif sub_type == "object" and isinstance(current, dict):
            count += _count_filled_text_slots(sub_schema, current)
        elif sub_type == "array" and isinstance(current, list) and current:
            item_schema = sub_schema.get("items") or {}
            for item in current:
                if isinstance(item, str) and item.strip():
                    count += 1
                elif isinstance(item, dict):
                    count += _count_filled_text_slots(item_schema, item)
    return count


def _has_meaningful_chart_payload(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    categories = value.get("categories")
    if isinstance(categories, list) and any(str(item).strip() for item in categories):
        return True
    values = value.get("values")
    if isinstance(values, list) and any(item not in (None, "", 0, 0.0) for item in values):
        return True
    data = value.get("data")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("value") not in (None, "", 0, 0.0):
                return True
    series = value.get("series")
    if isinstance(series, list):
        for item in series:
            if not isinstance(item, dict):
                continue
            series_values = item.get("values")
            if isinstance(series_values, list) and any(
                point not in (None, "", 0, 0.0) for point in series_values
            ):
                return True
    return False


def _has_meaningful_table_payload(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    columns = value.get("columns")
    rows = value.get("rows")
    has_columns = isinstance(columns, list) and any(str(item).strip() for item in columns)
    has_rows = isinstance(rows, list) and any(
        isinstance(row, list) and any(str(cell).strip() for cell in row)
        for row in rows
    )
    return has_columns and has_rows


def _has_meaningful_kpi_payload(value: Any) -> bool:
    if isinstance(value, list):
        return any(
            isinstance(item, dict) and any(str(v).strip() for v in item.values())
            for item in value
        )
    if isinstance(value, dict):
        return any(
            str(v).strip() for v in value.values() if isinstance(v, (str, int, float))
        )
    return False


def _schema_requires_structured_payload(schema: Any) -> Dict[str, bool]:
    if not isinstance(schema, dict):
        return {"chart": False, "table": False, "kpi": False}
    properties = schema.get("properties") or {}
    required = {"chart": False, "table": False, "kpi": False}
    for key, sub_schema in properties.items():
        if not isinstance(sub_schema, dict):
            continue
        normalized_key = _normalized_key(key)
        sub_type = _schema_type(sub_schema)
        if sub_type == "object":
            if "chart" in normalized_key or "graph" in normalized_key:
                required["chart"] = True
            if "table" in normalized_key:
                required["table"] = True
            child_required = _schema_requires_structured_payload(sub_schema)
            for family, family_required in child_required.items():
                required[family] = required[family] or family_required
        elif sub_type == "array":
            if any(token in normalized_key for token in ("kpi", "metric", "stats", "statistics")):
                required["kpi"] = True
            item_schema = sub_schema.get("items") or {}
            child_required = _schema_requires_structured_payload(item_schema)
            for family, family_required in child_required.items():
                required[family] = required[family] or family_required
    return required


def _has_required_structured_payload(schema: Any, content: Any) -> bool:
    if not isinstance(content, dict):
        return True
    required = _schema_requires_structured_payload(schema)
    if not any(required.values()):
        return True

    def _walk(schema_node: Any, content_node: Any) -> Dict[str, bool]:
        found = {"chart": False, "table": False, "kpi": False}
        if not isinstance(schema_node, dict) or not isinstance(content_node, dict):
            return found
        properties = schema_node.get("properties") or {}
        for key, sub_schema in properties.items():
            if not isinstance(sub_schema, dict):
                continue
            normalized_key = _normalized_key(key)
            sub_type = _schema_type(sub_schema)
            current = content_node.get(key)
            if sub_type == "object":
                if ("chart" in normalized_key or "graph" in normalized_key) and _has_meaningful_chart_payload(current):
                    found["chart"] = True
                if "table" in normalized_key and _has_meaningful_table_payload(current):
                    found["table"] = True
                child_found = _walk(sub_schema, current)
                for family, value in child_found.items():
                    found[family] = found[family] or value
            elif sub_type == "array":
                if any(token in normalized_key for token in ("kpi", "metric", "stats", "statistics")) and _has_meaningful_kpi_payload(current):
                    found["kpi"] = True
                item_schema = sub_schema.get("items") or {}
                if isinstance(current, list):
                    for item in current:
                        if isinstance(item, dict):
                            child_found = _walk(item_schema, item)
                            for family, value in child_found.items():
                                found[family] = found[family] or value
        return found

    available = _walk(schema, content)
    return all(
        not is_required or available.get(family, False)
        for family, is_required in required.items()
    )


def _content_has_meaningful_chart_payload(content: Any) -> bool:
    if isinstance(content, dict):
        for key, value in content.items():
            normalized_key = _normalized_key(key)
            if ("chart" in normalized_key or "graph" in normalized_key) and _has_meaningful_chart_payload(value):
                return True
            if _content_has_meaningful_chart_payload(value):
                return True
    elif isinstance(content, list):
        return any(_content_has_meaningful_chart_payload(item) for item in content)
    return False


def _content_has_meaningful_table_payload(content: Any) -> bool:
    if isinstance(content, dict):
        for key, value in content.items():
            normalized_key = _normalized_key(key)
            if "table" in normalized_key and _has_meaningful_table_payload(value):
                return True
            if _content_has_meaningful_table_payload(value):
                return True
    elif isinstance(content, list):
        return any(_content_has_meaningful_table_payload(item) for item in content)
    return False


def _content_has_meaningful_kpi_payload(content: Any) -> bool:
    if isinstance(content, dict):
        for key, value in content.items():
            normalized_key = _normalized_key(key)
            if any(token in normalized_key for token in ("kpi", "metric", "stats", "statistics")) and _has_meaningful_kpi_payload(value):
                return True
            if _content_has_meaningful_kpi_payload(value):
                return True
    elif isinstance(content, list):
        if _has_meaningful_kpi_payload(content):
            return True
        return any(_content_has_meaningful_kpi_payload(item) for item in content)
    return False


def _should_use_template_first(schema: Any, content: Dict[str, Any], slide_text: str) -> Tuple[bool, float]:
    total_slots = max(_count_text_slots(schema), 1)
    filled_slots = _count_filled_text_slots(schema, content)
    fill_ratio = min(filled_slots / total_slots, 1.0)
    family = _infer_verbatim_slide_family(slide_text)
    if not _has_required_structured_payload(schema, content):
        return False, fill_ratio
    if family in {"bullet", "roadmap", "dense-text", "comparison"}:
        return False, fill_ratio
    if family == "cover":
        return (fill_ratio >= 0.5 and len(_clean_verbatim_text(slide_text)) <= 220), fill_ratio
    if family == "chart" and not _content_has_meaningful_chart_payload(content):
        return False, fill_ratio
    if family == "table" and not _content_has_meaningful_table_payload(content):
        return False, fill_ratio
    if family == "kpi" and not _content_has_meaningful_kpi_payload(content):
        return False, fill_ratio
    if fill_ratio >= 0.6:
        return True, fill_ratio
    if family in {"table", "chart", "kpi"} and fill_ratio >= 0.45:
        return True, fill_ratio
    if family in {"cover"} and fill_ratio >= 0.5:
        return True, fill_ratio
    return False, fill_ratio


def _fill_verbatim_content(
    schema: dict,
    slide_text: str,
    slide_meta: Optional[Dict[str, Any]] = None,
) -> dict:
    slide_text = _clean_verbatim_text(slide_text)
    title, body, body_lines = _split_slide_text(slide_text)
    content = _schema_default(schema)
    if not isinstance(content, dict):
        content = {}

    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    body_field_keys = _text_field_keys(properties)
    body_chunks = _split_text_for_fields(body or title, len(body_field_keys))
    body_chunk_by_key = dict(zip(body_field_keys, body_chunks))
    title_set = False
    body_set = False

    for key, value_schema in properties.items():
        if not isinstance(value_schema, dict):
            continue
        value_type = _schema_type(value_schema)
        if value_type == "string":
            if _is_title_key(key) and not title_set:
                content[key] = title
                title_set = True
            elif _is_body_key(key) and not body_set:
                content[key] = body_chunk_by_key.get(key, body or title)
                body_set = True
            elif _is_text_key(key):
                content[key] = body_chunk_by_key.get(key, "")
        elif value_type == "array":
            content[key] = _fill_array_items(value_schema, body_lines, key, slide_text)
        elif value_type == "object":
            if "chart" in _normalized_key(key) or "graph" in _normalized_key(key):
                content[key] = _extract_chart_from_text(slide_text, value_schema, slide_meta)
                continue
            if "table" in _normalized_key(key):
                content[key] = _extract_table_from_text(slide_text, value_schema, slide_meta)
                continue

            nested = content.get(key)
            if not isinstance(nested, dict):
                nested = _schema_default(value_schema)
                if not isinstance(nested, dict):
                    nested = {}
            nested_properties = value_schema.get("properties", {})
            nested_body_field_keys = _text_field_keys(nested_properties)
            nested_body_chunks = _split_text_for_fields(
                body or title, len(nested_body_field_keys)
            )
            nested_body_chunk_by_key = dict(zip(nested_body_field_keys, nested_body_chunks))
            for nested_key, nested_schema in nested_properties.items():
                if not isinstance(nested_schema, dict):
                    continue
                nested_type = _schema_type(nested_schema)
                if nested_type == "string":
                    if _is_title_key(nested_key) and not title_set:
                        nested[nested_key] = title
                        title_set = True
                    elif _is_body_key(nested_key) and not body_set:
                        nested[nested_key] = nested_body_chunk_by_key.get(
                            nested_key, body or title
                        )
                        body_set = True
                    elif _is_text_key(nested_key):
                        nested[nested_key] = nested_body_chunk_by_key.get(nested_key, "")
                elif nested_type == "array":
                    nested[nested_key] = _fill_array_items(nested_schema, body_lines, nested_key, slide_text)
                elif nested_type == "object" and ("chart" in _normalized_key(nested_key) or "graph" in _normalized_key(nested_key)):
                    nested[nested_key] = _extract_chart_from_text(slide_text, nested_schema, slide_meta)
                elif nested_type == "object" and "table" in _normalized_key(nested_key):
                    nested[nested_key] = _extract_table_from_text(slide_text, nested_schema, slide_meta)
            content[key] = nested

    if not title_set:
        content["title"] = title
    if not body_set and body:
        content["description"] = body

    density = _verbatim_density(slide_text)
    family = _infer_verbatim_slide_family(slide_text)
    detected_family = _classify_verbatim_slide(slide_text)
    content["__speaker_note__"] = slide_text
    content["__verbatim_import__"] = True
    content["__verbatim_full_text__"] = slide_text
    content["__verbatim_density__"] = density
    content["__verbatim_family__"] = family
    content["__verbatim_detected_family__"] = detected_family
    content["__verbatim_canvas_aspect__"] = "16:9"
    content["__verbatim_fit_policy__"] = "fit-content-inside-16x9"
    content["__verbatim_needs_split__"] = False
    _clear_schema_string_defaults(schema, content)
    if _is_image_generation_disabled():
        content = _strip_image_fields_from_content(content)
    should_use_template_first, fill_ratio = _should_use_template_first(schema, content, slide_text)
    content["__verbatim_template_fill_ratio__"] = round(fill_ratio, 3)
    content["__verbatim_render_mode__"] = "template-first" if should_use_template_first else "verbatim-canvas"
    return _coerce_content_to_schema(schema, content)


@PRESENTATION_ROUTER.get("/all", response_model=List[PresentationWithSlides])
async def get_all_presentations(sql_session: AsyncSession = Depends(get_async_session)):
    presentations_with_slides = []

    query = (
        select(PresentationModel, SlideModel)
        .join(
            SlideModel,
            (SlideModel.presentation == PresentationModel.id) & (SlideModel.index == 0),
        )
        .order_by(PresentationModel.created_at.desc())
    )

    results = await sql_session.execute(query)
    rows = results.all()
    presentations_with_slides = [
        PresentationWithSlides(
            **presentation.model_dump(),
            slides=[first_slide],
        )
        for presentation, first_slide in rows
    ]
    return presentations_with_slides


@PRESENTATION_ROUTER.get("/{id}", response_model=PresentationWithSlides)
async def get_presentation(
    id: uuid.UUID, sql_session: AsyncSession = Depends(get_async_session)
):
    presentation = await sql_session.get(PresentationModel, id)
    if not presentation:
        raise HTTPException(404, "Presentation not found")
    slides = await sql_session.scalars(
        select(SlideModel)
        .where(SlideModel.presentation == id)
        .order_by(SlideModel.index)
    )
    return PresentationWithSlides(
        **presentation.model_dump(),
        slides=slides,
    )


@PRESENTATION_ROUTER.delete("/{id}", status_code=204)
async def delete_presentation(
    id: uuid.UUID, sql_session: AsyncSession = Depends(get_async_session)
):
    presentation = await sql_session.get(PresentationModel, id)
    if not presentation:
        raise HTTPException(404, "Presentation not found")

    await sql_session.delete(presentation)
    await sql_session.commit()


@PRESENTATION_ROUTER.post("/create", response_model=PresentationModel)
async def create_presentation(
    content: Annotated[str, Body()],
    n_slides: Annotated[int, Body()],
    language: Annotated[str, Body()],
    file_paths: Annotated[Optional[List[str]], Body()] = None,
    tone: Annotated[Tone, Body()] = Tone.DEFAULT,
    verbosity: Annotated[Verbosity, Body()] = Verbosity.STANDARD,
    instructions: Annotated[Optional[str], Body()] = None,
    include_table_of_contents: Annotated[bool, Body()] = False,
    include_title_slide: Annotated[bool, Body()] = True,
    web_search: Annotated[bool, Body()] = False,
    sql_session: AsyncSession = Depends(get_async_session),
):
    content = _clean_verbatim_text(content)
    instructions = _clean_verbatim_text(instructions) if instructions else instructions

    if include_table_of_contents and n_slides < 3:
        raise HTTPException(
            status_code=400,
            detail="Number of slides cannot be less than 3 if table of contents is included",
        )

    presentation_id = uuid.uuid4()

    presentation = PresentationModel(
        id=presentation_id,
        content=content,
        n_slides=n_slides,
        language=language,
        file_paths=file_paths,
        tone=tone.value,
        verbosity=verbosity.value,
        instructions=instructions,
        include_table_of_contents=include_table_of_contents,
        include_title_slide=include_title_slide,
        web_search=web_search,
    )

    sql_session.add(presentation)
    await sql_session.commit()

    return presentation


@PRESENTATION_ROUTER.post("/prepare", response_model=PresentationModel)
async def prepare_presentation(
    presentation_id: Annotated[uuid.UUID, Body()],
    outlines: Annotated[List[SlideOutlineModel], Body()],
    layout: Annotated[PresentationLayoutModel, Body()],
    title: Annotated[Optional[str], Body()] = None,
    sql_session: AsyncSession = Depends(get_async_session),
):
    if not outlines:
        raise HTTPException(status_code=400, detail="Outlines are required")

    presentation = await sql_session.get(PresentationModel, presentation_id)
    if not presentation:
        raise HTTPException(status_code=404, detail="Presentation not found")

    presentation_outline_model = PresentationOutlineModel(slides=outlines)

    total_slide_layouts = len(layout.slides)
    total_outlines = len(outlines)

    if layout.ordered:
        presentation_structure = layout.to_presentation_structure()
    else:
        presentation_structure: PresentationStructureModel = (
            await generate_presentation_structure(
                presentation_outline=presentation_outline_model,
                presentation_layout=layout,
                instructions=presentation.instructions,
            )
        )

    presentation_structure.slides = presentation_structure.slides[: len(outlines)]
    for index in range(total_outlines):
        random_slide_index = random.randint(0, total_slide_layouts - 1)
        if index >= total_outlines:
            presentation_structure.slides.append(random_slide_index)
            continue
        if presentation_structure.slides[index] >= total_slide_layouts:
            presentation_structure.slides[index] = random_slide_index

    if presentation.include_table_of_contents:
        n_toc_slides = presentation.n_slides - total_outlines
        toc_slide_layout_index = select_toc_or_list_slide_layout_index(layout)
        if toc_slide_layout_index != -1:
            outline_index = 1 if presentation.include_title_slide else 0
            for i in range(n_toc_slides):
                outlines_to = outline_index + 10
                if total_outlines == outlines_to:
                    outlines_to -= 1

                presentation_structure.slides.insert(
                    i + 1 if presentation.include_title_slide else i,
                    toc_slide_layout_index,
                )
                toc_outline = "Table of Contents\n\n"

                for outline in presentation_outline_model.slides[
                    outline_index:outlines_to
                ]:
                    page_number = (
                        outline_index - i + n_toc_slides + 1
                        if presentation.include_title_slide
                        else outline_index - i + n_toc_slides
                    )
                    toc_outline += f"Slide page number: {page_number}\n Slide Content: {outline.content[:100]}\n\n"
                    outline_index += 1

                outline_index += 1

                presentation_outline_model.slides.insert(
                    i + 1 if presentation.include_title_slide else i,
                    SlideOutlineModel(
                        content=toc_outline,
                    ),
                )

    sql_session.add(presentation)
    presentation.outlines = presentation_outline_model.model_dump(mode="json")
    presentation.title = title or presentation.title
    presentation.set_layout(layout)
    presentation.set_structure(presentation_structure)
    await sql_session.commit()

    return presentation


@PRESENTATION_ROUTER.post("/prepare-verbatim", response_model=PresentationWithSlides)
async def prepare_presentation_verbatim(
    presentation_id: Annotated[uuid.UUID, Body()],
    outlines: Annotated[List[SlideOutlineModel], Body()],
    layout: Annotated[PresentationLayoutModel, Body()],
    use_llm_planner: Annotated[bool, Body()] = True,
    pptx_slides: Annotated[Optional[List[Dict[str, Any]]], Body()] = None,
    sql_session: AsyncSession = Depends(get_async_session),
):
    if not outlines:
        raise HTTPException(status_code=400, detail="Outlines are required")

    presentation = await sql_session.get(PresentationModel, presentation_id)
    if not presentation:
        raise HTTPException(status_code=404, detail="Presentation not found")

    import_safe_layout = _filter_import_safe_layout(layout)
    hybrid_outline_items = _build_hybrid_verbatim_outline_items(outlines, pptx_slides)
    presentation_outline_model = PresentationOutlineModel(
        slides=[SlideOutlineModel(content=item["content"]) for item in hybrid_outline_items]
    )
    deterministic_indexes: List[int] = []
    for index, outline in enumerate(presentation_outline_model.slides):
        outline_meta = hybrid_outline_items[index]
        deterministic_indexes.append(
            _choose_verbatim_layout_index_for_slide(
                import_safe_layout,
                outline.content,
                deterministic_indexes,
                {
                    "source_index": outline_meta.get("source_index"),
                    "source_slide_index": outline_meta.get("source_index"),
                },
            )
        )
    presentation_structure = (
        await _generate_verbatim_presentation_structure(
            presentation_outline_model=presentation_outline_model,
            layout=import_safe_layout,
            instructions=presentation.instructions,
        )
        if use_llm_planner
        else PresentationStructureModel(
            slides=deterministic_indexes
        )
    )

    await sql_session.execute(
        delete(SlideModel).where(SlideModel.presentation == presentation_id)
    )

    slides: List[SlideModel] = []
    for index, outline in enumerate(presentation_outline_model.slides):
        outline_meta = hybrid_outline_items[index]
        selected_layout_index = presentation_structure.slides[index]
        deterministic_layout_index = deterministic_indexes[index] if index < len(deterministic_indexes) else selected_layout_index
        selected_layout_candidate = import_safe_layout.slides[selected_layout_index]
        if _layout_unsuitable_for_verbatim(selected_layout_candidate, outline.content):
            selected_layout_index = deterministic_layout_index
        elif _is_intro_candidate(
            outline.content,
            {
                "source_index": outline_meta.get("source_index"),
                "source_slide_index": outline_meta.get("source_index"),
            },
        ) and not any(
            keyword in _layout_search_text(selected_layout_candidate)
            for keyword in {"intro", "cover", "hero", "statement", "section"}
        ):
            selected_layout_index = deterministic_layout_index
        selected_layout = import_safe_layout.slides[selected_layout_index]
        slide_content = _fill_verbatim_content(
            selected_layout.json_schema,
            outline.content,
            outline_meta.get("source_meta"),
        )
        slide_content["__source_slide_index__"] = outline_meta["source_index"]
        slide_content["__source_slide_number__"] = outline_meta["source_slide_number"]
        slide_content["__continuation_part_index__"] = outline_meta["part_index"]
        slide_content["__continuation_part_count__"] = outline_meta["part_count"]
        slide_content["__verbatim_source_full_text__"] = outline_meta["source_text"]
        slide_content["__verbatim_needs_split__"] = outline_meta["part_count"] > 1
        slide_content["__selected_layout_id__"] = selected_layout.id
        slide_content["__selected_layout_name__"] = selected_layout.name
        slide_content["__selected_layout_description__"] = selected_layout.description
        slides.append(
            SlideModel(
                presentation=presentation_id,
                layout_group=layout.name,
                layout=selected_layout.id,
                index=index,
                speaker_note=outline_meta["source_text"],
                content=slide_content,
            )
        )

    presentation.content = "\n\n".join(
        item["content"] for item in hybrid_outline_items
    )
    presentation.n_slides = len(hybrid_outline_items)
    presentation.outlines = presentation_outline_model.model_dump(mode="json")
    presentation.title = get_presentation_title_from_outlines(presentation_outline_model)
    presentation.set_layout(import_safe_layout)
    presentation.set_structure(presentation_structure)

    sql_session.add(presentation)
    sql_session.add_all(slides)
    await sql_session.commit()

    return PresentationWithSlides(
        **presentation.model_dump(),
        slides=slides,
    )


@PRESENTATION_ROUTER.get("/stream/{id}", response_model=PresentationWithSlides)
async def stream_presentation(
    id: uuid.UUID, sql_session: AsyncSession = Depends(get_async_session)
):
    presentation = await sql_session.get(PresentationModel, id)
    if not presentation:
        raise HTTPException(status_code=404, detail="Presentation not found")
    if not presentation.structure:
        raise HTTPException(
            status_code=400,
            detail="Presentation not prepared for stream",
        )
    if not presentation.outlines:
        raise HTTPException(
            status_code=400,
            detail="Outlines can not be empty",
        )

    image_generation_service = ImageGenerationService(get_images_directory())

    async def inner():
        structure = presentation.get_structure()
        layout = presentation.get_layout()
        outline = presentation.get_presentation_outline()

        # These tasks will be gathered and awaited after all slides are generated
        async_assets_generation_tasks = []

        slides: List[SlideModel] = []
        yield SSEResponse(
            event="response",
            data=json.dumps({"type": "chunk", "chunk": '{ "slides": [ '}),
        ).to_string()
        for i, slide_layout_index in enumerate(structure.slides):
            slide_layout = layout.slides[slide_layout_index]

            try:
                slide_content = await get_slide_content_from_type_and_outline(
                    slide_layout,
                    outline.slides[i],
                    presentation.language,
                    presentation.tone,
                    presentation.verbosity,
                    presentation.instructions,
                )
            except HTTPException as e:
                yield SSEErrorResponse(detail=e.detail).to_string()
                return

            slide = SlideModel(
                presentation=id,
                layout_group=layout.name,
                layout=slide_layout.id,
                index=i,
                speaker_note=slide_content.get("__speaker_note__", ""),
                content=slide_content,
            )
            slides.append(slide)

            # This will mutate slide and add placeholder assets
            process_slide_add_placeholder_assets(slide)

            # This will mutate slide - start task immediately so it runs in parallel with next slide LLM generation
            async_assets_generation_tasks.append(
                asyncio.create_task(process_slide_and_fetch_assets(image_generation_service, slide))
            )

            yield SSEResponse(
                event="response",
                data=json.dumps({"type": "chunk", "chunk": slide.model_dump_json()}),
            ).to_string()

        yield SSEResponse(
            event="response",
            data=json.dumps({"type": "chunk", "chunk": " ] }"}),
        ).to_string()

        generated_assets_lists = await asyncio.gather(*async_assets_generation_tasks)
        generated_assets = []
        for assets_list in generated_assets_lists:
            generated_assets.extend(assets_list)

        # Moved this here to make sure new slides are generated before deleting the old ones
        await sql_session.execute(
            delete(SlideModel).where(SlideModel.presentation == id)
        )
        await sql_session.commit()

        sql_session.add(presentation)
        sql_session.add_all(slides)
        sql_session.add_all(generated_assets)
        await sql_session.commit()

        response = PresentationWithSlides(
            **presentation.model_dump(),
            slides=slides,
        )

        yield SSECompleteResponse(
            key="presentation",
            value=response.model_dump(mode="json"),
        ).to_string()

    return StreamingResponse(inner(), media_type="text/event-stream")


@PRESENTATION_ROUTER.patch("/update", response_model=PresentationWithSlides)
async def update_presentation(
    id: Annotated[uuid.UUID, Body()],
    n_slides: Annotated[Optional[int], Body()] = None,
    title: Annotated[Optional[str], Body()] = None,
    slides: Annotated[Optional[List[SlideModel]], Body()] = None,
    sql_session: AsyncSession = Depends(get_async_session),
):
    presentation = await sql_session.get(PresentationModel, id)
    if not presentation:
        raise HTTPException(status_code=404, detail="Presentation not found")

    presentation_update_dict = {}
    if n_slides:
        presentation_update_dict["n_slides"] = n_slides
    if title:
        presentation_update_dict["title"] = title

    if n_slides or title:
        presentation.sqlmodel_update(presentation_update_dict)

    if slides:
        # Just to make sure id is UUID
        for slide in slides:
            slide.presentation = uuid.UUID(slide.presentation)
            slide.id = uuid.UUID(slide.id)

        await sql_session.execute(
            delete(SlideModel).where(SlideModel.presentation == presentation.id)
        )
        sql_session.add_all(slides)

    await sql_session.commit()

    return PresentationWithSlides(
        **presentation.model_dump(),
        slides=slides or [],
    )


@PRESENTATION_ROUTER.post("/export/pptx", response_model=str)
async def export_presentation_as_pptx(
    pptx_model: Annotated[PptxPresentationModel, Body()],
):
    temp_dir = TEMP_FILE_SERVICE.create_temp_dir()

    pptx_creator = PptxPresentationCreator(pptx_model, temp_dir)
    await pptx_creator.create_ppt()

    export_directory = get_exports_directory()
    pptx_path = os.path.join(
        export_directory, f"{pptx_model.name or uuid.uuid4()}.pptx"
    )
    pptx_creator.save(pptx_path)

    return pptx_path


@PRESENTATION_ROUTER.post("/export", response_model=PresentationPathAndEditPath)
async def export_presentation_as_pptx_or_pdf(
    id: Annotated[uuid.UUID, Body(description="Presentation ID to export")],
    export_as: Annotated[
        Literal["pptx", "pdf"], Body(description="Format to export the presentation as")
    ] = "pptx",
    sql_session: AsyncSession = Depends(get_async_session),
):
    presentation = await sql_session.get(PresentationModel, id)

    if not presentation:
        raise HTTPException(status_code=404, detail="Presentation not found")

    presentation_and_path = await export_presentation(
        id,
        presentation.title or str(uuid.uuid4()),
        export_as,
    )

    return PresentationPathAndEditPath(
        **presentation_and_path.model_dump(),
        edit_path=f"/presentation?id={id}",
    )


async def check_if_api_request_is_valid(
    request: GeneratePresentationRequest,
    sql_session: AsyncSession = Depends(get_async_session),
) -> Tuple[uuid.UUID,]:
    presentation_id = uuid.uuid4()
    print(f"Presentation ID: {presentation_id}")

    # Making sure either content, slides markdown or files is provided
    if not (request.content or request.slides_markdown or request.files):
        raise HTTPException(
            status_code=400,
            detail="Either content or slides markdown or files is required to generate presentation",
        )

    # Making sure number of slides is greater than 0
    if request.n_slides <= 0:
        raise HTTPException(
            status_code=400,
            detail="Number of slides must be greater than 0",
        )

    # Checking if template is valid
    if request.template not in DEFAULT_TEMPLATES:
        request.template = request.template.lower()
        if not request.template.startswith("custom-"):
            raise HTTPException(
                status_code=400,
                detail="Template not found. Please use a valid template.",
            )
        template_id = request.template.replace("custom-", "")
        try:
            template = await sql_session.get(TemplateModel, uuid.UUID(template_id))
            if not template:
                raise Exception()
        except Exception:
            raise HTTPException(
                status_code=400,
                detail="Template not found. Please use a valid template.",
            )

    return (presentation_id,)


async def generate_presentation_handler(
    request: GeneratePresentationRequest,
    presentation_id: uuid.UUID,
    async_status: Optional[AsyncPresentationGenerationTaskModel],
    sql_session: AsyncSession = Depends(get_async_session),
):
    try:
        using_slides_markdown = False

        if request.slides_markdown:
            using_slides_markdown = True
            request.n_slides = len(request.slides_markdown)

        if not using_slides_markdown:
            additional_context = ""

            # Updating async status
            if async_status:
                async_status.message = "Generating presentation outlines"
                async_status.updated_at = datetime.now()
                sql_session.add(async_status)
                await sql_session.commit()

            if request.files:
                documents_loader = DocumentsLoader(file_paths=request.files)
                await documents_loader.load_documents()
                documents = documents_loader.documents
                if documents:
                    additional_context = "\n\n".join(documents)

            # Finding number of slides to generate by considering table of contents
            n_slides_to_generate = request.n_slides
            if request.include_table_of_contents:
                needed_toc_count = math.ceil(
                    (
                        (request.n_slides - 1)
                        if request.include_title_slide
                        else request.n_slides
                    )
                    / 10
                )
                n_slides_to_generate -= math.ceil(
                    (request.n_slides - needed_toc_count) / 10
                )

            presentation_outlines_text = ""
            async for chunk in generate_ppt_outline(
                request.content,
                n_slides_to_generate,
                request.language,
                additional_context,
                request.tone.value,
                request.verbosity.value,
                request.instructions,
                request.include_title_slide,
                request.web_search,
            ):

                if isinstance(chunk, HTTPException):
                    raise chunk

                presentation_outlines_text += chunk

            try:
                presentation_outlines_json = dict(
                    dirtyjson.loads(presentation_outlines_text)
                )
            except Exception:
                traceback.print_exc()
                raise HTTPException(
                    status_code=400,
                    detail="Failed to generate presentation outlines. Please try again.",
                )
            presentation_outlines = PresentationOutlineModel(
                **presentation_outlines_json
            )
            total_outlines = n_slides_to_generate

        else:
            # Setting outlines to slides markdown
            presentation_outlines = PresentationOutlineModel(
                slides=[
                    SlideOutlineModel(content=slide)
                    for slide in request.slides_markdown
                ]
            )
            total_outlines = len(request.slides_markdown)

        # Updating async status
        if async_status:
            async_status.message = "Selecting layout for each slide"
            async_status.updated_at = datetime.now()
            sql_session.add(async_status)
            await sql_session.commit()

        print("-" * 40)
        print(f"Generated {total_outlines} outlines for the presentation")

        # Parse Layouts
        layout_model = await get_layout_by_name(request.template)
        total_slide_layouts = len(layout_model.slides)

        # Generate Structure
        if layout_model.ordered:
            presentation_structure = layout_model.to_presentation_structure()
        else:
            presentation_structure: PresentationStructureModel = (
                await generate_presentation_structure(
                    presentation_outlines,
                    layout_model,
                    request.instructions,
                    using_slides_markdown,
                )
            )

        presentation_structure.slides = presentation_structure.slides[:total_outlines]
        for index in range(total_outlines):
            random_slide_index = random.randint(0, total_slide_layouts - 1)
            if index >= total_outlines:
                presentation_structure.slides.append(random_slide_index)
                continue
            if presentation_structure.slides[index] >= total_slide_layouts:
                presentation_structure.slides[index] = random_slide_index

        # Injecting table of contents to the presentation structure and outlines
        if request.include_table_of_contents and not using_slides_markdown:
            n_toc_slides = request.n_slides - total_outlines
            toc_slide_layout_index = select_toc_or_list_slide_layout_index(layout_model)
            if toc_slide_layout_index != -1:
                outline_index = 1 if request.include_title_slide else 0
                for i in range(n_toc_slides):
                    outlines_to = outline_index + 10
                    if total_outlines == outlines_to:
                        outlines_to -= 1

                    presentation_structure.slides.insert(
                        i + 1 if request.include_title_slide else i,
                        toc_slide_layout_index,
                    )
                    toc_outline = "Table of Contents\n\n"

                    for outline in presentation_outlines.slides[
                        outline_index:outlines_to
                    ]:
                        page_number = (
                            outline_index - i + n_toc_slides + 1
                            if request.include_title_slide
                            else outline_index - i + n_toc_slides
                        )
                        toc_outline += f"Slide page number: {page_number}\n Slide Content: {outline.content[:100]}\n\n"
                        outline_index += 1

                    outline_index += 1

                    presentation_outlines.slides.insert(
                        i + 1 if request.include_title_slide else i,
                        SlideOutlineModel(
                            content=toc_outline,
                        ),
                    )

        # Create PresentationModel
        presentation = PresentationModel(
            id=presentation_id,
            content=request.content,
            n_slides=request.n_slides,
            language=request.language,
            title=get_presentation_title_from_outlines(presentation_outlines),
            outlines=presentation_outlines.model_dump(),
            layout=layout_model.model_dump(),
            structure=presentation_structure.model_dump(),
            tone=request.tone.value,
            verbosity=request.verbosity.value,
            instructions=request.instructions,
        )

        # Updating async status
        if async_status:
            async_status.message = "Generating slides"
            async_status.updated_at = datetime.now()
            sql_session.add(async_status)
            await sql_session.commit()

        image_generation_service = ImageGenerationService(get_images_directory())
        async_assets_generation_tasks = []

        # 7. Generate slide content concurrently (batched), then build slides and fetch assets
        slides: List[SlideModel] = []

        slide_layout_indices = presentation_structure.slides
        slide_layouts = [layout_model.slides[idx] for idx in slide_layout_indices]

        # Schedule slide content generation and asset fetching in batches of 10
        batch_size = 10
        for start in range(0, len(slide_layouts), batch_size):
            end = min(start + batch_size, len(slide_layouts))

            print(f"Generating slides from {start} to {end}")

            # Generate contents for this batch concurrently
            content_tasks = [
                get_slide_content_from_type_and_outline(
                    slide_layouts[i],
                    presentation_outlines.slides[i],
                    request.language,
                    request.tone.value,
                    request.verbosity.value,
                    request.instructions,
                )
                for i in range(start, end)
            ]
            batch_contents: List[dict] = await asyncio.gather(*content_tasks)

            # Build slides for this batch
            batch_slides: List[SlideModel] = []
            for offset, slide_content in enumerate(batch_contents):
                i = start + offset
                slide_layout = slide_layouts[i]
                slide = SlideModel(
                    presentation=presentation_id,
                    layout_group=layout_model.name,
                    layout=slide_layout.id,
                    index=i,
                    speaker_note=slide_content.get("__speaker_note__"),
                    content=slide_content,
                )
                slides.append(slide)
                batch_slides.append(slide)

            # Start asset fetch tasks immediately so they run in parallel with next batch's LLM calls
            asset_tasks = [
                asyncio.create_task(process_slide_and_fetch_assets(image_generation_service, slide))
                for slide in batch_slides
            ]
            async_assets_generation_tasks.extend(asset_tasks)

        if async_status:
            async_status.message = "Fetching assets for slides"
            async_status.updated_at = datetime.now()
            sql_session.add(async_status)
            await sql_session.commit()

        # Run all asset tasks concurrently while batches may still be generating content
        generated_assets_list = await asyncio.gather(*async_assets_generation_tasks)
        generated_assets = []
        for assets_list in generated_assets_list:
            generated_assets.extend(assets_list)

        # 8. Save PresentationModel and Slides
        sql_session.add(presentation)
        sql_session.add_all(slides)
        sql_session.add_all(generated_assets)
        await sql_session.commit()

        if async_status:
            async_status.message = "Exporting presentation"
            async_status.updated_at = datetime.now()
            sql_session.add(async_status)

        # 9. Export
        presentation_and_path = await export_presentation(
            presentation_id, presentation.title or str(uuid.uuid4()), request.export_as
        )

        response = PresentationPathAndEditPath(
            **presentation_and_path.model_dump(),
            edit_path=f"/presentation?id={presentation_id}",
        )

        if async_status:
            async_status.message = "Presentation generation completed"
            async_status.status = "completed"
            async_status.data = response.model_dump(mode="json")
            async_status.updated_at = datetime.now()
            sql_session.add(async_status)
            await sql_session.commit()

        # Triggering webhook on success
        CONCURRENT_SERVICE.run_task(
            None,
            WebhookService.send_webhook,
            WebhookEvent.PRESENTATION_GENERATION_COMPLETED,
            response.model_dump(mode="json"),
        )

        return response

    except Exception as e:
        if not isinstance(e, HTTPException):
            traceback.print_exc()
            e = HTTPException(status_code=500, detail="Presentation generation failed")

        api_error_model = APIErrorModel.from_exception(e)

        # Triggering webhook on failure
        CONCURRENT_SERVICE.run_task(
            None,
            WebhookService.send_webhook,
            WebhookEvent.PRESENTATION_GENERATION_FAILED,
            api_error_model.model_dump(mode="json"),
        )

        if async_status:
            async_status.status = "error"
            async_status.message = "Presentation generation failed"
            async_status.updated_at = datetime.now()
            async_status.error = api_error_model.model_dump(mode="json")
            sql_session.add(async_status)
            await sql_session.commit()

        else:
            raise e


@PRESENTATION_ROUTER.post("/generate", response_model=PresentationPathAndEditPath)
async def generate_presentation_sync(
    request: GeneratePresentationRequest,
    sql_session: AsyncSession = Depends(get_async_session),
):
    try:
        (presentation_id,) = await check_if_api_request_is_valid(request, sql_session)
        return await generate_presentation_handler(
            request, presentation_id, None, sql_session
        )
    except Exception:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Presentation generation failed")


@PRESENTATION_ROUTER.post(
    "/generate/async", response_model=AsyncPresentationGenerationTaskModel
)
async def generate_presentation_async(
    request: GeneratePresentationRequest,
    background_tasks: BackgroundTasks,
    sql_session: AsyncSession = Depends(get_async_session),
):
    try:
        (presentation_id,) = await check_if_api_request_is_valid(request, sql_session)

        async_status = AsyncPresentationGenerationTaskModel(
            status="pending",
            message="Queued for generation",
            data=None,
        )
        sql_session.add(async_status)
        await sql_session.commit()

        background_tasks.add_task(
            generate_presentation_handler,
            request,
            presentation_id,
            async_status=async_status,
            sql_session=sql_session,
        )
        return async_status

    except Exception as e:
        if not isinstance(e, HTTPException):
            print(e)
            e = HTTPException(status_code=500, detail="Presentation generation failed")

        raise e


@PRESENTATION_ROUTER.get(
    "/status/{id}", response_model=AsyncPresentationGenerationTaskModel
)
async def check_async_presentation_generation_status(
    id: str = Path(description="ID of the presentation generation task"),
    sql_session: AsyncSession = Depends(get_async_session),
):
    status = await sql_session.get(AsyncPresentationGenerationTaskModel, id)
    if not status:
        raise HTTPException(
            status_code=404, detail="No presentation generation task found"
        )
    return status


@PRESENTATION_ROUTER.post("/edit", response_model=PresentationPathAndEditPath)
async def edit_presentation_with_new_content(
    data: Annotated[EditPresentationRequest, Body()],
    sql_session: AsyncSession = Depends(get_async_session),
):
    presentation = await sql_session.get(PresentationModel, data.presentation_id)
    if not presentation:
        raise HTTPException(status_code=404, detail="Presentation not found")

    slides = await sql_session.scalars(
        select(SlideModel).where(SlideModel.presentation == data.presentation_id)
    )

    new_slides = []
    slides_to_delete = []
    for each_slide in slides:
        updated_content = None
        new_slide_data = list(
            filter(lambda x: x.index == each_slide.index, data.slides)
        )
        if new_slide_data:
            updated_content = deep_update(each_slide.content, new_slide_data[0].content)
            new_slides.append(
                each_slide.get_new_slide(presentation.id, updated_content)
            )
            slides_to_delete.append(each_slide.id)

    await sql_session.execute(
        delete(SlideModel).where(SlideModel.id.in_(slides_to_delete))
    )

    sql_session.add_all(new_slides)
    await sql_session.commit()

    presentation_and_path = await export_presentation(
        presentation.id, presentation.title or str(uuid.uuid4()), data.export_as
    )

    return PresentationPathAndEditPath(
        **presentation_and_path.model_dump(),
        edit_path=f"/presentation?id={presentation.id}",
    )


@PRESENTATION_ROUTER.post("/derive", response_model=PresentationPathAndEditPath)
async def derive_presentation_from_existing_one(
    data: Annotated[EditPresentationRequest, Body()],
    sql_session: AsyncSession = Depends(get_async_session),
):
    presentation = await sql_session.get(PresentationModel, data.presentation_id)
    if not presentation:
        raise HTTPException(status_code=404, detail="Presentation not found")

    slides = await sql_session.scalars(
        select(SlideModel).where(SlideModel.presentation == data.presentation_id)
    )

    new_presentation = presentation.get_new_presentation()
    new_slides = []
    for each_slide in slides:
        updated_content = None
        new_slide_data = list(
            filter(lambda x: x.index == each_slide.index, data.slides)
        )
        if new_slide_data:
            updated_content = deep_update(each_slide.content, new_slide_data[0].content)
        new_slides.append(
            each_slide.get_new_slide(new_presentation.id, updated_content)
        )

    sql_session.add(new_presentation)
    sql_session.add_all(new_slides)
    await sql_session.commit()

    presentation_and_path = await export_presentation(
        new_presentation.id, new_presentation.title or str(uuid.uuid4()), data.export_as
    )

    return PresentationPathAndEditPath(
        **presentation_and_path.model_dump(),
        edit_path=f"/presentation?id={new_presentation.id}",
    )
