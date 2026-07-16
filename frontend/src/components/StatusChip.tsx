import { Chip } from "@mui/material";
import { titleCase } from "../utils/format";

const colors: Record<string, { color: string; background: string }> = {
  critical: { color: "#912018", background: "#fee4e2" },
  warning: { color: "#93370d", background: "#fef0c7" },
  low: { color: "#344054", background: "#eaecf0" },
  open: { color: "#912018", background: "#fee4e2" },
  resolved: { color: "#05603a", background: "#d1fadf" },
  suppressed: { color: "#344054", background: "#eaecf0" },
  succeeded: { color: "#05603a", background: "#d1fadf" },
  failed: { color: "#912018", background: "#fee4e2" },
  permanently_failed: { color: "#912018", background: "#fee4e2" },
  cancelled: { color: "#344054", background: "#eaecf0" },
  running: { color: "#1849a9", background: "#d1e9ff" },
  scanning: { color: "#1849a9", background: "#d1e9ff" },
  analyzing: { color: "#6941c6", background: "#ebe9fe" },
  selecting: { color: "#6941c6", background: "#ebe9fe" },
  queued: { color: "#344054", background: "#eaecf0" },
  pending: { color: "#344054", background: "#eaecf0" },
  retry_scheduled: { color: "#93370d", background: "#fef0c7" },
  failure: { color: "#912018", background: "#fee4e2" },
  unstable: { color: "#93370d", background: "#fef0c7" },
  aborted: { color: "#344054", background: "#eaecf0" },
  success: { color: "#05603a", background: "#d1fadf" },
  complete: { color: "#05603a", background: "#d1fadf" },
  complete_with_issues: { color: "#93370d", background: "#fef0c7" },
  selected: { color: "#1849a9", background: "#d1e9ff" },
  reused: { color: "#05603a", background: "#d1fadf" },
  deferred: { color: "#475467", background: "#eaecf0" },
  manual_only: { color: "#475467", background: "#eaecf0" },
  budget_deferred: { color: "#93370d", background: "#fef0c7" },
  new_failure: { color: "#912018", background: "#fee4e2" },
  new_regression: { color: "#9c2a10", background: "#ffead5" },
  recurring: { color: "#1849a9", background: "#d1e9ff" },
  flaky: { color: "#854a0e", background: "#fef0c7" },
  propagated: { color: "#475467", background: "#eaecf0" },
  enriched: { color: "#05603a", background: "#d1fadf" },
  log_pending: { color: "#344054", background: "#eaecf0" },
  partial: { color: "#93370d", background: "#fef0c7" },
  change_request: { color: "#175cd3", background: "#d1e9ff" },
  branch: { color: "#05603a", background: "#d1fadf" },
  tag: { color: "#475467", background: "#eaecf0" },
};

const labels: Record<string, string> = {
  complete_with_issues: "Complete with issues",
  manual_only: "Manual only",
  budget_deferred: "Budget deferred",
};

export default function StatusChip({ value, size = "small" }: { value: string; size?: "small" | "medium" }) {
  const style = colors[value] ?? colors.low;
  return (
    <Chip
      size={size}
      label={labels[value] ?? titleCase(value)}
      sx={{ color: style.color, bgcolor: style.background, border: "1px solid", borderColor: `${style.color}22` }}
    />
  );
}
