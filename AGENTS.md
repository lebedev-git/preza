# Presenton Local Patch Notes

## Project Goal

This workspace patches the original `ghcr.io/presenton/presenton:latest` Docker image instead of editing upstream directly.

The main direction of the work is not generic slide generation. The main direction is:

- upload an existing `.pptx`;
- extract the source text slide-by-slide;
- preserve the original meaning and structure;
- adapt each imported slide into a Presenton layout without hallucinations, broken routing, empty canvases, or unreadable export;
- keep the imported text as the source of truth.

In short: this project is focused on correct PPTX verbatim import and adaptation.

## Current Runtime

The patched local image currently runs with Ollama using:

- `LLM=ollama`
- `OLLAMA_URL=http://host.docker.internal:11434`
- `OLLAMA_MODEL=gemma4:e4b`

The local git repository is initialized and currently pushed to:

- `https://github.com/lebedev-git/preza.git`

## What This Patch Set Adds

- Ollama support for `gemma4:e4b`
- PPTX text extraction by slide
- PPTX verbatim import flow
- per-slide LLM-assisted layout planning
- deterministic layout fallback when LLM planning fails
- safer verbatim content filling
- stricter routing for chart/table/kpi slides
- frontend verbatim fallback rendering with denser typography
- root-cause fixes for stretched numbered cards and empty template-first layouts

## Build

Run from the project root:

```powershell
docker build -t presenton-gemma4:latest .
```

The Dockerfile copies local patch files into the upstream image and runs a production Next.js build:

```dockerfile
RUN cd /app/servers/nextjs && npm run build
```

Warnings about Browserslist, missing static `presentation-templates`, or dynamic route usage are currently non-fatal if the build exits successfully.

## Verification After Changes

After code changes, always run the production Docker build from the project root unless the user explicitly asks to skip it:

```powershell
docker build -t presenton-gemma4:latest .
```

Treat a successful Docker build as the default verification gate because it copies the local patches into the upstream image and runs the production Next.js build.

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
  -e OLLAMA_MODEL=gemma4:e4b `
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
OLLAMA_MODEL=gemma4:e4b
```

## PPTX Flow

1. User uploads a `.pptx` file on `/upload`.
2. Frontend calls:

```text
POST /api/v1/ppt/pptx-slides/extract-text
```

3. Backend extracts readable text by slide, including text boxes, tables, notes when requested, and XML fallback text.
4. Upload page shows `Slide text breakdown`.
5. Frontend creates a presentation record from the extracted slide text.
6. On the outline/template step, PPTX flow calls:

```text
POST /api/v1/ppt/presentation/prepare-verbatim
```

7. Backend chooses a suitable layout family for each imported slide and fills it with the original extracted text.
8. User is routed to:

```text
/presentation?id=<presentation_id>&type=standard
```

## LLM Role

For PPTX import, LLM should not rewrite slide text. It is used only as a layout planner:

- choose which template layout should be used for each slide
- prefer intro/section layouts for short divider slides
- prefer numbered/process layouts for multi-point slides
- prefer table/chart/kpi layouts only when there is real structured evidence
- prefer denser text/content layouts for academic slides

The original extracted text remains the source of truth and is stored in:

- visible layout fields where possible
- `__speaker_note__`
- `__verbatim_full_text__`

## Current Root-Cause Fixes

These fixes are already part of the patch set:

- string defaults from Zod schemas are cleared so imported slides do not inherit fake quote/author defaults such as Winston Churchill
- image/icon defaults are stripped when `DISABLE_IMAGE_GENERATION=true`, so imported slides do not keep Unsplash placeholders
- quote/team/image-heavy layouts are filtered out of the import-safe verbatim pool
- first slides and short section-divider slides are routed toward intro-style handling
- numbered structure detection is stronger and is used in deterministic layout selection
- `template-first` is now based on actual field fill ratio and meaningful structured payload, not on the old visual-slot heuristic
- empty or weak chart payloads are blocked from pretending to be valid chart slides
- underfilled template-first slides can drop into verbatim fallback instead of staying as empty layouts
- short numbered section cards no longer stretch to full slide height in the fallback renderer

## Fallback Logic

If LLM layout planning fails or returns invalid JSON, backend uses deterministic layout selection. It scores layouts by:

- slide text length
- line count
- numbered/bullet structure
- numeric or table-like content
- layout name and description
- avoiding too many repeated layouts in a row

Frontend also has a verbatim fallback renderer in `V1ContentRender.tsx`. It is used when:

- backend marks the slide as `verbatim-canvas`
- template-first overflows
- template-first is severely underfilled and would otherwise leave large empty regions

## Ollama Structured Output

Ollama/Gemma structured output uses a custom path in `patches/fastapi/services/llm_client.py`.

Instead of relying on fragile OpenAI-style `json_schema`, it:

- requests `response_format: {"type": "json_object"}`
- injects the schema into the system prompt
- strips markdown fences
- extracts the first JSON object from the response
- retries once if JSON parsing fails

## Typography And Rendering

Imported PPTX slides are marked with:

```json
{
  "__verbatim_import__": true,
  "__verbatim_density__": "normal | medium | dense",
  "__verbatim_full_text__": "..."
}
```

Frontend `V1ContentRender.tsx` applies verbatim-aware rendering for imported slides:

- larger heading/body scale than the original compact mode
- accent bar and ghost numeral treatment for intro/section slides
- denser cards, roadmap blocks, metrics, and section decks
- automatic fallback away from broken or empty template-first layouts

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

- slide text breakdown appears
- outline step is not stuck on `Loading...`
- layouts vary by slide structure
- no fake quote/author defaults appear
- no Unsplash placeholder images remain when image generation is disabled
- presentation page opens
- logs show no `dirtyjson`, `Expecting value`, or repeated planner failures

## Known Limitations

- Existing generated presentations are not automatically regenerated after code changes.
- Some source PPTX objects are not reconstructed yet: SmartArt, embedded chart series, complex diagrams, and image-only text.
- Current chart support is still inferred mostly from extracted text/tables, not true PPTX chart XML parsing.
- Export quality can still lag behind browser rendering for some dense academic slides.
- Very dense slides still need more aggressive semantic splitting in some cases.
- Backend and frontend still duplicate some family detection logic; this should eventually be unified.
