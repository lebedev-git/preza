/**
 * UploadPage Component
 * 
 * This component handles the presentation generation upload process, allowing users to:
 * - Configure presentation settings (slides, language)
 * - Input prompts
 * - Upload supporting documents
 * 
 * @component
 */

"use client";
import React, { useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import { useDispatch } from "react-redux";
import { clearOutlines, setOutlines, setPresentationId } from "@/store/slices/presentationGeneration";
import { ConfigurationSelects } from "./ConfigurationSelects";
import { PromptInput } from "./PromptInput";
import {  LanguageType, PresentationConfig, ToneType, VerbosityType } from "../type";
import SupportingDoc from "./SupportingDoc";
import { Button } from "@/components/ui/button";
import { ChevronRight, FileText, Loader2 } from "lucide-react";
import { toast } from "sonner";
import { PresentationGenerationApi } from "../../services/api/presentation-generation";
import { getHeader } from "../../services/api/header";
import { OverlayLoader } from "@/components/ui/overlay-loader";
import Wrapper from "@/components/Wrapper";
import { setPptGenUploadState } from "@/store/slices/presentationGenUpload";
import { trackEvent, MixpanelEvent } from "@/utils/mixpanel";
import { templates } from "@/app/presentation-templates";
import { TemplateLayoutsWithSettings } from "@/app/presentation-templates/utils";

// Types for loading state
interface LoadingState {
  isLoading: boolean;
  message: string;
  duration?: number;
  showProgress?: boolean;
  extra_info?: string;
}

interface PptxTextBlock {
  text: string;
  kind: string;
  left?: number | null;
  top?: number | null;
  width?: number | null;
  height?: number | null;
  canvas_left?: number | null;
  canvas_top?: number | null;
  canvas_width?: number | null;
  canvas_height?: number | null;
  source_order?: number;
  table_rows?: string[][];
  bullets?: string[];
  numeric_series?: Array<Record<string, unknown>>;
}

interface PptxSlideText {
  slide_number: number;
  text: string;
  xml_text: string;
  notes?: string | null;
  blocks: PptxTextBlock[];
  slide_width?: number;
  slide_height?: number;
  slide_aspect?: string;
  canvas_width?: number;
  canvas_height?: number;
  bullets?: string[];
  numeric_series?: Array<Record<string, unknown>>;
}

interface PptxTextPreview {
  fileName: string;
  slides: PptxSlideText[];
  total_slides: number;
}

const getDefaultPptxTemplate = (): TemplateLayoutsWithSettings => {
  const defaultTemplate = templates.find(
    (template: TemplateLayoutsWithSettings) => template.settings?.default
  );
  return defaultTemplate || templates[0];
};

const toPresentationLayoutPayload = (template: TemplateLayoutsWithSettings) => ({
  name: template.id,
  ordered: false,
  slides: template.layouts.map((layoutItem) => ({
    id: layoutItem.layoutId,
    name: layoutItem.layoutName,
    description: layoutItem.layoutDescription,
    templateID: template.id,
    templateName: template.name,
    json_schema: layoutItem.schemaJSON,
  })),
});

const stringifyPptxValue = (value: unknown): string => {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) {
    return value.map(stringifyPptxValue).filter(Boolean).join("\n");
  }
  if (typeof value === "object") {
    const objectValue = value as Record<string, unknown>;
    if (typeof objectValue.text === "string") return objectValue.text;
    if (typeof objectValue.content === "string") return objectValue.content;
    return Object.entries(objectValue)
      .map(([key, entryValue]) => {
        const stringValue = stringifyPptxValue(entryValue);
        return stringValue ? `${key}: ${stringValue}` : "";
      })
      .filter(Boolean)
      .join("\n");
  }
  return String(value);
};

const normalizePptxSlide = (slide: any): PptxSlideText => {
  const blocks = Array.isArray(slide?.blocks)
    ? slide.blocks.map((block: any) => ({
      ...block,
      text: stringifyPptxValue(block?.text),
      kind: typeof block?.kind === "string" ? block.kind : "text",
      source_order: Number.isFinite(Number(block?.source_order)) ? Number(block.source_order) : 0,
      table_rows: Array.isArray(block?.table_rows) ? block.table_rows : [],
      bullets: Array.isArray(block?.bullets) ? block.bullets.map(stringifyPptxValue).filter(Boolean) : [],
      numeric_series: Array.isArray(block?.numeric_series) ? block.numeric_series : [],
    }))
    : [];

  const text = stringifyPptxValue(slide?.text)
    || blocks.map((block: PptxTextBlock) => block.text).filter(Boolean).join("\n\n");

  return {
    slide_number: Number(slide?.slide_number) || 0,
    text,
    xml_text: stringifyPptxValue(slide?.xml_text),
    notes: slide?.notes ? stringifyPptxValue(slide.notes) : null,
    blocks,
    slide_width: Number(slide?.slide_width) || undefined,
    slide_height: Number(slide?.slide_height) || undefined,
    slide_aspect: typeof slide?.slide_aspect === "string" ? slide.slide_aspect : undefined,
    canvas_width: Number(slide?.canvas_width) || 1280,
    canvas_height: Number(slide?.canvas_height) || 720,
    bullets: Array.isArray(slide?.bullets) ? slide.bullets.map(stringifyPptxValue).filter(Boolean) : [],
    numeric_series: Array.isArray(slide?.numeric_series) ? slide.numeric_series : [],
  };
};

const UploadPage = () => {
  const router = useRouter();
  const pathname = usePathname();
  const dispatch = useDispatch();

  // State management
  const [files, setFiles] = useState<File[]>([]);
  const [pptxTextPreview, setPptxTextPreview] = useState<PptxTextPreview | null>(null);
  const [pptxTextLoading, setPptxTextLoading] = useState(false);
  const [pptxTextError, setPptxTextError] = useState<string | null>(null);
  const [config, setConfig] = useState<PresentationConfig>({
    slides: "8",
    language: LanguageType.English,
    prompt: "",
    tone: ToneType.Default,
    verbosity: VerbosityType.Standard,
    instructions: "",
    includeTableOfContents: false,
    includeTitleSlide: false,
    webSearch: false,
  });

  const [loadingState, setLoadingState] = useState<LoadingState>({
    isLoading: false,
    message: "",
    duration: 4,
    showProgress: false,
    extra_info: "",
  });

  /**
   * Updates the presentation configuration
   * @param key - Configuration key to update
   * @param value - New value for the configuration
   */
  const handleConfigChange = (key: keyof PresentationConfig, value: string) => {
    setConfig((prev) => ({ ...prev, [key]: value }));
  };

  const extractPptxText = async (pptxFiles: File[]) => {
    const [pptxFile] = pptxFiles;
    if (!pptxFile) return;

    setPptxTextLoading(true);
    setPptxTextError(null);
    setPptxTextPreview(null);

    try {
      const formData = new FormData();
      formData.append("pptx_file", pptxFile);

      const response = await fetch("/api/v1/ppt/pptx-slides/extract-text", {
        method: "POST",
        body: formData,
        cache: "no-cache",
      });

      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(errorText || "Could not extract text from the presentation");
      }

      const data = await response.json();
      setPptxTextPreview({
        fileName: pptxFile.name,
        slides: Array.isArray(data.slides) ? data.slides.map(normalizePptxSlide) : [],
        total_slides: data.total_slides || 0,
      });
    } catch (error: any) {
      console.error("PPTX text extraction failed", error);
      setPptxTextError(error.message || "Could not extract text from the presentation");
      toast.error("Could not extract presentation text");
    } finally {
      setPptxTextLoading(false);
    }
  };

  /**
   * Validates the current configuration and files
   * @returns boolean indicating if the configuration is valid
   */
  const validateConfiguration = (): boolean => {
    if (!config.language || !config.slides) {
      toast.error("Please select number of Slides & Language");
      return false;
    }

    if (!config.prompt.trim() && files.length === 0) {
      toast.error("No Prompt or Document Provided");
      return false;
    }
    return true;
  };

  /**
   * Handles the presentation generation process
   */
  const handleGeneratePresentation = async () => {
    if (!validateConfiguration()) return;

    try {
      if (pptxTextPreview?.slides?.length) {
        await handlePptxVerbatimPresentation();
        return;
      }

      const hasUploadedAssets = files.length > 0;

      if (hasUploadedAssets) {
        await handleDocumentProcessing();
      } else {
        await handleDirectPresentationGeneration();
      }
    } catch (error) {
      handleGenerationError(error);
    }
  };

  const handlePptxVerbatimPresentation = async () => {
    setLoadingState({
      isLoading: true,
      message: "Preparing extracted slide text...",
      showProgress: true,
      duration: 10,
    });

    const extractedOutlines = pptxTextPreview!.slides.map((slide) => ({
      content: slide.text || `Slide ${slide.slide_number}`,
    }));

    trackEvent(MixpanelEvent.Upload_Create_Presentation_API_Call);
    const visualInstructions = [
      "PPTX verbatim rebuild. Preserve every extracted source line as visible slide content.",
      "Use a 16:9 presentation canvas, choose a suitable layout for each slide, and prefer splitting dense slides over clipping text.",
      "Adjust font scale and spacing so slide text stays inside the frame.",
      config.prompt?.trim() ? `Visual direction from user: ${config.prompt.trim()}` : "",
      config.instructions?.trim() ? `Additional instructions: ${config.instructions.trim()}` : "",
    ].filter(Boolean).join("\n");

    const createResponse = await PresentationGenerationApi.createPresentation({
      content: extractedOutlines.map((slide) => slide.content).join("\n\n"),
      n_slides: extractedOutlines.length,
      file_paths: [],
      language: config?.language ?? "",
      tone: config?.tone,
      verbosity: config?.verbosity,
      instructions: visualInstructions,
      include_table_of_contents: false,
      include_title_slide: false,
      web_search: false,
    });

    setLoadingState({
      isLoading: true,
      message: "Repacking PPTX into 16:9 slides...",
      showProgress: true,
      duration: 60,
      extra_info: "Choosing layouts, building tables, charts, KPI blocks, and fitting text.",
    });

    const selectedTemplate = getDefaultPptxTemplate();
    if (!selectedTemplate?.layouts?.length) {
      throw new Error("No built-in presentation templates are available");
    }
    const layout = toPresentationLayoutPayload(selectedTemplate);

    const prepareResponse = await fetch("/api/v1/ppt/presentation/prepare-verbatim", {
      method: "POST",
      headers: getHeader(),
      body: JSON.stringify({
        presentation_id: createResponse.id,
        outlines: extractedOutlines,
        layout,
        use_llm_planner: true,
        pptx_slides: pptxTextPreview!.slides,
      }),
      cache: "no-cache",
    });

    if (!prepareResponse.ok) {
      const errorText = await prepareResponse.text();
      throw new Error(errorText || "Failed to prepare PPTX presentation");
    }
    await prepareResponse.json();

    dispatch(setPresentationId(createResponse.id));
    dispatch(setOutlines(extractedOutlines));
    window.sessionStorage.removeItem("presenton:pptx-verbatim-id");
    window.sessionStorage.removeItem("presenton:pptx-verbatim-outlines");
    window.sessionStorage.removeItem("presenton:pptx-verbatim-slides");
    trackEvent(MixpanelEvent.Navigation, { from: pathname, to: "/presentation" });
    router.push(`/presentation?id=${createResponse.id}&type=standard`);
  };

  /**
   * Handles document processing
   */
  const handleDocumentProcessing = async () => {
    setLoadingState({
      isLoading: true,
      message: "Processing documents...",
      showProgress: true,
      duration: 90,
      extra_info: files.length > 0 ? "It might take a few minutes for large documents." : "",
    });

    let documents = [];

    if (files.length > 0) {
      trackEvent(MixpanelEvent.Upload_Upload_Documents_API_Call);
      const uploadResponse = await PresentationGenerationApi.uploadDoc(files);
      documents = uploadResponse;
    }

    const promises: Promise<any>[] = [];

    if (documents.length > 0) {
      trackEvent(MixpanelEvent.Upload_Decompose_Documents_API_Call);
      promises.push(PresentationGenerationApi.decomposeDocuments(documents));
    }
    const responses = await Promise.all(promises);
    dispatch(setPptGenUploadState({
      config,
      files: responses,
    }));
    dispatch(clearOutlines())
    trackEvent(MixpanelEvent.Navigation, { from: pathname, to: "/documents-preview" });
    router.push("/documents-preview");
  };

  /**
   * Handles direct presentation generation without documents
   */
  const handleDirectPresentationGeneration = async () => {
    setLoadingState({
      isLoading: true,
      message: "Generating outlines...",
      showProgress: true,
      duration: 30,
    });

    // Use the first available layout group for direct generation
    trackEvent(MixpanelEvent.Upload_Create_Presentation_API_Call);
    const createResponse = await PresentationGenerationApi.createPresentation({
      content: config?.prompt ?? "",
      n_slides: config?.slides ? parseInt(config.slides) : null,
      file_paths: [],
      language: config?.language ?? "",
      tone: config?.tone,
      verbosity: config?.verbosity,
      instructions: config?.instructions || null,
      include_table_of_contents: !!config?.includeTableOfContents,
      include_title_slide: !!config?.includeTitleSlide,
      web_search: !!config?.webSearch,
    });


    dispatch(setPresentationId(createResponse.id));
    dispatch(clearOutlines())
    trackEvent(MixpanelEvent.Navigation, { from: pathname, to: "/outline" });
    router.push("/outline");
  };

  /**
   * Handles errors during presentation generation
   */
  const handleGenerationError = (error: any) => {
    console.error("Error in upload page", error);
    setLoadingState({
      isLoading: false,
      message: "",
      duration: 0,
      showProgress: false,
    });
    toast.error("Error", {
      description: error.message || "Error in upload page.",
    });
  };

  return (
    <Wrapper className="pb-10 lg:max-w-[70%] xl:max-w-[65%]">
      <OverlayLoader
        show={loadingState.isLoading}
        text={loadingState.message}
        showProgress={loadingState.showProgress}
        duration={loadingState.duration}
        extra_info={loadingState.extra_info}
      />
      <div className="flex flex-col gap-4 md:items-center md:flex-row justify-between py-4">
        <p></p>
        <ConfigurationSelects
          config={config}
          onConfigChange={handleConfigChange}
        />
      </div>

      <div className="relative">
        <PromptInput
          value={config.prompt}
          onChange={(value) => handleConfigChange("prompt", value)}
          data-testid="prompt-input"
        />
      </div>
      <SupportingDoc
        files={[...files]}
        onFilesChange={setFiles}
        onPptxFilesAdded={extractPptxText}
        data-testid="file-upload-input"
      />
      {(pptxTextLoading || pptxTextError || pptxTextPreview) && (
        <section className="mb-8 rounded-lg border border-[#d8d5ff] bg-white p-5 font-instrument_sans shadow-sm">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-md bg-[#5146E5]/10 text-[#5146E5]">
                <FileText className="h-5 w-5" />
              </div>
              <div>
                <h2 className="text-lg font-semibold text-[#242424]">
                  Slide text breakdown
                </h2>
                {pptxTextPreview && (
                  <p className="text-sm text-gray-500">
                    {pptxTextPreview.fileName} - {pptxTextPreview.total_slides} slides
                  </p>
                )}
              </div>
            </div>
            {pptxTextLoading && (
              <span className="inline-flex items-center gap-2 rounded-md bg-[#5146E5]/10 px-3 py-2 text-sm font-medium text-[#5146E5]">
                <Loader2 className="h-4 w-4 animate-spin" />
                Extracting text
              </span>
            )}
          </div>

          {pptxTextError && (
            <p className="mt-4 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
              {pptxTextError}
            </p>
          )}

          {pptxTextPreview && (
            <div className="mt-5 max-h-[420px] space-y-3 overflow-y-auto pr-2 custom_scrollbar">
              {pptxTextPreview.slides.map((slide) => (
                <article
                  key={slide.slide_number}
                  className="rounded-lg border border-gray-200 bg-[#fbfbff] p-4"
                >
                  <div className="mb-3 flex items-center justify-between gap-3">
                    <h3 className="text-sm font-semibold uppercase tracking-wide text-[#5146E5]">
                      Slide {slide.slide_number}
                    </h3>
                    <span className="text-xs text-gray-500">
                      {slide.blocks.length} text blocks
                    </span>
                  </div>
                  <pre className="whitespace-pre-wrap break-words text-sm leading-6 text-[#292929] font-instrument_sans">
                    {slide.text || "No readable text found on this slide."}
                  </pre>
                </article>
              ))}
            </div>
          )}
        </section>
      )}
      <Button
        onClick={handleGeneratePresentation}
        className="w-full rounded-[32px] flex items-center justify-center py-6 bg-[#5141e5] text-white font-instrument_sans font-semibold text-xl hover:bg-[#5141e5]/80 transition-colors duration-300"
        data-testid="next-button"
      >
        <span>Next</span>
        <ChevronRight className="!w-6 !h-6" />
      </Button>
    </Wrapper>
  );
};

export default UploadPage;
