import { Box, CircularProgress, Typography } from "@mui/material";
import { lazy, Suspense, useEffect, useState, type ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";

const Actions = lazy(() => import("./pages/Actions"));
const ActionDetailPage = lazy(() => import("./pages/ActionDetail"));
const Assistant = lazy(() => import("./pages/Assistant"));
const IncidentDetailPage = lazy(() => import("./pages/IncidentDetail"));
const Incidents = lazy(() => import("./pages/Incidents"));
const ScanDetailPage = lazy(() => import("./pages/ScanDetail"));
const Scans = lazy(() => import("./pages/Scans"));

function AuthGate({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<"loading" | "ok" | "unauthorized">("loading");

  useEffect(() => {
    fetch("/auth/me")
      .then((response) => setStatus(response.ok ? "ok" : "unauthorized"))
      .catch(() => setStatus("unauthorized"));
  }, []);

  if (status === "loading") {
    return (
      <Box sx={{ display: "grid", placeItems: "center", height: "100vh" }}>
        <CircularProgress size={30} />
      </Box>
    );
  }
  if (status === "unauthorized") {
    window.location.assign("/auth/login");
    return <Typography sx={{ p: 3 }}>Redirecting to sign in</Typography>;
  }
  return children;
}

export default function App() {
  return (
    <AuthGate>
      <Suspense fallback={<Box sx={{ minHeight: 240, display: "grid", placeItems: "center" }}><CircularProgress size={28} /></Box>}>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<Navigate to="/scans" replace />} />
            <Route path="/scans" element={<Scans />} />
            <Route path="/scans/:scanId" element={<ScanDetailPage />} />
            <Route path="/incidents" element={<Incidents />} />
            <Route path="/incidents/:incidentId" element={<IncidentDetailPage />} />
            <Route path="/actions" element={<Actions />} />
            <Route path="/actions/:actionId" element={<ActionDetailPage />} />
            <Route path="/assistant" element={<Assistant />} />
            <Route path="*" element={<Navigate to="/scans" replace />} />
          </Route>
        </Routes>
      </Suspense>
    </AuthGate>
  );
}
