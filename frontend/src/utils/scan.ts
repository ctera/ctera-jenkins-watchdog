import type { Scan } from "../services/api";
import { titleCase } from "./format";

export type ScanAnalysis = NonNullable<Scan["analysis"]>;

export interface ScanWorkflowStatus {
  value: "scanning" | "analyzing" | "waiting_budget" | "complete" | "complete_with_issues";
  label: "Scanning" | "Analyzing" | "Waiting budget" | "Complete" | "Complete with issues";
}

export function isCollectionActive(scan: Scan): boolean {
  return scan.status === "queued" || scan.status === "running";
}

export function isAnalysisActive(scan: Scan): boolean {
  if (["investigating", "waiting_budget"].includes(scan.jenkins_failures?.status ?? "")) return true;
  return Boolean(scan.analysis?.active_count);
}

export function scanWorkflowStatus(scan: Scan): ScanWorkflowStatus {
  const reportStatus = scan.jenkins_failures?.status;
  if (reportStatus === "collecting") return { value: "scanning", label: "Scanning" };
  if (reportStatus === "investigating") return { value: "analyzing", label: "Analyzing" };
  if (reportStatus === "waiting_budget") return { value: "waiting_budget", label: "Waiting budget" };
  if (reportStatus === "failed" || reportStatus === "cancelled") {
    return { value: "complete_with_issues", label: "Complete with issues" };
  }
  if (reportStatus === "complete") return { value: "complete", label: "Complete" };
  if (isCollectionActive(scan) || scan.analysis?.status === "selecting") {
    return { value: "scanning", label: "Scanning" };
  }
  if (isAnalysisActive(scan) || scan.analysis?.status === "queued" || scan.analysis?.status === "running") {
    return { value: "analyzing", label: "Analyzing" };
  }
  if (
    scan.status === "failed"
    || scan.status === "cancelled"
    || scan.analysis?.status === "complete_with_issues"
    || scan.analysis?.status === "budget_deferred"
  ) {
    return { value: "complete_with_issues", label: "Complete with issues" };
  }
  return { value: "complete", label: "Complete" };
}

export function scanStageLabel(stage: string): string {
  if (stage === "investigating") return "Selecting investigations";
  return titleCase(stage);
}

export function analysisProgress(analysis: ScanAnalysis | undefined): number {
  if (!analysis?.selected_count) return analysis?.active_count ? 4 : 100;
  const finished = analysis.succeeded_count + analysis.partial_count + analysis.failed_count;
  return Math.min(100, Math.max(analysis.active_count ? 4 : 0, (finished / analysis.selected_count) * 100));
}
