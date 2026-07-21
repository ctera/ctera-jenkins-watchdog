import { Alert, Box, Button, Chip, Drawer, MenuItem, Stack, Table, TableBody, TableCell, TableHead, TableRow, TextField, Typography } from "@mui/material";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import { createJenkinsFailureReport, getJenkinsFailureReport, listJenkinsFailureReports, type JenkinsFailureReport, type JenkinsFailureReportBuild } from "../services/api";

export default function JenkinsFailureReports() {
  const [report, setReport] = useState<JenkinsFailureReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [job, setJob] = useState("");
  const [selected, setSelected] = useState<JenkinsFailureReportBuild | null>(null);
  const [recentReports, setRecentReports] = useState<JenkinsFailureReport[]>([]);
  const load = async (id: string, nextJob = job) => setReport(await getJenkinsFailureReport(id, 0, 50, { job: nextJob || undefined }));
  useEffect(() => {
    let active = true;
    void (async () => {
      const reports = await listJenkinsFailureReports();
      if (!active) return;
      setRecentReports(reports);
      const current = reports.find((item) => ["collecting", "investigating", "waiting_budget"].includes(item.status)) ?? reports[0];
      if (current) await load(current.id, "");
    })();
    return () => { active = false; };
  }, []);
  useEffect(() => {
    if (!report || ["complete", "failed", "cancelled"].includes(report.status)) return;
    const timer = window.setInterval(() => void load(report.id), 3000);
    return () => window.clearInterval(timer);
  }, [report?.id, report?.status, job]);
  async function start(mode: "regular" | "deep") {
    setLoading(true);
    try {
      const next = await createJenkinsFailureReport(mode);
      setReport(next);
      setRecentReports((reports) => [next, ...reports.filter((report) => report.id !== next.id)]);
    } finally { setLoading(false); }
  }
  return <Box>
    <PageHeader title="Jenkins Failure Reports" subtitle="Every failed Jenkins build in a fixed time window receives its own evidence-backed investigation." actions={<Stack direction="row" gap={1}><Button variant="contained" onClick={() => void start("regular")} disabled={loading}>Regular 4h</Button><Button variant="outlined" onClick={() => void start("deep")} disabled={loading}>Deep 24h</Button></Stack>} />
    {!report && <Alert severity="info">Start a report to collect the full Jenkins failure set for a fixed window.</Alert>}
    {report && <Stack gap={2}>
      <Stack direction={{ xs: "column", md: "row" }} gap={1} alignItems={{ md: "center" }}>
        <Chip label={report.status.replaceAll("_", " ")} color={report.status === "complete" ? "success" : "primary"} />
        <Typography variant="body2">{report.jobs_discovered} jobs checked · {report.failures_found} failed builds · {report.total_builds} rows</Typography>
        {report.budget_reset_at && <Typography variant="body2" color="warning.main">Budget resumes {new Date(report.budget_reset_at).toLocaleString()}</Typography>}
      </Stack>
      <Stack direction="row" gap={1} flexWrap="wrap">
        {Object.entries(report.counts).map(([status, count]) => <Chip key={status} size="small" label={`${count} ${status.replaceAll("_", " ")}`} color={status === "explained" ? "success" : status === "waiting_budget" ? "warning" : "default"} />)}
      </Stack>
      {recentReports.length > 1 && <TextField select size="small" label="Report" value={report.id} onChange={(event) => void load(event.target.value, "")} sx={{ maxWidth: 520 }}>
        {recentReports.map((item) => <MenuItem key={item.id} value={item.id}>{new Date(item.window_ended_at).toLocaleString()} · {item.mode} · {item.status}</MenuItem>)}
      </TextField>}
      {report.coverage_exceptions.length > 0 && <Alert severity="warning">{report.coverage_exceptions.length} coverage exceptions: {report.coverage_exceptions.slice(0, 5).map((item) => String(item.job_name ?? item.scope ?? item.kind)).join(", ")}{report.coverage_exceptions.length > 5 ? "..." : ""}</Alert>}
      <TextField size="small" label="Filter job" value={job} onChange={(event) => setJob(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void load(report.id); }} sx={{ maxWidth: 360 }} />
      <Table size="small"><TableHead><TableRow><TableCell>Build</TableCell><TableCell>Time</TableCell><TableCell>Source</TableCell><TableCell>Agent</TableCell><TableCell>Status</TableCell></TableRow></TableHead><TableBody>{report.builds.map((build) => <TableRow key={build.id} hover onClick={() => setSelected(build)} sx={{ cursor: "pointer" }}><TableCell><Link to={`/jenkins/builds/${build.build_id}`}>{build.job_name} #{build.build_number}</Link></TableCell><TableCell>{new Date(build.started_at).toLocaleString()}</TableCell><TableCell>{String(build.source.change_number ?? build.source.repository ?? "-")}</TableCell><TableCell>{build.investigation_status ?? "queued"}</TableCell><TableCell><Chip size="small" label={build.status.replaceAll("_", " ")} /></TableCell></TableRow>)}</TableBody></Table>
    </Stack>}
    <Drawer anchor="right" open={Boolean(selected)} onClose={() => setSelected(null)}><Box sx={{ width: { xs: "100vw", sm: 480 }, p: 3 }}><Typography variant="h6">{selected?.job_name} #{selected?.build_number}</Typography><Typography sx={{ mt: 2 }} variant="subtitle2">Root cause</Typography><Typography>{String(selected?.assessment?.root_cause ?? selected?.error_summary ?? "Agent investigation is still in progress.")}</Typography><Typography sx={{ mt: 2 }} variant="subtitle2">Suggested fix</Typography><Typography>{String(selected?.assessment?.suggested_fix ?? "-")}</Typography><Typography sx={{ mt: 2 }} variant="subtitle2">Evidence</Typography><pre style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify(selected?.assessment?.evidence ?? [], null, 2)}</pre></Box></Drawer>
  </Box>;
}
