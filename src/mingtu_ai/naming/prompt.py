"""Prompt construction for the fixed naming contract."""

from __future__ import annotations

import json

import re

from mingtu_ai.models import AnalysisInput


GENERIC_FILENAME_RE = re.compile(
    r"^(?:螢幕截圖|螢幕_截圖|截圖|screenshot|screen_shot|selection|capture|snapshot|img_\d+|dsc_\d+)",
    re.IGNORECASE,
)

_RULES = """Analyze this local file and propose a searchable, information-dense, entity-specific filename stem.
Return only JSON matching the supplied schema.

Core Objective:
Understand the true semantic purpose, context, and key details of the file. Do not perform surface-level image tagging or generic scene descriptions. Create a stem that a human would use to find this specific file later.

Rules:
1. UNDERSTAND THE PURPOSE & CONTEXT:
   - For Error Logs / Debug Screenshots: Identify exact exception type, module, line/cause (e.g. "PyTorch_CUDA_OutOfMemory_Error").
   - For Articles / Research / Documents: Identify title, author, key topic, or report name (e.g. "Attention_Is_All_You_Need").
   - For UI / Settings / Workflows: Identify app name, setting page, and main control (e.g. "KDE_Plasma6_Display_Settings").
   - For Physical Entities / Products / Vehicles: Identify exact brand, product line, model number, or catalog name (e.g. "Audi_R8_V10", "ThinkPad_X1_Carbon").
   - For Media / Audio / Video: Identify title, speaker, song, course name, or event topic.

2. AVOID SURFACE DESCRIPTIONS & GENERIC TAGS:
   - NEVER output generic visual scene tags or color descriptions (e.g., DO NOT return "white_car_on_road", "man_in_suit", "blue_background").
   - NEVER return generic placeholder terms like "螢幕截圖", "截圖", "Screenshot", "Selection", "Capture", "Image", "Picture" as the stem.

3. LANGUAGE & FORMATTING:
   - Write a concise 10 to 30 character stem in the content's language (or Traditional Chinese/English for global brands/products).
   - Preserve technical terms, brand names, model numbers, and verbatim titles.
   - Produce a stem only: never add a file extension or path.
   - Use underscores between semantic parts and no unsafe punctuation.

4. ORIGINAL FILENAME & EVIDENCE:
   - If the original filename is a generic screenshot default name (e.g. "螢幕截圖_*"), ignore the original filename completely.
   - For non-image files with insufficient evidence, keep the original stem and lower confidence.
"""


def build_naming_prompt(item: AnalysisInput, *, max_text_chars: int = 16_000) -> str:
    metadata = json.dumps(item.metadata, ensure_ascii=False, sort_keys=True)
    is_generic = bool(GENERIC_FILENAME_RE.search(item.source_path.stem))
    original_note = (
        " (Note: Uninformative generic screenshot/camera default name. "
        "Do NOT use '螢幕截圖' or 'Screenshot' in the stem. Determine stem strictly from content.)"
        if is_generic
        else ""
    )

    if item.kind == "image" or item.image_paths:
        content_text = (
            item.text[:max_text_chars]
            if item.text
            else "(Attached image payload provided for vision analysis. Read visible text, window titles, and layout from the image.)"
        )
    else:
        content_text = item.text[:max_text_chars] if item.text else "(no extracted text)"

    return (
        f"{_RULES}\n"
        f"Original filename: {item.source_path.name}{original_note}\n"
        f"MIME type: {item.mime_type}\n"
        f"Adapter kind: {item.kind}\n"
        f"Metadata: {metadata}\n"
        f"Extracted content/text:\n{content_text}"
    )
