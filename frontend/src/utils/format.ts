export function formatDate(value: string | null | undefined): string {
  if (!value) return "-";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

export function formatRelative(value: string | null | undefined): string {
  if (!value) return "-";
  const seconds = Math.round((new Date(value).getTime() - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  const ranges: Array<[Intl.RelativeTimeFormatUnit, number]> = [
    ["day", 86400],
    ["hour", 3600],
    ["minute", 60],
  ];
  for (const [unit, size] of ranges) {
    if (Math.abs(seconds) >= size) return formatter.format(Math.round(seconds / size), unit);
  }
  return formatter.format(seconds, "second");
}

export function titleCase(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function shortId(value: string): string {
  return value.slice(0, 8);
}

export function formatTokens(value: unknown): string {
  const tokens = typeof value === "number" ? value : Number(value ?? 0);
  return Number.isFinite(tokens) ? `${Math.round(tokens).toLocaleString()} tokens` : "0 tokens";
}

export function formatUsd(value: unknown): string {
  const cost = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(cost)) return "Cost unavailable";
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: cost < 0.01 ? 4 : 2,
    maximumFractionDigits: cost < 0.01 ? 6 : 2,
  }).format(cost);
}
