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
  queued: { color: "#344054", background: "#eaecf0" },
  pending: { color: "#344054", background: "#eaecf0" },
  retry_scheduled: { color: "#93370d", background: "#fef0c7" },
};

export default function StatusChip({ value, size = "small" }: { value: string; size?: "small" | "medium" }) {
  const style = colors[value] ?? colors.low;
  return (
    <Chip
      size={size}
      label={titleCase(value)}
      sx={{ color: style.color, bgcolor: style.background, border: "1px solid", borderColor: `${style.color}22` }}
    />
  );
}
