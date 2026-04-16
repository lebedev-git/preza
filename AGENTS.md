# Presenton Local Patch Notes

## Project Goal

This workspace patches the original `ghcr.io/presenton/presenton:latest` Docker image instead of editing the upstream image directly. The local image adds:

- Ollama model support for `gemma4:e2b`.
- PPTX text extraction by slide.
- A PPTX verbatim import flow.
- LLM-assisted layout selection inside the chosen template.
- Safer fallback layout selection when LLM structured output fails.
- Compact font fitting for imported PPTX slides.

## Build

Run from the project root:

```powershell
docker build -t presenton-gemma4:latest .
```

The Dockerfile copies local patch files into the upstream image and runs a full Next.js production build:

```dockerfile
RUN cd /app/servers/nextjs && npm run build
```

Warnings about Browserslist, missing static `presentation-templates`, or dynamic route usage are currently non-fatal if the build exits successfully.

## Run

Start the patched container:

```powershell
docker rm -f presenton
docker run -d --name presenton `
  -p 5000:80 `
  -v "C:\presenton-data:/app_data" `
  -e DISABLE_IMAGE_GENERATION=true `
  -e CAN_CHANGE_KEYS=false `
  -e LLM=ollama `
  -e OLLAMA_URL=http://host.docker.internal:11434 `
  -e OLLAMA_MODEL=gemma4:e2b `
  presenton-gemma4:latest
```

Open:

```text
http://localhost:5000/upload
```

## Verify Runtime

```powershell
docker ps --filter name=presenton
docker inspect presenton --format '{{range .Config.Env}}{{println .}}{{end}}' | Select-String -Pattern 'LLM|OLLAMA'
docker logs --tail 120 presenton
```

Expected env:

```text
LLM=ollama
OLLAMA_URL=http://host.docker.internal:11434
OLLAMA_MODEL=gemma4:e2b
```

## PPTX Flow

1. User uploads a `.pptx` file on `/upload`.
2. Frontend calls:

```text
POST /api/v1/ppt/pptx-slides/extract-text
```

3. Backend extracts readable text by slide, including text boxes, tables, notes when requested, and XML fallback text.
4. Upload page shows `Slide text breakdown`.
5. When the user continues, the frontend creates a presentation record from extracted slide text.
6. On the outline/template step, PPTX flow calls:

```text
POST /api/v1/ppt/presentation/prepare-verbatim
```

7. Backend chooses layouts inside the selected template and fills them with the original extracted text.
8. User is routed to:

```text
/presentation?id=<presentation_id>&type=standard
```

## LLM Role

For PPTX import, LLM should not rewrite slide text. It is used only as a layout planner:

- choose which template layout should be used for each slide;
- prefer data/metric/chart layouts for numeric or table-like slides;
- prefer bullet/list/card layouts for multi-point slides;
- prefer text/content layouts for dense academic text.

The original extracted text remains the source of truth and is stored in:

- visible layout fields where possible;
- `__speaker_note__`;
- `__verbatim_full_text__`.

## Fallback Logic

If LLM layout planning fails or returns invalid JSON, backend uses deterministic layout selection. It scores layouts by:

- slide text length;
- line count;
- numeric/table-like content;
- layout name and description;
- avoiding too many repeated layouts in a row.

This prevents all slides from falling into one repeated template layout.

## Ollama Structured Output

Ollama/Gemma structured output uses a custom path in `patches/fastapi/services/llm_client.py`.

Instead of relying on fragile OpenAI-style `json_schema`, it:

- requests `response_format: {"type": "json_object"}`;
- injects the schema into the system prompt;
- strips markdown fences;
- extracts the first JSON object from the response;
- retries once if JSON parsing fails.

## Font And Layout Fitting

Imported PPTX slides are marked with:

```json
{
  "__verbatim_import__": true,
  "__verbatim_density__": "normal | medium | dense",
  "__verbatim_full_text__": "..."
}
```

Frontend `V1ContentRender.tsx` applies compact CSS only for imported PPTX slides. Dense slides get smaller heading/body fonts and tighter line-height.

Current limitation: very long academic slides may still need automatic splitting into multiple slides. That is the next recommended improvement.

## Important Patch Files

```text
Dockerfile
patches/fastapi/constants/supported_ollama_models.py
patches/fastapi/services/llm_client.py
patches/fastapi/api/v1/ppt/endpoints/pptx_slides.py
patches/fastapi/api/v1/ppt/endpoints/presentation.py
patches/nextjs/app/(presentation-generator)/upload/components/UploadPage.tsx
patches/nextjs/app/(presentation-generator)/upload/components/SupportingDoc.tsx
patches/nextjs/app/(presentation-generator)/outline/hooks/usePresentationGeneration.ts
patches/nextjs/app/(presentation-generator)/outline/hooks/useOutlineStreaming.ts
patches/nextjs/app/(presentation-generator)/components/V1ContentRender.tsx
```

## Quick Smoke Test

```powershell
Invoke-WebRequest -Uri http://localhost:5000/upload -UseBasicParsing
```

Then upload a PPTX and check:

- slide text breakdown appears;
- outline step is not stuck on `Loading...`;
- selected template produces varied layouts;
- presentation page opens;
- logs show no `dirtyjson`, `Expecting value`, or `planner failed` errors.

## Known Limitations

- Existing generated presentations are not automatically regenerated after code changes.
- Some source PPTX objects are not reconstructed yet: SmartArt, embedded chart series, complex diagrams, and image-only text.
- Current graph support is based on extracted text/tables, not true PPTX chart XML parsing.
- For very dense slides, font fitting helps, but automatic slide splitting is still needed for best visual quality.
