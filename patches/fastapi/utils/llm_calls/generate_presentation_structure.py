import json
import re
from typing import Any, List, Optional

import dirtyjson
from models.llm_message import LLMSystemMessage, LLMUserMessage
from models.presentation_layout import PresentationLayoutModel
from models.presentation_outline_model import PresentationOutlineModel
from services.llm_client import LLMClient
from utils.llm_client_error_handler import handle_llm_client_exceptions
from utils.llm_provider import get_model
from utils.get_dynamic_models import get_presentation_structure_model_with_n_slides
from models.presentation_structure_model import PresentationStructureModel


def get_messages(
    presentation_layout: PresentationLayoutModel,
    n_slides: int,
    data: str,
    instructions: Optional[str] = None,
):
    return [
        LLMSystemMessage(
            content=f"""
                You're a professional presentation designer with creative freedom to design engaging presentations.

                {presentation_layout.to_string()}

                # DESIGN PHILOSOPHY
                - Create visually compelling and varied presentations
                - Match layout to content purpose and audience needs
                - Prioritize engagement over rigid formatting rules

                # Layout Selection Guidelines
                1. **Content-driven choices**: Let the slide's purpose guide layout selection
                - Opening/closing -> Title layouts
                - Processes/workflows -> Visual process layouts
                - Comparisons/contrasts -> Side-by-side layouts
                - Data/metrics -> Chart/graph layouts
                - Concepts/ideas -> Image + text layouts
                - Key insights -> Emphasis layouts

                2. **Visual variety**: Aim for diverse, engaging presentation flow
                - Mix text-heavy and visual-heavy slides naturally
                - Use your judgment on when repetition serves the content
                - Balance information density across slides

                **Trust your design instincts. Focus on creating the most effective presentation for the content and audience.**

                {"# User Instruction:" if instructions else ""}
                {instructions or ""}

                User instruction should be taken into account while creating the presentation structure, except for number of slides.

                Select layout index for each of the {n_slides} slides based on what will best serve the presentation's goals.
                Return only JSON in this exact shape: {{"slides":[0,1,2]}}.
            """,
        ),
        LLMUserMessage(content=f"\n{data}\n"),
    ]


def get_messages_for_slides_markdown(
    presentation_layout: PresentationLayoutModel,
    n_slides: int,
    data: str,
    instructions: Optional[str] = None,
):
    return [
        LLMSystemMessage(
            content=f"""
                You're a professional presentation designer selecting slide layout indexes.

                {"# User Instruction:" if instructions else ""}
                {instructions or ""}

                {presentation_layout.to_string()}

                Select layout that best matches the content of the slides.
                User instruction should be taken into account while creating the presentation structure, except for number of slides.

                CRITICAL OUTPUT CONTRACT:
                - Return exactly one JSON object.
                - The object must have one key: "slides".
                - "slides" must contain exactly {n_slides} integer layout indexes.
                - Do not return a JSON schema.
                - Do not return "properties", "items", "required", markdown, comments, or explanation.
                - Example for {n_slides} slides: {{"slides":[0,1,0]}}
            """,
        ),
        LLMUserMessage(content=f"\n{data}\n"),
    ]


def _normalize_slide_indexes(
    indexes: Any,
    n_slides: int,
    n_layouts: int,
) -> Optional[List[int]]:
    if not isinstance(indexes, list):
        return None

    normalized: List[int] = []
    for value in indexes:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            index = value
        elif isinstance(value, float) and value.is_integer():
            index = int(value)
        elif isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
            index = int(value.strip())
        else:
            return None

        if index < 0 or index >= n_layouts:
            return None
        normalized.append(index)

    if len(normalized) < n_slides:
        return None
    return normalized[:n_slides]


def _find_slide_indexes(
    value: Any,
    n_slides: int,
    n_layouts: int,
    depth: int = 0,
) -> Optional[List[int]]:
    if depth > 6:
        return None

    direct = _normalize_slide_indexes(value, n_slides, n_layouts)
    if direct is not None:
        return direct

    if isinstance(value, dict):
        for key in ("slides", "slide_indexes", "slideIndices", "layout_indexes", "layouts"):
            if key in value:
                nested = _find_slide_indexes(value[key], n_slides, n_layouts, depth + 1)
                if nested is not None:
                    return nested
        for child in value.values():
            nested = _find_slide_indexes(child, n_slides, n_layouts, depth + 1)
            if nested is not None:
                return nested
    elif isinstance(value, list):
        for child in value:
            nested = _find_slide_indexes(child, n_slides, n_layouts, depth + 1)
            if nested is not None:
                return nested

    return None


def _extract_json_candidates(text: str) -> List[Any]:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    candidates = [cleaned]
    first_brace = cleaned.find("{")
    last_brace = cleaned.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidates.append(cleaned[first_brace : last_brace + 1])

    first_bracket = cleaned.find("[")
    last_bracket = cleaned.rfind("]")
    if first_bracket != -1 and last_bracket > first_bracket:
        candidates.append(cleaned[first_bracket : last_bracket + 1])

    parsed_values = []
    for candidate in candidates:
        try:
            parsed_values.append(dirtyjson.loads(candidate))
        except Exception:
            continue
    return parsed_values


async def _repair_presentation_structure(
    client: LLMClient,
    model: str,
    presentation_outline: PresentationOutlineModel,
    presentation_layout: PresentationLayoutModel,
    instructions: Optional[str],
    using_slides_markdown: bool,
    bad_response: Any,
) -> Optional[PresentationStructureModel]:
    n_slides = len(presentation_outline.slides)
    n_layouts = len(presentation_layout.slides)
    layout_indexes = ", ".join(str(index) for index in range(n_layouts))
    bad_preview = json.dumps(bad_response, ensure_ascii=False, default=str)[:2000]

    messages = [
        LLMSystemMessage(
            content=(
                "You are repairing a presentation layout selection response. "
                "Return exactly one JSON object and nothing else. "
                f"The object must be {{\"slides\":[...]}} with exactly {n_slides} integers. "
                f"Allowed layout indexes are: {layout_indexes}. "
                "Do not return a schema or any explanatory text."
            )
        ),
        LLMUserMessage(
            content=(
                f"Slides:\n{presentation_outline.to_string()}\n\n"
                f"Available layouts:\n{presentation_layout.to_string()}\n\n"
                f"User instructions:\n{instructions or ''}\n\n"
                f"Previous invalid response:\n{bad_preview}"
            )
        ),
    ]

    try:
        raw = await client.generate(model=model, messages=messages, max_tokens=512)
    except Exception as exc:
        print(f"generate_presentation_structure: repair request failed: {exc}")
        return None

    for candidate in _extract_json_candidates(raw):
        indexes = _find_slide_indexes(candidate, n_slides, n_layouts)
        if indexes is not None:
            return PresentationStructureModel(slides=indexes)

    print(
        "generate_presentation_structure: repair response invalid "
        f"model={model} content_preview={raw[:500]!r}"
    )
    return None


async def generate_presentation_structure(
    presentation_outline: PresentationOutlineModel,
    presentation_layout: PresentationLayoutModel,
    instructions: Optional[str] = None,
    using_slides_markdown: bool = False,
) -> PresentationStructureModel:
    client = LLMClient()
    model = get_model()
    n_slides = len(presentation_outline.slides)
    n_layouts = len(presentation_layout.slides)
    response_model = get_presentation_structure_model_with_n_slides(n_slides)
    messages = (
        get_messages_for_slides_markdown(
            presentation_layout,
            n_slides,
            presentation_outline.to_string(),
            instructions,
        )
        if using_slides_markdown
        else get_messages(
            presentation_layout,
            n_slides,
            presentation_outline.to_string(),
            instructions,
        )
    )

    try:
        response = await client.generate_structured(
            model=model,
            messages=messages,
            response_format=response_model.model_json_schema(),
            strict=True,
            max_tokens=768,
        )

        indexes = _find_slide_indexes(response, n_slides, n_layouts)
        if indexes is not None:
            return PresentationStructureModel(slides=indexes)

        repaired = await _repair_presentation_structure(
            client=client,
            model=model,
            presentation_outline=presentation_outline,
            presentation_layout=presentation_layout,
            instructions=instructions,
            using_slides_markdown=using_slides_markdown,
            bad_response=response,
        )
        if repaired is not None:
            return repaired

        return PresentationStructureModel(**response)
    except Exception as e:
        raise handle_llm_client_exceptions(e)
