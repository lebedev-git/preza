"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { RootState } from "@/store/store";
import { useDispatch, useSelector } from "react-redux";
import { OverlayLoader } from "@/components/ui/overlay-loader";
import Wrapper from "@/components/Wrapper";
import OutlineContent from "./OutlineContent";
import EmptyStateView from "./EmptyStateView";
import GenerateButton from "./GenerateButton";

import { TABS } from "../types/index";
import { useOutlineStreaming } from "../hooks/useOutlineStreaming";
import { useOutlineManagement } from "../hooks/useOutlineManagement";
import { usePresentationGeneration } from "../hooks/usePresentationGeneration";
import TemplateSelection from "./TemplateSelection";
import { TemplateLayoutsWithSettings } from "@/app/presentation-templates/utils";
import { templates } from "@/app/presentation-templates";
import { setOutlines } from "@/store/slices/presentationGeneration";

const readPptxSessionOutlines = (presentationId: string | null) => {
  if (typeof window === "undefined" || !presentationId) return [];
  if (window.sessionStorage.getItem("presenton:pptx-verbatim-id") !== presentationId) {
    return [];
  }

  try {
    const parsed = JSON.parse(
      window.sessionStorage.getItem("presenton:pptx-verbatim-outlines") || "[]"
    );
    return Array.isArray(parsed)
      ? parsed
        .map((item) => ({ content: typeof item?.content === "string" ? item.content : "" }))
        .filter((item) => item.content.trim())
      : [];
  } catch {
    return [];
  }
};

const getDefaultTemplate = () =>
  templates.find((template: TemplateLayoutsWithSettings & { default?: boolean }) => template.default) ||
  templates[0] ||
  null;

const OutlinePage: React.FC = () => {
  const dispatch = useDispatch();
  const { presentation_id, outlines } = useSelector(
    (state: RootState) => state.presentationGeneration
  );

  const [activeTab, setActiveTab] = useState<string>(TABS.OUTLINE);
  const [selectedTemplate, setSelectedTemplate] = useState<TemplateLayoutsWithSettings | string | null>(null);
  const autoStartedRef = useRef(false);

  const isPptxVerbatimFlow = useMemo(() => {
    if (typeof window === "undefined" || !presentation_id) return false;
    return window.sessionStorage.getItem("presenton:pptx-verbatim-id") === presentation_id;
  }, [presentation_id]);

  useEffect(() => {
    if (!isPptxVerbatimFlow || !presentation_id) return;

    if (!outlines?.length) {
      const restoredOutlines = readPptxSessionOutlines(presentation_id);
      if (restoredOutlines.length) {
        dispatch(setOutlines(restoredOutlines));
      }
    }

    if (!selectedTemplate) {
      setSelectedTemplate(getDefaultTemplate());
    }
  }, [dispatch, isPptxVerbatimFlow, outlines?.length, presentation_id, selectedTemplate]);

  const streamState = useOutlineStreaming(presentation_id);
  const { handleDragEnd, handleAddSlide } = useOutlineManagement(outlines);
  const { loadingState, handleSubmit } = usePresentationGeneration(
    presentation_id,
    outlines,
    selectedTemplate,
    setActiveTab
  );

  useEffect(() => {
    if (!isPptxVerbatimFlow) return;
    if (autoStartedRef.current) return;
    if (!presentation_id || !selectedTemplate || !outlines?.length) return;
    if (streamState.isLoading || streamState.isStreaming || loadingState.isLoading) return;

    autoStartedRef.current = true;
    handleSubmit();
  }, [
    handleSubmit,
    isPptxVerbatimFlow,
    loadingState.isLoading,
    outlines?.length,
    presentation_id,
    selectedTemplate,
    streamState.isLoading,
    streamState.isStreaming,
  ]);

  if (!presentation_id) {
    return <EmptyStateView />;
  }

  const overlayMessage = isPptxVerbatimFlow && loadingState.isLoading
    ? loadingState.message || "Repacking PPTX into 16:9 slides..."
    : loadingState.message;

  return (
    <div className="h-[calc(100vh-72px)]">
      <OverlayLoader
        show={loadingState.isLoading}
        text={overlayMessage}
        showProgress={loadingState.showProgress}
        duration={loadingState.duration}
      />

      <Wrapper className="h-full flex flex-col w-full">
        <div className="flex-grow overflow-y-hidden w-[1200px] mx-auto">
          <Tabs value={activeTab} onValueChange={setActiveTab} className="h-full flex flex-col">
            <TabsList className="grid w-[50%] mx-auto my-4 grid-cols-2">
              <TabsTrigger value={TABS.OUTLINE}>Outline & Content</TabsTrigger>
              <TabsTrigger value={TABS.LAYOUTS}>Select Template</TabsTrigger>
            </TabsList>

            <div className="flex-grow w-full mx-auto">
              <TabsContent
                value={TABS.OUTLINE}
                className="h-[calc(100vh-16rem)] overflow-y-auto custom_scrollbar"
              >
                <OutlineContent
                  outlines={outlines}
                  isLoading={streamState.isLoading}
                  isStreaming={streamState.isStreaming}
                  activeSlideIndex={streamState.activeSlideIndex}
                  highestActiveIndex={streamState.highestActiveIndex}
                  onDragEnd={handleDragEnd}
                  onAddSlide={handleAddSlide}
                />
              </TabsContent>

              <TabsContent value={TABS.LAYOUTS} className="h-[calc(100vh-16rem)] overflow-y-auto custom_scrollbar">
                <TemplateSelection
                  selectedTemplate={selectedTemplate}
                  onSelectTemplate={setSelectedTemplate}
                />
              </TabsContent>
            </div>
          </Tabs>
        </div>

        <div className="py-4 border-t border-gray-200">
          <div className="max-w-[1200px] mx-auto">
            <GenerateButton
              outlineCount={outlines.length}
              loadingState={loadingState}
              streamState={streamState}
              selectedTemplate={selectedTemplate}
              onSubmit={handleSubmit}
            />
          </div>
        </div>
      </Wrapper>
    </div>
  );
};

export default OutlinePage;
