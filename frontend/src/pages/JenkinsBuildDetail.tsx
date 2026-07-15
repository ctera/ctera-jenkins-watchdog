import {
  Alert,
  Box,
  Button,
  Chip,
  Divider,
  Link,
  Paper,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
} from "@mui/material";
import ArrowBackIcon from "@mui/icons-material/ArrowBack";
import OpenInNewIcon from "@mui/icons-material/OpenInNew";
import PsychologyOutlinedIcon from "@mui/icons-material/PsychologyOutlined";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import MarkdownContent from "../components/MarkdownContent";
import { ErrorPanel, LoadingPanel } from "../components/StatePanel";
import StatusChip from "../components/StatusChip";
import { analyzeJenkinsBuild, getJenkinsBuild, type JenkinsBuildDetail } from "../services/api";
import { formatDate, titleCase } from "../utils/format";

type UnknownRecord = Record<string, unknown>;

export default function JenkinsBuildDetailPage() {
  const { buildId = "" } = useParams();
  const navigate = useNavigate();
  const [build, setBuild] = useState<JenkinsBuildDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [analysisMode, setAnalysisMode] = useState<"regular" | "deep">("regular");

  const load = useCallback(async () => {
    try {
      setBuild(await getJenkinsBuild(buildId));
      setError(null);
    } catch (requestError) {
      setError(requestError);
    } finally {
      setLoading(false);
    }
  }, [buildId]);

  useEffect(() => { void load(); }, [load]);

  const analysisActive = ["queued", "running"].includes(build?.investigation_request?.status ?? "");
  useEffect(() => {
    if (!analysisActive) return;
    const timer = window.setInterval(() => void load(), 2500);
    return () => window.clearInterval(timer);
  }, [analysisActive, load]);

  async function analyze() {
    if (!build || analysisActive || analyzing) return;
    setAnalyzing(true);
    try {
      const request = await analyzeJenkinsBuild(build.id, analysisMode);
      setBuild((current) => current ? { ...current, investigation_request: request } : current);
      setError(null);
    } catch (requestError) {
      setError(requestError);
    } finally {
      setAnalyzing(false);
    }
  }

  const evidence = useMemo(() => asRecord(build?.evidence), [build]);
  const errorLines = asRecordsOrStrings(evidence.error_lines).slice(-8);
  const stages = asRecords(evidence.stages);
  const causes = asRecords(evidence.causes);

  if (loading && !build) return <LoadingPanel label="Loading Jenkins build evidence" />;
  if (error && !build) return <ErrorPanel error={error} />;
  if (!build) return null;

  const lineage = [
    ...build.upstream_builds.map((item) => ({ ...item, relationship: "Upstream" })),
    { id: build.id, job_name: build.job_name, build_number: build.build_number, result: build.result, started_at: build.started_at, relationship: "Selected" },
    ...build.downstream_builds.map((item) => ({ ...item, relationship: "Downstream" })),
  ].map(asRecord);

  return (
    <Box>
      <Button startIcon={<ArrowBackIcon />} onClick={() => navigate("/overview")} sx={{ mb: 1 }}>Jenkins failures</Button>
      <PageHeader
        title={`${breakableJobName(build.job_name)} #${build.build_number}`}
        subtitle={`${formatDate(build.started_at)} · ${duration(build.duration_ms)}`}
        actions={<Button component={Link} href={build.url} target="_blank" rel="noreferrer" variant="outlined" endIcon={<OpenInNewIcon />}>Open in Jenkins</Button>}
      />

      <Alert severity={build.enrichment_status === "failed" ? "warning" : build.enrichment_status === "enriched" ? "success" : "info"} sx={{ mb: 2 }}>
        Deterministic evidence: {titleCase(build.enrichment_status)}
      </Alert>

      <Paper variant="outlined" sx={{ mb: 2.5, overflow: "hidden" }}>
        <Stack direction={{ xs: "column", md: "row" }} alignItems={{ xs: "flex-start", md: "center" }} gap={1.25} sx={{ p: 2 }}>
          <StatusChip value={build.result.toLowerCase()} size="medium" />
          <StatusChip value={build.novelty} size="medium" />
          <Chip variant="outlined" label={titleCase(build.failure_classification)} />
          {build.propagated_failure && <Chip variant="outlined" label="Propagated failure" />}
          <Box sx={{ ml: { md: "auto" }, textAlign: { md: "right" } }}>
            <Typography variant="caption" color="text.secondary">Priority</Typography>
            <Typography variant="h5" color={build.priority_score >= 60 ? "error.main" : build.priority_score >= 30 ? "warning.main" : "text.primary"}>{build.priority_score}</Typography>
          </Box>
        </Stack>
        <Divider />
        <Box sx={{ p: 2 }}>
          <Typography variant="h6" sx={{ overflowWrap: "anywhere" }}>{build.failure_summary || `${titleCase(build.failure_classification)} in ${build.job_name}`}</Typography>
          {(build.priority_reasons ?? []).length > 0 && <Stack direction="row" gap={0.75} flexWrap="wrap" sx={{ mt: 1.25 }}>{(build.priority_reasons ?? []).map((reason) => <Chip size="small" variant="outlined" key={reason} label={reason} />)}</Stack>}
        </Box>
      </Paper>

      <AgentAnalysis
        build={build}
        active={analysisActive}
        working={analyzing}
        mode={analysisMode}
        onMode={setAnalysisMode}
        onAnalyze={() => void analyze()}
        onOpenIncident={(id) => navigate(`/incidents/${id}`)}
      />

      <Section title="Build context">
        <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", sm: "repeat(2, minmax(0, 1fr))", lg: "repeat(4, minmax(0, 1fr))" } }}>
          <Fact label="Failed stage" value={build.failed_stage || "Unknown"} />
          <Fact label="Trigger" value={titleCase(build.trigger_kind)} />
          <Fact label="Root execution" value={`${build.root_job} #${build.root_build_number}`} />
          <Fact label="History coverage" value={titleCase(build.coverage)} />
          <Fact label="Head" value={build.head_name || "Unknown"} />
          <Fact label="Head type" value={titleCase(build.head_type)} />
          <Fact label="Provider" value={build.source_provider ? titleCase(build.source_provider) : "Unknown"} />
          <Fact label="Repository" value={build.repository || "Unknown"} />
        </Box>
        {build.change_number && (
          <Box sx={{ px: 2, py: 1.5, borderTop: "1px solid", borderColor: "divider" }}>
            <Typography variant="caption" color="text.secondary">Change request</Typography>
            <Typography variant="body2" fontWeight={650}>{build.change_url ? <Link href={build.change_url} target="_blank" rel="noreferrer">#{build.change_number}</Link> : `#${build.change_number}`}</Typography>
          </Box>
        )}
      </Section>

      <Section title="Logical execution">
        <TableContainer>
          <Table size="small" sx={{ minWidth: 720 }}>
            <TableHead><TableRow><TableCell>Relationship</TableCell><TableCell>Job</TableCell><TableCell>Build</TableCell><TableCell>Result</TableCell><TableCell>Started</TableCell></TableRow></TableHead>
            <TableBody>{lineage.map((item, index) => (
              <TableRow hover={Boolean(stringValue(item.id))} key={`${stringValue(item.relationship)}-${stringValue(item.id)}-${index}`} onClick={() => stringValue(item.id) && navigate(`/jenkins/builds/${stringValue(item.id)}`)} sx={{ cursor: stringValue(item.id) ? "pointer" : "default" }}>
                <TableCell>{stringValue(item.relationship)}</TableCell>
                <TableCell><Typography variant="body2" fontWeight={650}>{stringValue(item.job_name)}</Typography></TableCell>
                <TableCell>#{numberValue(item.build_number)}</TableCell>
                <TableCell><StatusChip value={(stringValue(item.result) || "unknown").toLowerCase()} /></TableCell>
                <TableCell>{formatDate(stringValue(item.started_at))}</TableCell>
              </TableRow>
            ))}</TableBody>
          </Table>
        </TableContainer>
      </Section>

      <Section title="Failure evidence">
        {errorLines.length > 0 ? (
          <Box component="pre" sx={{ m: 0, p: 2, bgcolor: "#181d27", color: "#f5f5f5", fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 12, lineHeight: 1.65, whiteSpace: "pre-wrap", overflowWrap: "anywhere", overflowX: "auto" }}>{errorLines.join("\n")}</Box>
        ) : <Typography color="text.secondary" sx={{ p: 2 }}>No matching console error lines were retained.</Typography>}
      </Section>

      <Section title="Pipeline stages">
        <TableContainer>
          <Table size="small">
            <TableHead><TableRow><TableCell>Stage</TableCell><TableCell>Status</TableCell><TableCell>Duration</TableCell></TableRow></TableHead>
            <TableBody>{stages.length === 0 ? <EmptyRow text="No stage data available" /> : stages.map((stage, index) => <TableRow key={`${stringValue(stage.name)}-${index}`}><TableCell>{stringValue(stage.name) || "Unnamed stage"}</TableCell><TableCell><StatusChip value={(stringValue(stage.status) || "unknown").toLowerCase()} /></TableCell><TableCell>{duration(numberValue(stage.duration_ms))}</TableCell></TableRow>)}</TableBody>
          </Table>
        </TableContainer>
      </Section>

      <Section title="Trigger causes">
        <TableContainer>
          <Table size="small" sx={{ minWidth: 700 }}>
            <TableHead><TableRow><TableCell>Cause</TableCell><TableCell>Upstream job</TableCell><TableCell>Build</TableCell></TableRow></TableHead>
            <TableBody>{causes.length === 0 ? <EmptyRow text="No cause data available" /> : causes.map((cause, index) => <TableRow key={`${stringValue(cause.kind)}-${index}`}><TableCell>{titleCase(stringValue(cause.kind).replaceAll("$", " ") || "unknown")}</TableCell><TableCell>{stringValue(cause.upstream_job) || "-"}</TableCell><TableCell>{numberValue(cause.upstream_build) ? `#${numberValue(cause.upstream_build)}` : "-"}</TableCell></TableRow>)}</TableBody>
          </Table>
        </TableContainer>
      </Section>
    </Box>
  );
}

function AgentAnalysis({
  build,
  active,
  working,
  mode,
  onMode,
  onAnalyze,
  onOpenIncident,
}: {
  build: JenkinsBuildDetail;
  active: boolean;
  working: boolean;
  mode: "regular" | "deep";
  onMode: (mode: "regular" | "deep") => void;
  onAnalyze: () => void;
  onOpenIncident: (id: string) => void;
}) {
  const request = build.investigation_request;
  const investigation = build.latest_investigation;
  const result = asRecord(investigation?.result);
  const trace = asRecords(result.tool_trace);
  const status = request?.status ?? (investigation?.status === "succeeded" ? "succeeded" : "not_started");

  return (
    <Section title="Agent analysis">
      <Box sx={{ p: 2 }}>
        <Stack direction={{ xs: "column", md: "row" }} gap={1.25} alignItems={{ xs: "stretch", md: "center" }}>
          {status === "not_started" ? <Chip label="Not analyzed" variant="outlined" /> : <StatusChip value={status} size="medium" />}
          {request && <Chip label={titleCase(request.mode)} size="small" variant="outlined" />}
          {investigation?.confidence && <Chip label={`${titleCase(investigation.confidence)} confidence`} size="small" variant="outlined" />}
          <Box sx={{ flex: 1 }} />
          <ToggleButtonGroup
            exclusive
            size="small"
            value={mode}
            onChange={(_, value: "regular" | "deep" | null) => value && onMode(value)}
            aria-label="Analysis depth"
            disabled={active || working}
          >
            <ToggleButton value="regular">Regular</ToggleButton>
            <ToggleButton value="deep">Deep</ToggleButton>
          </ToggleButtonGroup>
          <Button
            variant="contained"
            startIcon={<PsychologyOutlinedIcon />}
            onClick={onAnalyze}
            disabled={active || working}
          >
            {active ? "Analysis in progress" : investigation ? "Analyze again" : "Analyze build"}
          </Button>
          {build.incident_id && (
            <Button variant="outlined" onClick={() => onOpenIncident(build.incident_id!)}>Open incident</Button>
          )}
        </Stack>

        {request?.status === "queued" && <Alert severity="info" sx={{ mt: 2 }}>Agent analysis is queued.</Alert>}
        {request?.status === "running" && <Alert severity="info" sx={{ mt: 2 }}>Agent is gathering live evidence.</Alert>}
        {request?.status === "failed" && <Alert severity="error" sx={{ mt: 2 }}>{request.error_summary || "Agent analysis failed."}</Alert>}

        {investigation ? (
          <Stack gap={2.25} sx={{ mt: 2.5 }}>
            {investigation.error_summary && <Alert severity="error">{investigation.error_summary}</Alert>}
            {stringValue(result.quality_gate) && <Alert severity="warning">{stringValue(result.quality_gate)}</Alert>}
            <ReportField label="Root cause" value={result.root_cause} />
            <Divider />
            <ReportField label="Impact" value={result.impact} />
            <Divider />
            <ReportField label="Recommended action" value={result.suggested_fix} />
            {stringValue(result.fix_verification) && (
              <>
                <Divider />
                <ReportField label="Verification" value={result.fix_verification} />
              </>
            )}
            {Array.isArray(result.evidence) && result.evidence.length > 0 && (
              <>
                <Divider />
                <Box>
                  <Typography variant="subtitle2" sx={{ mb: 0.75 }}>Evidence used</Typography>
                  <Stack component="ul" gap={0.5} sx={{ m: 0, pl: 2.5 }}>
                    {result.evidence.map((item, index) => <Typography component="li" variant="body2" key={index}>{stringValue(item)}</Typography>)}
                  </Stack>
                </Box>
              </>
            )}
            {trace.length > 0 && (
              <>
                <Divider />
                <Box>
                  <Typography variant="subtitle2" sx={{ mb: 0.75 }}>Live evidence trace</Typography>
                  <TableContainer>
                    <Table size="small">
                      <TableHead><TableRow><TableCell>Round</TableCell><TableCell>Read operation</TableCell><TableCell>Status</TableCell><TableCell align="right">Duration</TableCell></TableRow></TableHead>
                      <TableBody>{trace.map((item, index) => <TableRow key={`${stringValue(item.tool)}-${index}`}><TableCell>{numberValue(item.round)}</TableCell><TableCell>{titleCase(stringValue(item.tool).replaceAll("_", " "))}</TableCell><TableCell><StatusChip value={item.ok ? "succeeded" : "failed"} /></TableCell><TableCell align="right">{numberValue(item.duration_ms)} ms</TableCell></TableRow>)}</TableBody>
                    </Table>
                  </TableContainer>
                </Box>
              </>
            )}
            <Typography variant="caption" color="text.secondary">
              {investigation.model} · {formatDate(investigation.completed_at)}
            </Typography>
          </Stack>
        ) : !active && request?.status !== "failed" ? (
          <Typography color="text.secondary" sx={{ mt: 2 }}>No agent assessment recorded.</Typography>
        ) : null}
      </Box>
    </Section>
  );
}

function ReportField({ label, value }: { label: string; value: unknown }) {
  return <Box><Typography variant="subtitle2" sx={{ mb: 0.75 }}>{label}</Typography><MarkdownContent content={stringValue(value) || "Not available"} /></Box>;
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return <Box sx={{ mb: 2.5 }}><Typography variant="h6" sx={{ mb: 1 }}>{title}</Typography><Paper variant="outlined" sx={{ overflow: "hidden" }}>{children}</Paper></Box>;
}

function Fact({ label, value }: { label: string; value: string }) {
  return <Box sx={{ p: 2, minWidth: 0, borderRight: "1px solid", borderBottom: "1px solid", borderColor: "divider" }}><Typography variant="caption" color="text.secondary">{label}</Typography><Typography variant="body2" fontWeight={650} sx={{ mt: 0.35, overflowWrap: "anywhere" }}>{value}</Typography></Box>;
}

function EmptyRow({ text }: { text: string }) {
  return <TableRow><TableCell colSpan={3}><Typography color="text.secondary" sx={{ py: 2, textAlign: "center" }}>{text}</Typography></TableCell></TableRow>;
}

function duration(milliseconds: number): string {
  const minutes = milliseconds / 60_000;
  if (minutes >= 1440) return `${(minutes / 1440).toFixed(1)}d`;
  if (minutes >= 60) return `${(minutes / 60).toFixed(1)}h`;
  if (minutes > 0 && minutes < 1) return `${Math.round(milliseconds / 1000)}s`;
  return `${Math.round(minutes)}m`;
}

function asRecord(value: unknown): UnknownRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as UnknownRecord : {};
}

function asRecords(value: unknown): UnknownRecord[] {
  return Array.isArray(value) ? value.map(asRecord) : [];
}

function asRecordsOrStrings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function numberValue(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function breakableJobName(value: string): string {
  return value.replaceAll("/", "/\u200b").replaceAll("_", "_\u200b");
}
