"use client";

import React, { useLayoutEffect, useMemo, useRef, useState } from "react";
import EditableLayoutWrapper from "../components/EditableLayoutWrapper";
import SlideErrorBoundary from "../components/SlideErrorBoundary";
import TiptapTextReplacer from "../components/TiptapTextReplacer";
import { validate as uuidValidate } from 'uuid';
import { getLayoutByLayoutId } from "@/app/presentation-templates";
import { useCustomTemplateDetails } from "@/app/hooks/useCustomTemplates";
import { updateSlideContent } from "@/store/slices/presentationGeneration";
import { useDispatch } from "react-redux";
import { Loader2 } from "lucide-react";

const stringifyLayoutValue = (value: unknown): string => {
    if (value === null || value === undefined) return "";
    if (typeof value === "string") return value;
    if (typeof value === "number" || typeof value === "boolean") return String(value);
    if (Array.isArray(value)) return value.map(stringifyLayoutValue).filter(Boolean).join("\n");
    if (typeof value === "object") {
        const objectValue = value as Record<string, unknown>;
        for (const key of ["text", "content", "description", "title", "label", "name"]) {
            if (typeof objectValue[key] === "string") return objectValue[key] as string;
        }
        return Object.values(objectValue).map(stringifyLayoutValue).filter(Boolean).join("\n");
    }
    return String(value);
};

const getSchemaType = (schema: any): string | null => {
    if (!schema || typeof schema !== "object") return null;
    if (Array.isArray(schema.type)) {
        return schema.type.find((type: string) => type !== "null") ?? null;
    }
    if (typeof schema.type === "string") return schema.type;
    for (const unionKey of ["anyOf", "oneOf", "allOf"]) {
        if (Array.isArray(schema[unionKey])) {
            for (const unionSchema of schema[unionKey]) {
                const unionType = getSchemaType(unionSchema);
                if (unionType) return unionType;
            }
        }
    }
    return null;
};

const sanitizeValueForSchema = (schema: any, value: unknown): unknown => {
    const schemaType = getSchemaType(schema);

    if (schemaType === "array") {
        const items = Array.isArray(value) ? value : [];
        return items.map((item) => sanitizeValueForSchema(schema?.items, item));
    }

    if (schemaType === "object") {
        const source = value && typeof value === "object" && !Array.isArray(value)
            ? value as Record<string, unknown>
            : {};
        return Object.fromEntries(
            Object.entries(schema?.properties ?? {}).map(([key, childSchema]) => [
                key,
                sanitizeValueForSchema(childSchema, source[key]),
            ])
        );
    }

    if (schemaType === "string") return stringifyLayoutValue(value);
    if (schemaType === "number" || schemaType === "integer") {
        const numericValue = Number(value);
        return Number.isFinite(numericValue) ? numericValue : 0;
    }
    if (schemaType === "boolean") return Boolean(value);
    return sanitizeUnknownValue(value);
};

const sanitizeUnknownValue = (value: unknown): unknown => {
    if (Array.isArray(value)) return value.map(sanitizeUnknownValue);
    if (value && typeof value === "object") {
        const entries = Object.entries(value as Record<string, unknown>);
        if (entries.length === 0) return "";
        return Object.fromEntries(entries.map(([key, childValue]) => [key, sanitizeUnknownValue(childValue)]));
    }
    return value ?? "";
};

const sanitizeLayoutData = (content: any, schema?: any) => {
    const source = content && typeof content === "object" ? content : {};
    const metadata = Object.fromEntries(
        Object.entries(source)
            .filter(([key]) => key.startsWith("__"))
            .map(([key, value]) => [key, sanitizeUnknownValue(value)])
    );
    const sanitized = schema?.properties
        ? Object.fromEntries(
            Object.entries(schema.properties).map(([key, propertySchema]) => [
                key,
                sanitizeValueForSchema(propertySchema, source[key]),
            ])
        )
        : Object.fromEntries(
            Object.entries(source).map(([key, value]) => [key, sanitizeUnknownValue(value)])
        );

    return {
        ...sanitized,
        ...metadata,
        __speaker_note__: stringifyLayoutValue(source.__speaker_note__),
        __verbatim_import__: Boolean(source.__verbatim_import__),
        __verbatim_full_text__: stringifyLayoutValue(source.__verbatim_full_text__),
        __verbatim_source_full_text__: stringifyLayoutValue(source.__verbatim_source_full_text__),
        __verbatim_density__: typeof source.__verbatim_density__ === "string" ? source.__verbatim_density__ : "normal",
        __verbatim_family__: typeof source.__verbatim_family__ === "string" ? source.__verbatim_family__ : "dense-text",
        __verbatim_canvas_aspect__: typeof source.__verbatim_canvas_aspect__ === "string" ? source.__verbatim_canvas_aspect__ : "16:9",
        __verbatim_fit_policy__: typeof source.__verbatim_fit_policy__ === "string" ? source.__verbatim_fit_policy__ : "fit-content-inside-16x9",
        __verbatim_render_mode__: typeof source.__verbatim_render_mode__ === "string" ? source.__verbatim_render_mode__ : "verbatim-canvas",
        __selected_layout_id__: stringifyLayoutValue(source.__selected_layout_id__),
        __selected_layout_name__: stringifyLayoutValue(source.__selected_layout_name__),
        __selected_layout_description__: stringifyLayoutValue(source.__selected_layout_description__),
    };
};

const splitVerbatimText = (text: string) => {
    const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    const title = lines[0] ?? "";
    const bodyLines = lines.slice(1);
    const body = bodyLines.join("\n");
    const paragraphs = body
        .split(/\n{2,}/)
        .map((part) => part.trim())
        .filter(Boolean);
    return { title, body, bodyLines, paragraphs: paragraphs.length ? paragraphs : bodyLines };
};

const splitBalancedColumns = (parts: string[]) => {
    if (parts.length <= 1) return [parts, []] as const;
    const totalLength = parts.reduce((sum, part) => sum + part.length, 0);
    let runningLength = 0;
    let splitIndex = 1;
    for (let index = 0; index < parts.length; index += 1) {
        runningLength += parts[index].length;
        if (runningLength >= totalLength / 2) {
            splitIndex = index + 1;
            break;
        }
    }
    return [parts.slice(0, splitIndex), parts.slice(splitIndex)] as const;
};

const parseVerbatimTable = (bodyLines: string[]) => {
    const rows = bodyLines
        .filter((line) => line.includes("|") || line.includes("\t"))
        .map((line) => line.split(line.includes("|") ? "|" : "\t").map((cell) => cell.trim()).filter(Boolean))
        .filter((row) => row.length > 0);
    if (!rows.length) return null;
    return {
        columns: rows[0],
        rows: rows.slice(1),
    };
};

const parseVerbatimBullets = (bodyLines: string[]) =>
    bodyLines
        .filter((line) => /^(\d+[\).\s]|[-*•])/.test(line))
        .map((line) => line.replace(/^(\d+[\).\s]+|[-*•]\s*)/, "").trim())
        .filter(Boolean);

const parseCleanVerbatimBullets = (bodyLines: string[]) => {
    const cleanBullets = bodyLines
        .filter((line) => /^\s*(?:\d{1,2}[\).]\s+|[-*\u2022]\s*)/.test(line))
        .map((line) => line.replace(/^\s*(?:\d{1,2}[\).]\s+|[-*\u2022]\s*)/, "").trim())
        .filter(Boolean);
    return cleanBullets.length ? cleanBullets : parseVerbatimBullets(bodyLines);
};

type VerbatimMetric = {
    label: string;
    value: string;
    suffix: string;
};

type VerbatimTable = {
    columns: string[];
    rows: string[][];
};

type VerbatimChartPoint = {
    label: string;
    value: number;
    displayValue?: string;
};

type VerbatimRoadmapItem = {
    marker: string;
    heading: string;
    details: string[];
    amount: string;
    dateHint: string;
    order: number;
};

type VerbatimRoadmapTotal = {
    year: string;
    value: string;
};

type VerbatimSection = {
    heading: string;
    lines: string[];
};

const parseNumberedSections = (bodyLines: string[]): VerbatimSection[] => {
    const sections: VerbatimSection[] = [];
    let current: VerbatimSection | null = null;

    bodyLines.forEach((line) => {
        const numberedMatch = line.match(/^\s*\d{1,2}[\).]\s*(.+)$/);
        if (numberedMatch) {
            current = { heading: numberedMatch[1].trim(), lines: [] };
            sections.push(current);
            return;
        }

        if (current) {
            current.lines.push(line);
        }
    });

    return sections.filter((section) => section.heading && (section.lines.length || sections.length >= 2));
};

const ROADMAP_HEADING_PATTERN = /^\s*(?:(?:этап|шаг|stage|step|phase)\s*\d+|итого|total|20\d{2}(?:\s*[–-]\s*20\d{2})?)/i;
const ROADMAP_TITLE_PATTERN = /(дорожн|roadmap|план|этап|stage|phase|timeline|202\d|203\d)/i;
const ROADMAP_DECORATIVE_NUMBER_PATTERN = /^\s*\d{1,2}\s*$/;

const extractAmountHint = (line: string) => {
    const match = line.match(/\b\d[\d\s.,]*(?:\s*(?:млн|тыс|руб|₽|%|k|m|bn))\b/i);
    return match?.[0]?.trim() ?? "";
};

const extractDateHint = (line: string) => {
    const yearRange = line.match(/20\d{2}\s*[–-]\s*20\d{2}|20\d{2}/);
    const quarter = line.match(/\(?\s*\d(?:\s*[–-]\s*\d)?\s*кв\.?\s*20\d{2}\s*\)?/i);
    return (quarter?.[0] ?? yearRange?.[0] ?? "").replace(/[()]/g, "").trim();
};

const parseRoadmapHeading = (line: string) => {
    const markerMatch = line.match(/^\s*((?:этап|шаг|stage|step|phase)\s*(\d+)|итого|total|20\d{2}(?:\s*[–-]\s*20\d{2})?)/i);
    const marker = markerMatch?.[1]?.trim() ?? "";
    const heading = marker
        ? line.slice(markerMatch?.[0].length ?? 0).replace(/^[\s:.-]+/, "").trim()
        : line.trim();
    const order = markerMatch?.[2] ? Number(markerMatch[2]) : Number.MAX_SAFE_INTEGER;
    return {
        marker: marker || "Этап",
        heading: heading || line.trim(),
        order: Number.isFinite(order) ? order : Number.MAX_SAFE_INTEGER,
    };
};

const parseRoadmapTotals = (bodyLines: string[]): VerbatimRoadmapTotal[] =>
    bodyLines
        .map((line) => {
            const match = line.match(/^\s*(20\d{2})\s*[:\-–]\s*(.+)$/);
            if (!match) return null;
            return {
                year: match[1],
                value: match[2].trim(),
            };
        })
        .filter((item): item is VerbatimRoadmapTotal => Boolean(item));

const parseVerbatimRoadmap = (title: string, bodyLines: string[]): VerbatimRoadmapItem[] => {
    const hasRoadmapSignal = ROADMAP_TITLE_PATTERN.test(title) || bodyLines.some((line) => ROADMAP_HEADING_PATTERN.test(line));
    if (!hasRoadmapSignal) return [];

    const items: VerbatimRoadmapItem[] = [];
    for (const line of bodyLines) {
        if (ROADMAP_DECORATIVE_NUMBER_PATTERN.test(line)) continue;
        if (/^\s*(?:итого|total)\s*:?$/i.test(line)) continue;
        if (/^\s*20\d{2}\s*[:\-–]\s*/.test(line)) continue;

        if (ROADMAP_HEADING_PATTERN.test(line)) {
            const parsed = parseRoadmapHeading(line);
            items.push({
                ...parsed,
                details: [],
                amount: extractAmountHint(line),
                dateHint: extractDateHint(line),
            });
            continue;
        }

        if (!items.length) continue;
        const current = items[items.length - 1];
        current.details.push(line);
        current.amount = current.amount || extractAmountHint(line);
        current.dateHint = current.dateHint || extractDateHint(line);
    }

    const meaningfulItems = items.filter((item) => item.heading || item.details.length);
    return [...meaningfulItems].sort((a, b) => {
        if (a.order === Number.MAX_SAFE_INTEGER && b.order === Number.MAX_SAFE_INTEGER) return 0;
        return a.order - b.order;
    });
};

const isLikelySectionHeading = (line: string, nextLine?: string) => {
    const normalized = line.trim();
    if (!normalized || !nextLine) return false;
    if (normalized.length > 74) return false;
    if (/^\d+[\).]/.test(normalized)) return false;
    if (/[.!?;:]$/.test(normalized)) return false;
    if (!/[A-Za-zА-Яа-яЁё]/.test(normalized)) return false;
    if (nextLine.trim().length < 24) return false;
    return true;
};

const parseVerbatimSections = (bodyLines: string[]): VerbatimSection[] => {
    const sections: VerbatimSection[] = [];
    let current: VerbatimSection | null = null;

    bodyLines.forEach((line, index) => {
        if (isLikelySectionHeading(line, bodyLines[index + 1])) {
            current = { heading: line.trim(), lines: [] };
            sections.push(current);
            return;
        }

        if (!current) {
            current = { heading: "", lines: [] };
            sections.push(current);
        }
        current.lines.push(line);
    });

    const meaningful = sections.filter((section) => section.heading && section.lines.length);
    return meaningful.length >= 2 ? meaningful : [];
};

const parseVerbatimMetrics = (bodyLines: string[]): VerbatimMetric[] =>
    bodyLines
        .map((line) => {
            const match = line.match(/[-+]?\d[\d\s.,]*(?:%|[A-Za-z]{1,5})?/);
            if (!match) return null;
            const rawValue = match[0].trim();
            const suffixMatch = rawValue.match(/[A-Za-z%]+$/);
            const suffix = suffixMatch ? suffixMatch[0] : "";
            const value = suffix ? rawValue.slice(0, -suffix.length).trim() : rawValue;
            const label = line
                .replace(match[0], "")
                .replace(/^[-*\u2022\s:]+/, "")
                .replace(/[:\-]+$/g, "")
                .trim();
            return {
                label: label || line,
                value: value || rawValue,
                suffix,
            };
        })
        .filter((metric): metric is VerbatimMetric => Boolean(metric));

const toFiniteNumber = (value: unknown): number | null => {
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (typeof value !== "string") return null;
    const match = value.replace(/\s/g, "").replace(",", ".").match(/[-+]?\d+(?:\.\d+)?/);
    if (!match) return null;
    const numericValue = Number(match[0]);
    return Number.isFinite(numericValue) ? numericValue : null;
};

const normalizeStructuredTable = (value: unknown): VerbatimTable | null => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    const table = value as Record<string, unknown>;
    const columns = Array.isArray(table.columns)
        ? table.columns.map(stringifyLayoutValue).filter(Boolean)
        : [];
    const rows = Array.isArray(table.rows)
        ? table.rows
            .filter(Array.isArray)
            .map((row) => row.map(stringifyLayoutValue))
            .filter((row) => row.some(Boolean))
        : [];
    if (!columns.length || !rows.length) return null;
    return { columns, rows };
};

const findStructuredTable = (data: Record<string, unknown>): VerbatimTable | null => {
    for (const [key, value] of Object.entries(data)) {
        if (key.startsWith("__")) continue;
        if (key.toLowerCase().includes("table")) {
            const table = normalizeStructuredTable(value);
            if (table) return table;
        }
    }
    return null;
};

const normalizeChartPoints = (value: unknown): VerbatimChartPoint[] => {
    if (Array.isArray(value)) {
        return value
            .map((item, index) => {
                if (typeof item === "number") return { label: `${index + 1}`, value: item };
                if (!item || typeof item !== "object" || Array.isArray(item)) return null;
                const point = item as Record<string, unknown>;
                const label = stringifyLayoutValue(point.label ?? point.name ?? point.category ?? point.x ?? `${index + 1}`);
                const numericValue = toFiniteNumber(point.value ?? point.amount ?? point.count ?? point.y);
                if (numericValue === null) return null;
                return {
                    label,
                    value: numericValue,
                    displayValue: stringifyLayoutValue(point.displayValue ?? point.raw ?? point.value),
                };
            })
            .filter((point): point is VerbatimChartPoint => Boolean(point));
    }
    if (!value || typeof value !== "object") return [];
    const chart = value as Record<string, unknown>;
    const dataPoints = normalizeChartPoints(chart.data ?? chart.points ?? chart.items);
    if (dataPoints.length) return dataPoints;
    const categories = Array.isArray(chart.categories) ? chart.categories.map(stringifyLayoutValue) : [];
    const series = Array.isArray(chart.series) ? chart.series[0] : null;
    const valuesSource = series && typeof series === "object" && !Array.isArray(series)
        ? ((series as Record<string, unknown>).values ?? (series as Record<string, unknown>).data)
        : chart.values;
    const values = Array.isArray(valuesSource) ? valuesSource.map(toFiniteNumber) : [];
    return values
        .map((numericValue, index) => numericValue === null ? null : {
            label: categories[index] || `${index + 1}`,
            value: numericValue,
        })
        .filter((point): point is VerbatimChartPoint => Boolean(point));
};

const findStructuredChart = (data: Record<string, unknown>): VerbatimChartPoint[] => {
    for (const [key, value] of Object.entries(data)) {
        if (key.startsWith("__")) continue;
        if (/(chart|graph)/i.test(key)) {
            const points = normalizeChartPoints(value);
            if (points.length) return points;
        }
    }
    return [];
};

const getBodyClass = (density: string, textLength: number) => {
    if (density === "dense" || textLength > 850) return "text-[14px] leading-[1.34]";
    if (density === "medium" || textLength > 520) return "text-[16px] leading-[1.42]";
    return "text-[18px] leading-[1.5]";
};

const getHeadingClass = (density: string, titleLength: number) => {
    if (density === "dense" || titleLength > 88) return "text-[clamp(28px,3.2vw,40px)] leading-[1.04]";
    if (density === "medium" || titleLength > 58) return "text-[clamp(32px,3.4vw,44px)] leading-[1.04]";
    return "text-[clamp(36px,3.8vw,48px)] leading-[1.02]";
};

const VerbatimImportFallback = ({ data, density }: { data: any; density: string }) => {
    const contentData = data && typeof data === "object" ? data as Record<string, unknown> : {};
    const fullText = stringifyLayoutValue(data.__verbatim_full_text__ || data.__speaker_note__);
    const family = typeof data.__verbatim_family__ === "string" ? data.__verbatim_family__ : "dense-text";
    const detectedFamily = typeof data.__verbatim_detected_family__ === "string" ? data.__verbatim_detected_family__ : family;
    const continuationPart = Number(data.__continuation_part_index__ ?? 0);
    const continuationCount = Number(data.__continuation_part_count__ ?? 1);
    const { title, body, bodyLines, paragraphs } = splitVerbatimText(fullText);
    const bullets = parseCleanVerbatimBullets(bodyLines);
    const table = findStructuredTable(contentData) ?? parseVerbatimTable(bodyLines);
    const roadmapItems = parseVerbatimRoadmap(title, bodyLines);
    const roadmapTotals = parseRoadmapTotals(bodyLines);
    const numberedSections = parseNumberedSections(bodyLines);
    const sections = numberedSections.length >= 2 ? numberedSections : parseVerbatimSections(bodyLines);
    const metrics = parseVerbatimMetrics(bodyLines);
    const structuredChartPoints = findStructuredChart(contentData);
    const metricChartPoints = metrics
        .reduce<VerbatimChartPoint[]>((items, metric) => {
            const numericValue = toFiniteNumber(metric.value);
            if (numericValue === null) return items;
            items.push({
                label: metric.label,
                value: numericValue,
                displayValue: `${metric.value}${metric.suffix}`,
            });
            return items;
        }, []);
    const chartPoints = structuredChartPoints.length ? structuredChartPoints : metricChartPoints;
    const chartMaxValue = Math.max(...chartPoints.map((point) => Math.abs(point.value)), 1);
    const [leftColumn, rightColumn] = splitBalancedColumns(paragraphs);
    const showRoadmap = !table && roadmapItems.length >= 2;
    const showSectionDeck = !showRoadmap && !table && sections.length >= 2;
    const showBalancedSectionCards = showSectionDeck && numberedSections.length >= 2 && numberedSections.length <= 4;
    const compactNumberedSections = showBalancedSectionCards
        && fullText.length <= 980
        && sections.every((section) => section.lines.length <= 4)
        && sections.every((section) => stringifyLayoutValue(section.heading).length <= 110);
    const showChart = !showRoadmap && !showSectionDeck && !table && chartPoints.length >= 3 && (family === "chart" || detectedFamily === "chart");
    const showMetrics = !showChart && !showRoadmap && !showSectionDeck && !table && metrics.length >= 3 && metrics.length <= 6 && fullText.length < 760;
    const showBulletGrid = !showMetrics && !showSectionDeck && family === "bullet" && (bullets.length > 0 || bodyLines.length >= 3);
    const bulletItems = bullets.length ? bullets : bodyLines;
    const showTwoColumns = !showRoadmap && !showSectionDeck && family === "dense-text" && (paragraphs.length >= 3 || body.length > 620);
    const headingClass = getHeadingClass(density, title.length);
    const bodyClass = getBodyClass(density, fullText.length);
    const kickerClass = "text-[11px] font-semibold uppercase tracking-[0.22em] text-[#71717a]/80";
    const tableTextClass = density === "dense" || (table?.rows.length ?? 0) > 6
        ? "text-[12px] leading-[1.28]"
        : "text-[13px] leading-[1.36]";
    const roadmapGridClass = roadmapItems.length <= 2
        ? "grid-cols-2"
        : "grid-cols-3";
    const roadmapTextClass = roadmapItems.length > 4 || fullText.length > 900
        ? "text-[12px] leading-[1.28]"
        : "text-[13px] leading-[1.34]";
    const roadmapPalette = ["#0f766e", "#1d4ed8", "#be123c", "#ca8a04", "#6d28d9", "#15803d"];
    const sectionTextClass = fullText.length > 1050
        ? "text-[13px] leading-[1.28]"
        : fullText.length > 760
            ? "text-[14px] leading-[1.34]"
            : "text-[15px] leading-[1.4]";
    const sourceSlideNumber = Number(data.__source_slide_number__ ?? continuationPart + 1);
    const showGhostNumeral = continuationPart === 0 && (family === "cover" || (fullText.length <= 220 && sections.length <= 1));
    const ghostNumeral = String(Number.isFinite(sourceSlideNumber) ? sourceSlideNumber : continuationPart + 1).padStart(2, "0");

    return (
        <div className="relative h-full w-full overflow-hidden bg-[linear-gradient(135deg,#fafafa_0%,#f4f4f5_55%,#eef6f5_100%)] text-[#18181b]">
            <div className="absolute left-0 top-0 h-full w-1 bg-[#0f766e]" />
            {showGhostNumeral ? (
                <div className="pointer-events-none absolute right-3 top-0 select-none text-[140px] font-black leading-none text-[#18181b]/[0.05]">
                    {ghostNumeral}
                </div>
            ) : null}
            <div className="flex h-full flex-col gap-5 px-[58px] py-[42px]">
                <div className="flex items-start justify-between gap-6">
                    <div className="min-w-0 max-w-[78%]">
                        <div className="mb-4 h-1 w-16 rounded-full bg-[#0f766e]/35" />
                        <h1 className={`${headingClass} font-semibold tracking-normal text-balance`}>
                            {title || "Untitled slide"}
                        </h1>
                    </div>
                    {continuationCount > 1 ? (
                        <div className="shrink-0 rounded-md border border-[#d4d4d8] bg-white px-3 py-2 text-[12px] font-semibold text-[#52525b]">
                            Part {continuationPart + 1} of {continuationCount}
                        </div>
                    ) : null}
                </div>

                <div className="min-h-[70vh] flex-1">
                {showRoadmap ? (
                    <div className="flex min-h-0 h-full flex-1 flex-col gap-4">
                        <div className={`grid min-h-0 flex-1 auto-rows-fr ${roadmapGridClass} gap-4`}>
                            {roadmapItems.map((item, index) => {
                                const accent = roadmapPalette[index % roadmapPalette.length];
                                return (
                                    <div key={`${item.marker}-${index}`} className="relative min-h-0 overflow-hidden rounded-2xl border border-[#d4d4d8] bg-white p-5 shadow-[0_12px_30px_rgba(24,24,27,0.05)]">
                                        <div className="absolute left-0 top-0 h-full w-1.5" style={{ backgroundColor: accent }} />
                                        <div className="mb-3 flex items-start justify-between gap-3 pl-2">
                                            <div className="min-w-0">
                                                <p className={`mb-1 ${kickerClass}`} style={{ color: accent }}>{item.marker}</p>
                                                <h3 className="text-[18px] font-semibold leading-[1.12] text-[#18181b]">{item.heading}</h3>
                                            </div>
                                            {item.amount || item.dateHint ? (
                                                <div className="shrink-0 space-y-1 text-right">
                                                    {item.amount ? <p className="rounded-md bg-[#f4f4f5] px-2 py-1 text-[12px] font-semibold text-[#18181b]">{item.amount}</p> : null}
                                                    {item.dateHint ? <p className="text-[12px] font-medium text-[#71717a]">{item.dateHint}</p> : null}
                                                </div>
                                            ) : null}
                                        </div>
                                        <div className="space-y-1.5 pl-2">
                                            {item.details.map((detail, detailIndex) => (
                                                <p key={detailIndex} className={`${roadmapTextClass} text-[#3f3f46]`}>
                                                    {detail}
                                                </p>
                                            ))}
                                        </div>
                                    </div>
                                );
                            })}
                        </div>
                        {roadmapTotals.length ? (
                            <div className="grid shrink-0 grid-cols-3 gap-3">
                                {roadmapTotals.slice(0, 3).map((total, index) => {
                                    const accent = roadmapPalette[index % roadmapPalette.length];
                                    return (
                                        <div key={`${total.year}-${index}`} className="rounded-xl border border-[#d4d4d8] bg-white px-4 py-3">
                                            <p className="text-[12px] font-semibold" style={{ color: accent }}>{total.year}</p>
                                            <p className="mt-1 text-[16px] font-semibold leading-tight text-[#18181b]">{total.value}</p>
                                        </div>
                                    );
                                })}
                            </div>
                        ) : null}
                    </div>
                ) : showBalancedSectionCards ? (
                    <div className={`grid min-h-0 gap-4 ${compactNumberedSections ? "content-start auto-rows-max self-start" : "h-full flex-1 auto-rows-fr"} ${sections.length === 2 ? "grid-cols-2" : sections.length === 3 ? "grid-cols-3" : "grid-cols-2"}`}>
                        {sections.map((section, index) => {
                            const accent = roadmapPalette[index % roadmapPalette.length];
                            return (
                                <div key={`${section.heading}-${index}`} className={`relative flex min-h-0 flex-col overflow-hidden rounded-2xl border border-[#d4d4d8] bg-white p-5 shadow-[0_12px_30px_rgba(24,24,27,0.05)] ${compactNumberedSections ? "self-start" : ""}`}>
                                    <div className="absolute left-0 top-0 h-full w-1.5" style={{ backgroundColor: accent }} />
                                    <div className="mb-4 flex items-start gap-3 pl-2">
                                        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md text-[15px] font-semibold text-white" style={{ backgroundColor: accent }}>
                                            {index + 1}
                                        </div>
                                        <h3 className="min-w-0 text-[18px] font-semibold leading-[1.12] text-[#18181b]">{section.heading}</h3>
                                    </div>
                                    <div className={`min-h-0 space-y-2 pl-2 ${compactNumberedSections ? "" : "overflow-hidden"}`}>
                                        {section.lines.map((line, lineIndex) => (
                                            <p key={lineIndex} className={`${sectionTextClass} text-[#3f3f46]`}>
                                                {line}
                                            </p>
                                        ))}
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                ) : showSectionDeck ? (
                    <div className="grid min-h-0 flex-1 grid-cols-[0.72fr_1.28fr] gap-6">
                        <div className="relative flex min-h-0 flex-col justify-between overflow-hidden rounded-2xl bg-[#18181b] p-6 text-white">
                            <div>
                                <div className="mb-5 h-1 w-24 rounded-full bg-[#0f766e]" />
                                <h2 className="text-[27px] font-semibold leading-[1.08] text-balance">{title || "Imported slide"}</h2>
                            </div>
                            <div className="mt-6 grid grid-cols-2 gap-3">
                                {sections.slice(0, 4).map((section, index) => (
                                    <div key={`${section.heading}-${index}`} className="border-t border-white/20 pt-3">
                                        <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-[#99f6e4]">0{index + 1}</p>
                                        <p className="mt-1 text-[13px] font-medium leading-[1.24] text-white/80">{section.heading}</p>
                                    </div>
                                ))}
                            </div>
                        </div>
                        <div className={`grid min-h-0 auto-rows-fr gap-3 ${sections.length <= 2 ? "grid-cols-2" : "grid-cols-2"}`}>
                            {sections.map((section, index) => {
                                const accent = roadmapPalette[index % roadmapPalette.length];
                                return (
                                    <div key={`${section.heading}-${index}`} className="relative min-h-0 overflow-hidden rounded-xl border border-[#d4d4d8] bg-white p-4">
                                        <div className="mb-3 flex items-center gap-3">
                                            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-[12px] font-semibold text-white" style={{ backgroundColor: accent }}>
                                                {index + 1}
                                            </div>
                                            <h3 className="text-[16px] font-semibold leading-[1.14] text-[#18181b]">{section.heading}</h3>
                                        </div>
                                        <div className="space-y-1.5">
                                            {section.lines.map((line, lineIndex) => (
                                                <p key={lineIndex} className={`${sectionTextClass} text-[#3f3f46]`}>
                                                    {line}
                                                </p>
                                            ))}
                                        </div>
                                    </div>
                                );
                            })}
                        </div>
                    </div>
                ) : family === "cover" ? (
                    <div className="flex flex-1 items-center">
                        <p className={`${bodyClass} max-w-[74%] whitespace-pre-line text-[#3f3f46]`}>
                            {body}
                        </p>
                    </div>
                ) : table ? (
                    <div className="min-h-0 h-full flex-1 rounded-2xl border border-[#d4d4d8] bg-white">
                        <table className="h-full w-full table-fixed text-left">
                            <thead>
                                <tr className="bg-[#f4f4f5]">
                                    {table.columns.map((column, index) => (
                                        <th key={index} className="border-b border-[#d4d4d8] px-3 py-2 text-[12px] font-semibold text-[#27272a]">
                                            {column}
                                        </th>
                                    ))}
                                </tr>
                            </thead>
                            <tbody>
                                {table.rows.map((row, rowIndex) => (
                                    <tr key={rowIndex} className="align-top">
                                        {table.columns.map((_, cellIndex) => (
                                            <td key={cellIndex} className={`${tableTextClass} border-b border-[#e4e4e7] px-3 py-2 text-[#3f3f46]`}>
                                                {row[cellIndex] ?? ""}
                                            </td>
                                        ))}
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                ) : showChart ? (
                    <div className="grid min-h-0 flex-1 grid-cols-[0.72fr_1.28fr] gap-6">
                        <div className="flex min-h-0 flex-col justify-between rounded-2xl bg-[#18181b] p-6 text-white">
                            <div>
                                <p className="mb-4 text-[11px] font-semibold uppercase tracking-[0.2em] text-[#99f6e4]">Data</p>
                                <h2 className="text-[28px] font-semibold leading-[1.08] text-balance">{title || "Chart"}</h2>
                            </div>
                            <div className="mt-6 space-y-2">
                                {chartPoints.slice(0, 4).map((point, index) => (
                                    <div key={`${point.label}-${index}`} className="flex items-center justify-between gap-4 border-t border-white/15 pt-2 text-[13px]">
                                        <span className="truncate text-white/72">{point.label}</span>
                                        <span className="font-semibold text-white">{point.displayValue || point.value}</span>
                                    </div>
                                ))}
                            </div>
                        </div>
                        <div className="min-h-0 rounded-2xl border border-[#d4d4d8] bg-white p-5">
                            <div className="flex h-full items-end gap-3">
                                {chartPoints.slice(0, 8).map((point, index) => {
                                    const height = Math.max(8, Math.round((Math.abs(point.value) / chartMaxValue) * 100));
                                    return (
                                        <div key={`${point.label}-${index}`} className="flex min-w-0 flex-1 flex-col items-center gap-2">
                                            <div className="flex h-full w-full items-end rounded-md bg-[#f4f4f5] px-1.5 pt-3">
                                                <div
                                                    className="w-full rounded-t-md bg-[#0f766e]"
                                                    style={{ height: `${height}%` }}
                                                />
                                            </div>
                                            <p className="line-clamp-2 min-h-[34px] text-center text-[12px] font-medium leading-[1.28] text-[#52525b]">{point.label}</p>
                                            <p className="text-[13px] font-semibold text-[#18181b]">{point.displayValue || point.value}</p>
                                        </div>
                                    );
                                })}
                            </div>
                        </div>
                    </div>
                ) : showMetrics ? (
                    <div className="grid h-full flex-1 auto-rows-fr grid-cols-3 content-center gap-4">
                        {metrics.map((metric, index) => (
                            <div key={index} className="rounded-2xl border border-[#d4d4d8] bg-white p-5 shadow-[0_12px_30px_rgba(24,24,27,0.05)]">
                                <p className="mb-3 text-[11px] font-semibold uppercase tracking-[0.2em] text-[#0f766e]">{metric.label}</p>
                                <p className="text-[34px] font-semibold leading-none text-[#18181b]">
                                    {metric.value}
                                    {metric.suffix ? <span className="ml-1 text-[18px] text-[#be123c]">{metric.suffix}</span> : null}
                                </p>
                            </div>
                        ))}
                    </div>
                ) : showBulletGrid ? (
                    <div className="grid h-full flex-1 auto-rows-fr grid-cols-2 content-start gap-4">
                        {bulletItems.map((bullet, index) => (
                            <div key={index} className="rounded-2xl border border-[#d4d4d8] bg-white px-5 py-4 shadow-[0_12px_30px_rgba(24,24,27,0.05)]">
                                <div className="flex items-start gap-3">
                                    <div className="mt-2 h-2 w-2 shrink-0 rounded-full bg-[#be123c]" />
                                    <p className={`${bodyClass} text-[#3f3f46]`}>{bullet}</p>
                                </div>
                            </div>
                        ))}
                    </div>
                ) : (
                    <div className={`min-h-0 flex-1 ${showTwoColumns ? "grid grid-cols-2 gap-8" : "grid grid-cols-[0.78fr_1.22fr] gap-8"}`}>
                        <div className="space-y-3">
                            {(showTwoColumns ? leftColumn : paragraphs.slice(0, Math.ceil(paragraphs.length / 2))).map((paragraph, index) => (
                                <p key={index} className={`${bodyClass} whitespace-pre-line text-[#3f3f46]`}>
                                    {paragraph}
                                </p>
                            ))}
                        </div>
                        <div className="space-y-3 rounded-lg border border-[#d4d4d8] bg-white p-5">
                            {(showTwoColumns ? rightColumn : paragraphs.slice(Math.ceil(paragraphs.length / 2))).map((paragraph, index) => (
                                <p key={index} className={`${bodyClass} whitespace-pre-line text-[#3f3f46]`}>
                                    {paragraph}
                                </p>
                            ))}
                        </div>
                    </div>
                )}
                </div>
            </div>
        </div>
    );
};

const VERBATIM_FIT_MIN_SCALE = 0.68;
const VERBATIM_FIT_MAX_SCALE = 1.18;
const VERBATIM_FIT_STEP = 0.045;
type VerbatimFitStatus = "measuring" | "fit" | "overflow";

export const V1ContentRender = ({ slide, isEditMode, theme }: { slide: any, isEditMode: boolean, theme?: any, enableEditMode?: boolean }) => {
    const dispatch = useDispatch();
    const containerRef = useRef<HTMLDivElement | null>(null);
    const isVerbatimImport = Boolean(slide?.content?.__verbatim_import__);
    const verbatimDensity = slide?.content?.__verbatim_density__ || "normal";
    const verbatimFullTextKey = stringifyLayoutValue(slide?.content?.__verbatim_full_text__ || slide?.content?.__speaker_note__);


    const customTemplateId = slide.layout_group.startsWith("custom-") ? slide.layout_group.split("custom-")[1] : slide.layout_group;
    const isCustomTemplate = uuidValidate(customTemplateId) || slide.layout_group.startsWith("custom-");

    // Always call the hook (React hooks rule), but with empty id when not a custom template
    const { template: customTemplate, loading: customLoading, fonts } = useCustomTemplateDetails({
        id: isCustomTemplate ? customTemplateId : "",
        name: isCustomTemplate ? slide.layout_group : "",
        description: ""
    });
    if (fonts && typeof fonts === 'object') {
        // useFontLoader(fonts as unknown as Record<string, string>);
    }

    // Memoize layout resolution to prevent unnecessary recalculations
    const resolvedLayout = useMemo(() => {
        if (isCustomTemplate) {
            if (customTemplate) {
                const layoutId = slide.layout.startsWith("custom-") ? slide.layout.split(":")[1] : slide.layout;


                const compiledLayout = customTemplate.layouts.find(
                    (layout) => layout.layoutId === layoutId
                );


                return {
                    component: compiledLayout?.component ?? null,
                    schema: compiledLayout?.schemaJSON ?? null,
                };
            }
            return { component: null, schema: null };
        } else {
            const template = getLayoutByLayoutId(slide.layout);
            return {
                component: template?.component ?? null,
                schema: (template as any)?.schemaJSON ?? (template as any)?.json_schema ?? (template as any)?.schema ?? null,
            };
        }
    }, [isCustomTemplate, customTemplate, slide.layout]);
    const Layout = resolvedLayout.component;
    const LayoutComp = Layout as React.ComponentType<{ data: any }>;
    const layoutData = {
        ...sanitizeLayoutData(slide.content, resolvedLayout.schema),
        _logo_url__: theme ? theme.logo_url : null,
        __companyName__: (theme && theme.company_name) ? theme.company_name : null,
    };
    const fitMeasureRef = useRef<HTMLDivElement | null>(null);
    const [templateOverflow, setTemplateOverflow] = useState(false);
    const [templateUnderfill, setTemplateUnderfill] = useState(false);
    const [fitScale, setFitScale] = useState(1);
    const [fitStatus, setFitStatus] = useState<VerbatimFitStatus>("measuring");
    const wantsTemplateFirst = Boolean(
        isVerbatimImport &&
        Layout &&
        layoutData.__verbatim_render_mode__ === "template-first"
    );
    const shouldRenderTemplateFirst = wantsTemplateFirst && !templateOverflow && !templateUnderfill;
    const baseFitScale = verbatimDensity === "dense" ? 0.9 : verbatimDensity === "medium" ? 0.96 : 1;

    useLayoutEffect(() => {
        setTemplateOverflow(false);
        setTemplateUnderfill(false);
        setFitScale(baseFitScale);
        setFitStatus("measuring");
    }, [slide?.id, slide?.layout, verbatimDensity, verbatimFullTextKey, baseFitScale]);

    useLayoutEffect(() => {
        if (!isVerbatimImport) return;

        const frame = window.requestAnimationFrame(() => {
            const node = fitMeasureRef.current;
            if (!node) return;

            const overflowY = node.scrollHeight > node.clientHeight + 2;
            const overflowX = node.scrollWidth > node.clientWidth + 2;
            const overflow = overflowY || overflowX;
            const textElements = Array.from(
                node.querySelectorAll<HTMLElement>("h1,h2,h3,p,li,td,th,span")
            ).filter((element) => element.textContent?.trim());
            const rootRect = node.getBoundingClientRect();
            const bounds = textElements.reduce(
                (acc, element) => {
                    const rect = element.getBoundingClientRect();
                    if (rect.width <= 0 || rect.height <= 0) return acc;
                    return {
                        left: Math.min(acc.left, rect.left),
                        top: Math.min(acc.top, rect.top),
                        right: Math.max(acc.right, rect.right),
                        bottom: Math.max(acc.bottom, rect.bottom),
                    };
                },
                { left: Infinity, top: Infinity, right: -Infinity, bottom: -Infinity }
            );
            const boundsWidth = Number.isFinite(bounds.left) ? bounds.right - bounds.left : rootRect.width;
            const boundsHeight = Number.isFinite(bounds.top) ? bounds.bottom - bounds.top : rootRect.height;
            const underfilled = !overflow
                && fitScale < VERBATIM_FIT_MAX_SCALE
                && boundsHeight < rootRect.height * 0.58
                && boundsWidth < rootRect.width * 0.82;
            const severelyUnderfilled = !overflow
                && shouldRenderTemplateFirst
                && fitScale >= VERBATIM_FIT_MAX_SCALE - 0.01
                && boundsHeight < rootRect.height * 0.6
                && boundsWidth < rootRect.width * 0.9;

            if (overflow && fitScale > VERBATIM_FIT_MIN_SCALE + 0.01) {
                setFitScale((current) => Math.max(VERBATIM_FIT_MIN_SCALE, Number((current - VERBATIM_FIT_STEP).toFixed(3))));
                setFitStatus("measuring");
                return;
            }

            if (overflow && shouldRenderTemplateFirst) {
                setTemplateOverflow(true);
                setFitScale(baseFitScale);
                setFitStatus("measuring");
                return;
            }

            if (underfilled) {
                setFitScale((current) => Math.min(VERBATIM_FIT_MAX_SCALE, Number((current + VERBATIM_FIT_STEP).toFixed(3))));
                setFitStatus("measuring");
                return;
            }

            if (severelyUnderfilled) {
                setTemplateUnderfill(true);
                setFitScale(baseFitScale);
                setFitStatus("measuring");
                return;
            }

            setFitStatus(overflow ? "overflow" : "fit");
        });

        return () => window.cancelAnimationFrame(frame);
    }, [isVerbatimImport, shouldRenderTemplateFirst, slide?.id, slide?.layout, verbatimDensity, verbatimFullTextKey, fitScale, baseFitScale]);

    // Show loading state for custom templates
    if (isCustomTemplate && customLoading) {
        return (
            <div className="flex flex-col items-center justify-center aspect-video h-full bg-gray-100 rounded-lg">
                <Loader2 className="w-4 h-4 animate-spin" />
            </div>
        );
    }


    if (!Layout && !isVerbatimImport) {
        if (Object.keys(slide?.content ?? {}).length === 0) {
            return (
                <div className="flex flex-col items-center cursor-pointer justify-center aspect-video h-full bg-gray-100 rounded-lg">
                    <p className="text-gray-600 text-center text-base">Blank Slide</p>
                    <p className="text-gray-600 text-center text-sm">This slide is empty. Please add content to it using the edit button.</p>
                </div>
            )
        }
        return (
            <div className="flex flex-col items-center justify-center aspect-video h-full bg-gray-100 rounded-lg">
                <p className="text-gray-600 text-center text-base">
                    Layout &quot;{slide.layout}&quot; not found in &quot;
                    {slide.layout_group}&quot; Template
                </p>
            </div>
        );
    }
    const verbatimFitStyles = isVerbatimImport ? (
        <style>{`
            .presenton-verbatim-fit { --verbatim-fit-scale: 1; --verbatim-space-scale: 1; aspect-ratio: 16 / 9; }
            .presenton-verbatim-fit[data-density="normal"] h1 { font-size: calc(48px * var(--verbatim-fit-scale)) !important; line-height: 1.04 !important; }
            .presenton-verbatim-fit[data-density="medium"] h1 { font-size: calc(40px * var(--verbatim-fit-scale)) !important; line-height: 1.04 !important; }
            .presenton-verbatim-fit[data-density="dense"] h1 { font-size: calc(32px * var(--verbatim-fit-scale)) !important; line-height: 1.04 !important; }
            .presenton-verbatim-fit[data-density="normal"] h2,
            .presenton-verbatim-fit[data-density="normal"] h3 { font-size: calc(28px * var(--verbatim-fit-scale)) !important; line-height: 1.12 !important; font-weight: 600 !important; }
            .presenton-verbatim-fit[data-density="medium"] h2,
            .presenton-verbatim-fit[data-density="medium"] h3 { font-size: calc(22px * var(--verbatim-fit-scale)) !important; line-height: 1.16 !important; font-weight: 600 !important; }
            .presenton-verbatim-fit[data-density="dense"] h2,
            .presenton-verbatim-fit[data-density="dense"] h3 { font-size: calc(18px * var(--verbatim-fit-scale)) !important; line-height: 1.14 !important; font-weight: 600 !important; }
            .presenton-verbatim-fit[data-density="normal"] p,
            .presenton-verbatim-fit[data-density="normal"] li,
            .presenton-verbatim-fit[data-density="normal"] td,
            .presenton-verbatim-fit[data-density="normal"] th { font-size: calc(18px * var(--verbatim-fit-scale)) !important; line-height: 1.42 !important; }
            .presenton-verbatim-fit[data-density="medium"] p,
            .presenton-verbatim-fit[data-density="medium"] li,
            .presenton-verbatim-fit[data-density="medium"] td,
            .presenton-verbatim-fit[data-density="medium"] th { font-size: calc(16px * var(--verbatim-fit-scale)) !important; line-height: 1.36 !important; }
            .presenton-verbatim-fit[data-density="dense"] p,
            .presenton-verbatim-fit[data-density="dense"] li,
            .presenton-verbatim-fit[data-density="dense"] td,
            .presenton-verbatim-fit[data-density="dense"] th { font-size: calc(14px * var(--verbatim-fit-scale)) !important; line-height: 1.3 !important; }
            .presenton-verbatim-fit[data-density="medium"] [class*="text-[64px]"] { font-size: calc(42px * var(--verbatim-fit-scale)) !important; line-height: 1.08 !important; }
            .presenton-verbatim-fit[data-density="dense"] [class*="text-[64px]"] { font-size: calc(34px * var(--verbatim-fit-scale)) !important; line-height: 1.08 !important; }
            .presenton-verbatim-fit[data-density="medium"] [class*="text-[48px]"] { font-size: calc(34px * var(--verbatim-fit-scale)) !important; }
            .presenton-verbatim-fit[data-density="dense"] [class*="text-[48px]"] { font-size: calc(28px * var(--verbatim-fit-scale)) !important; }
            .presenton-verbatim-fit[data-density="normal"] [class*="text-[42px]"] { font-size: calc(42px * var(--verbatim-fit-scale)) !important; line-height: 1.06 !important; }
            .presenton-verbatim-fit[data-density="normal"] [class*="text-[34px]"] { font-size: calc(34px * var(--verbatim-fit-scale)) !important; line-height: 1.1 !important; }
            .presenton-verbatim-fit[data-density="medium"] [class*="text-[34px]"] { font-size: calc(28px * var(--verbatim-fit-scale)) !important; line-height: 1.12 !important; }
            .presenton-verbatim-fit[data-density="dense"] [class*="text-[34px]"] { font-size: calc(22px * var(--verbatim-fit-scale)) !important; line-height: 1.12 !important; }
            .presenton-verbatim-fit[data-density="normal"] [class*="text-[28px]"] { font-size: calc(28px * var(--verbatim-fit-scale)) !important; line-height: 1.12 !important; }
            .presenton-verbatim-fit[data-density="medium"] [class*="text-[28px]"] { font-size: calc(20px * var(--verbatim-fit-scale)) !important; line-height: 1.2 !important; }
            .presenton-verbatim-fit[data-density="dense"] [class*="text-[28px]"] { font-size: calc(16px * var(--verbatim-fit-scale)) !important; line-height: 1.16 !important; }
            .presenton-verbatim-fit[data-density="normal"] [class*="text-[24px]"] { font-size: calc(24px * var(--verbatim-fit-scale)) !important; line-height: 1.16 !important; }
            .presenton-verbatim-fit[data-density="medium"] [class*="text-[24px]"] { font-size: calc(20px * var(--verbatim-fit-scale)) !important; line-height: 1.18 !important; }
            .presenton-verbatim-fit[data-density="dense"] [class*="text-[24px]"] { font-size: calc(17px * var(--verbatim-fit-scale)) !important; line-height: 1.15 !important; }
            .presenton-verbatim-fit[data-density="normal"] [class*="text-[16px]"] { font-size: calc(18px * var(--verbatim-fit-scale)) !important; line-height: 1.42 !important; }
            .presenton-verbatim-fit[data-density="medium"] [class*="text-[16px]"] { font-size: calc(16px * var(--verbatim-fit-scale)) !important; line-height: 1.36 !important; }
            .presenton-verbatim-fit[data-density="dense"] [class*="text-[16px]"] { font-size: calc(14px * var(--verbatim-fit-scale)) !important; line-height: 1.3 !important; }
            .presenton-verbatim-fit [class*="text-[12px]"] { font-size: calc(12px * var(--verbatim-fit-scale)) !important; line-height: 1.28 !important; }
            .presenton-verbatim-fit [class*="text-[11px]"] { font-size: calc(11px * var(--verbatim-fit-scale)) !important; line-height: 1.24 !important; letter-spacing: 0.12em !important; text-transform: uppercase !important; }
            .presenton-verbatim-fit [class*="tracking-[-"] { letter-spacing: 0 !important; }
            .presenton-verbatim-fit,
            .presenton-verbatim-fit * { min-width: 0 !important; overflow-wrap: anywhere !important; word-break: normal !important; }
            .presenton-verbatim-fit p,
            .presenton-verbatim-fit li,
            .presenton-verbatim-fit span,
            .presenton-verbatim-fit h1,
            .presenton-verbatim-fit h2,
            .presenton-verbatim-fit h3 { text-wrap: pretty; }
            .presenton-verbatim-fit[data-fit-status="overflow"] { outline: 0; }
        `}</style>
    ) : null;

    const renderLayout = () => (
        <div
            className={isVerbatimImport ? "presenton-verbatim-fit w-full h-full" : "w-full h-full"}
            data-density={verbatimDensity}
            data-fit-status={fitStatus}
            style={isVerbatimImport ? ({
                "--verbatim-fit-scale": fitScale,
                "--verbatim-space-scale": Math.max(0.78, Math.min(1.12, fitScale)),
            } as React.CSSProperties) : undefined}
        >
            {verbatimFitStyles}
            <div ref={isVerbatimImport ? fitMeasureRef : null} className={isVerbatimImport ? "presenton-verbatim-measure h-full w-full overflow-hidden" : "h-full w-full"}>
                {isVerbatimImport && shouldRenderTemplateFirst ? (
                    <LayoutComp data={layoutData} />
                ) : isVerbatimImport ? (
                    <VerbatimImportFallback data={layoutData} density={verbatimDensity} />
                ) : (
                    <LayoutComp data={layoutData} />
                )}
            </div>
        </div>
    );

    if (isEditMode) {
        return (
            <SlideErrorBoundary label={`Slide ${slide.index + 1}`}>
                <div ref={containerRef} className={`w-full h-full `}>

                    <EditableLayoutWrapper
                        slideIndex={slide.index}
                        slideData={slide.content}
                        properties={slide.properties}
                    >
                        <TiptapTextReplacer
                            key={slide.id}
                            slideData={slide.content}
                            slideIndex={slide.index}
                            onContentChange={(
                                content: string,
                                dataPath: string,
                                slideIndex?: number
                            ) => {
                                if (dataPath && slideIndex !== undefined) {
                                    dispatch(
                                        updateSlideContent({
                                            slideIndex: slideIndex,
                                            dataPath: dataPath,
                                            content: content,
                                        })
                                    );
                                }
                            }}
                        >
                            {renderLayout()}
                        </TiptapTextReplacer>
                    </EditableLayoutWrapper>



                </div>
            </SlideErrorBoundary>

        );
    }
    return (
        renderLayout()
    )
};
