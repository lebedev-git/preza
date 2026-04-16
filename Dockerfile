FROM ghcr.io/presenton/presenton:latest

COPY patches/fastapi/constants/supported_ollama_models.py /app/servers/fastapi/constants/supported_ollama_models.py
COPY patches/fastapi/services/llm_client.py /app/servers/fastapi/services/llm_client.py
COPY patches/fastapi/api/v1/ppt/endpoints/pptx_slides.py /app/servers/fastapi/api/v1/ppt/endpoints/pptx_slides.py
COPY patches/fastapi/api/v1/ppt/endpoints/presentation.py /app/servers/fastapi/api/v1/ppt/endpoints/presentation.py
COPY patches/fastapi/utils/llm_calls/generate_presentation_structure.py /app/servers/fastapi/utils/llm_calls/generate_presentation_structure.py
COPY patches/nextjs/app/(presentation-generator)/upload/components/UploadPage.tsx /app/servers/nextjs/app/(presentation-generator)/upload/components/UploadPage.tsx
COPY patches/nextjs/app/(presentation-generator)/upload/components/SupportingDoc.tsx /app/servers/nextjs/app/(presentation-generator)/upload/components/SupportingDoc.tsx
COPY patches/nextjs/app/(presentation-generator)/components/V1ContentRender.tsx /app/servers/nextjs/app/(presentation-generator)/components/V1ContentRender.tsx
COPY patches/nextjs/app/(presentation-generator)/outline/components/OutlinePage.tsx /app/servers/nextjs/app/(presentation-generator)/outline/components/OutlinePage.tsx
COPY patches/nextjs/app/(presentation-generator)/outline/hooks/usePresentationGeneration.ts /app/servers/nextjs/app/(presentation-generator)/outline/hooks/usePresentationGeneration.ts
COPY patches/nextjs/app/(presentation-generator)/outline/hooks/useOutlineStreaming.ts /app/servers/nextjs/app/(presentation-generator)/outline/hooks/useOutlineStreaming.ts

RUN cd /app/servers/nextjs && npm run build
