import { Routes, Route, Navigate } from "react-router-dom";
import { useEffect, useState } from "react";
import { Box, CircularProgress, Typography } from "@mui/material";
import Layout from "./components/Layout";
import Dashboard from "./pages/Dashboard";
import Findings from "./pages/Findings";
import JiraIssues from "./pages/JiraIssues";
import Chat from "./pages/Chat";
import { ScanProvider } from "./context/ScanContext";

function AuthGate({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<"loading" | "ok" | "unauthorized">("loading");

  useEffect(() => {
    fetch("/auth/me")
      .then((r) => {
        if (r.ok) setStatus("ok");
        else setStatus("unauthorized");
      })
      .catch(() => setStatus("unauthorized"));
  }, []);

  if (status === "loading") {
    return (
      <Box sx={{ display: "flex", justifyContent: "center", alignItems: "center", height: "100vh" }}>
        <CircularProgress />
      </Box>
    );
  }

  if (status === "unauthorized") {
    window.location.href = "/auth/login";
    return (
      <Box sx={{ display: "flex", flexDirection: "column", justifyContent: "center", alignItems: "center", height: "100vh", gap: 2 }}>
        <Typography variant="h6">Redirecting to login...</Typography>
      </Box>
    );
  }

  return <>{children}</>;
}

export default function App() {
  return (
    <AuthGate>
      <ScanProvider>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/" element={<Dashboard />} />
            <Route path="/findings" element={<Findings />} />
            <Route path="/jira" element={<JiraIssues />} />
            <Route path="/chat" element={<Chat />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </ScanProvider>
    </AuthGate>
  );
}
