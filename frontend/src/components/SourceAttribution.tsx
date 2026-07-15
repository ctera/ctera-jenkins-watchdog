import { Box, Chip, Link, Stack, Tooltip, Typography } from "@mui/material";
import { titleCase } from "../utils/format";

type SourceObject = object;

interface NormalizedSource {
  raw: Record<string, unknown>;
  kind: string;
  status: string;
  provider: string;
  repository: string;
  changeNumber: string;
  branch: string;
  commitSha: string;
  url: string;
  title: string;
  state: string;
  reason: string;
  profileId: string;
  resolutionMethod: string;
  jobName: string;
  triggerKind: string;
  sourceCount: number;
}

export function SourceSummary({ source }: { source: SourceObject }) {
  const item = normalize(source);
  const primary = primaryLabel(item);
  const secondary = [secondaryLabel(item), statusLabel(item.status)].filter(Boolean).join(" · ");
  const content = (
    <Typography variant="body2" fontWeight={650} sx={{ overflowWrap: "break-word" }}>
      {primary}
    </Typography>
  );

  return (
    <Tooltip title={item.reason || sourceTooltip(item)} placement="top-start">
      <Box sx={{ minWidth: 0 }}>
        {item.url ? <Link href={item.url} target="_blank" rel="noreferrer" underline="hover" onClick={(event) => event.stopPropagation()}>{content}</Link> : content}
        {secondary && <Typography variant="caption" color={statusColor(item.status)} sx={{ display: "block", overflowWrap: "anywhere" }}>{secondary}</Typography>}
      </Box>
    </Tooltip>
  );
}

export function SourceDetails({ source }: { source: SourceObject }) {
  const item = normalize(source);
  const nested = Array.isArray(item.raw.sources)
    ? item.raw.sources.filter((value): value is Record<string, unknown> => Boolean(value) && typeof value === "object" && !Array.isArray(value))
    : [];
  const facts = sourceFacts(item);

  return (
    <Stack gap={1.5}>
      <Stack direction={{ xs: "column", sm: "row" }} gap={1} alignItems={{ xs: "flex-start", sm: "center" }}>
        <Box sx={{ flex: 1, minWidth: 0 }}><SourceSummary source={source} /></Box>
        <Stack direction="row" gap={0.75} flexWrap="wrap">
          <Chip size="small" variant="outlined" color={statusChipColor(item.status)} label={statusLabel(item.status)} />
          {item.profileId && <Chip size="small" variant="outlined" label={item.profileId} />}
          {item.kind === "change_request" && item.status === "verified" && (
            <Chip size="small" variant="outlined" label={booleanValue(item.raw.source_allow_mr_comments, item.raw.allow_mr_comments) ? "MR comments enabled" : "Read only"} />
          )}
        </Stack>
      </Stack>
      {item.title && <Typography variant="body2">{item.title}</Typography>}
      {nested.length > 0 && (
        <Stack gap={1} divider={<Box sx={{ borderTop: "1px solid", borderColor: "divider" }} />}>
          {nested.map((child, index) => <SourceSummary key={`${textValue(child.kind)}-${textValue(child.repository)}-${index}`} source={child} />)}
        </Stack>
      )}
      {facts.length > 0 && (
        <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", sm: "repeat(2, minmax(0, 1fr))", lg: "repeat(3, minmax(0, 1fr))" }, gap: 1.5 }}>
          {facts.map(([label, value]) => (
            <Box key={label} sx={{ minWidth: 0 }}>
              <Typography variant="caption" color="text.secondary">{label}</Typography>
              <Typography variant="body2" sx={{ overflowWrap: "anywhere" }}>{value}</Typography>
            </Box>
          ))}
        </Box>
      )}
      {item.reason && <Typography variant="caption" color={statusColor(item.status)} sx={{ overflowWrap: "anywhere" }}>{item.reason}</Typography>}
    </Stack>
  );
}

function normalize(source: SourceObject): NormalizedSource {
  const raw = source as Record<string, unknown>;
  const rawKind = textValue(raw.source_kind, raw.kind) || "unresolved";
  const kind = rawKind === "merge_request" ? "change_request" : rawKind === "repository" ? "repository_revision" : rawKind;
  const confirmed = booleanValue(raw.confirmed);
  const verified = booleanValue(raw.verified) || Boolean(textValue(raw.source_verified_at));
  const status = textValue(raw.source_status, raw.status)
    || (verified ? "verified" : confirmed ? "resolved" : reasonStatus(textValue(raw.source_reason, raw.reason)));
  return {
    raw,
    kind,
    status,
    provider: textValue(raw.source_provider, raw.provider),
    repository: textValue(raw.repository),
    changeNumber: textValue(raw.change_number),
    branch: textValue(raw.source_branch, raw.branch),
    commitSha: textValue(raw.source_commit_sha, raw.commit_sha),
    url: externalUrl(textValue(raw.source_url, raw.change_url, raw.url)),
    title: textValue(raw.source_title, raw.title),
    state: textValue(raw.source_state, raw.state),
    reason: textValue(raw.source_reason, raw.reason),
    profileId: textValue(raw.source_profile_id, raw.profile_id),
    resolutionMethod: textValue(raw.source_resolution_method, raw.resolution_method),
    jobName: textValue(raw.root_job, raw.job_name),
    triggerKind: textValue(raw.trigger_kind),
    sourceCount: numberValue(raw.source_count),
  };
}

function primaryLabel(item: NormalizedSource): string {
  if (item.kind === "change_request") {
    const marker = item.provider.toLowerCase() === "gitlab" ? "!" : "#";
    return item.changeNumber
      ? `${providerLabel(item.provider || "change request")} ${marker}${item.changeNumber}`
      : "Change request";
  }
  if (item.kind === "repository_revision") return item.branch || shortSha(item.commitSha) || "Repository revision";
  if (item.kind === "pipeline") return item.jobName || item.title || "Pipeline execution";
  if (item.kind === "multiple") return `Multiple sources${item.sourceCount ? ` (${item.sourceCount})` : ""}`;
  if (item.kind === "infrastructure") return "Infrastructure";
  if (item.status === "pending") return "Resolving source";
  if (item.status === "conflict") return "Source conflict";
  if (item.status === "unavailable") return "Source unavailable";
  return "Unresolved source";
}

function secondaryLabel(item: NormalizedSource): string {
  if (item.repository) return item.repository;
  if (item.kind === "repository_revision" && item.commitSha) return shortSha(item.commitSha);
  if (item.kind === "pipeline") return item.triggerKind ? titleCase(item.triggerKind) : "Jenkins";
  if (item.provider) return providerLabel(item.provider);
  return "";
}

function sourceFacts(item: NormalizedSource): Array<[string, string]> {
  const values: Array<[string, string]> = [];
  if (item.provider) values.push(["Provider", providerLabel(item.provider)]);
  if (item.repository) values.push(["Repository", item.repository]);
  if (item.changeNumber) values.push(["Change request", `${item.provider.toLowerCase() === "gitlab" ? "!" : "#"}${item.changeNumber}`]);
  if (item.branch) values.push(["Branch", item.branch]);
  if (item.commitSha) values.push(["Revision", item.commitSha]);
  if (item.state) values.push(["State", titleCase(item.state)]);
  if (item.jobName && item.kind === "pipeline") values.push(["Root job", item.jobName]);
  if (item.triggerKind) values.push(["Trigger", titleCase(item.triggerKind)]);
  if (item.resolutionMethod && item.resolutionMethod !== "none") values.push(["Resolution", titleCase(item.resolutionMethod)]);
  return values;
}

function sourceTooltip(item: NormalizedSource): string {
  const profile = item.profileId ? `Profile ${item.profileId}` : "No matching profile";
  return `${statusLabel(item.status)} · ${profile}`;
}

function statusLabel(status: string): string {
  return ({
    pending: "Resolving",
    resolved: "Resolved",
    verified: "Verified",
    conflict: "Conflict",
    unavailable: "Unavailable",
    unresolved: "Unresolved",
  } as Record<string, string>)[status] || titleCase(status || "unresolved");
}

function providerLabel(provider: string): string {
  if (provider.toLowerCase() === "gitlab") return "GitLab";
  if (provider.toLowerCase() === "github") return "GitHub";
  return titleCase(provider);
}

function statusColor(status: string): "success.main" | "warning.main" | "error.main" | "text.secondary" {
  if (status === "verified") return "success.main";
  if (status === "conflict") return "error.main";
  if (status === "unavailable") return "warning.main";
  return "text.secondary";
}

function statusChipColor(status: string): "success" | "warning" | "error" | "default" {
  if (status === "verified") return "success";
  if (status === "conflict") return "error";
  if (status === "unavailable") return "warning";
  return "default";
}

function reasonStatus(reason: string): string {
  if (reason.includes("conflict") || reason.includes("mismatch")) return "conflict";
  if (reason.includes("unavailable")) return "unavailable";
  return "unresolved";
}

function textValue(...values: unknown[]): string {
  for (const value of values) if (typeof value === "string" && value.trim()) return value.trim();
  return "";
}

function booleanValue(...values: unknown[]): boolean {
  return values.some((value) => value === true);
}

function numberValue(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function shortSha(value: string): string {
  return value ? value.slice(0, 10) : "";
}

function externalUrl(value: string): string {
  return /^https?:\/\//i.test(value) ? value : "";
}
