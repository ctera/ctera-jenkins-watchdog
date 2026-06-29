import { createContext, useContext, useCallback, useRef, useState, type Dispatch, type MutableRefObject, type ReactNode, type SetStateAction } from "react";
import { stopScan, streamScan, type ScanEvent } from "../services/api";

interface ScanState {
  scanning: boolean;
  deep: boolean;
  stopping: boolean;
  status: string;
  events: ScanEvent[];
}

interface ScanContextValue {
  scan: ScanState;
  startScan: () => void;
  startDeepScan: () => void;
  stopScan: () => void;
}

const ScanContext = createContext<ScanContextValue | null>(null);

function runScanStream(
  deep: boolean,
  setScan: Dispatch<SetStateAction<ScanState>>,
  runningRef: MutableRefObject<boolean>,
) {
  if (runningRef.current) return;
  runningRef.current = true;
  setScan({
    scanning: true,
    deep,
    stopping: false,
    status: deep ? "Starting deep scan..." : "Starting scan...",
    events: [],
  });

  (async () => {
    try {
      for await (const event of streamScan({ deep })) {
        setScan((prev) => {
          const events = [...prev.events, event];
          let status = prev.status;
          const prefix = prev.deep ? "Deep scan" : "Scan";
          switch (event.type) {
            case "scan_started":
              status = event.deep
                ? `Deep scan ${event.scan_id} started (24h window, up to 50 investigations)`
                : `Scan ${event.scan_id} started`;
              break;
            case "detection_complete":
              status = event.deep
                ? `Detection complete (${event.window_hours}h window): ${event.total_findings} findings`
                : `Detection complete: ${event.total_findings} findings`;
              break;
            case "triage_start":
              status = `Triaging ${event.count} findings...`;
              break;
            case "triage_complete":
              status = `Triage complete: ${event.total_findings} findings (${event.dismissed_count} dismissed)`;
              break;
            case "triage_skipped":
              status = `Deep scan: keeping all ${event.count} findings (no triage)`;
              break;
            case "investigation_plan":
              status = event.deep
                ? `Deep investigating ${event.count} findings (warning+)...`
                : `Investigating ${event.count} findings...`;
              break;
            case "investigation_start":
              status = `[${event.index}/${event.total}] Investigating ${event.resource}`;
              break;
            case "tool_call":
              status = `  → Calling ${event.tool}`;
              break;
            case "reasoning":
              status = `  → Analyzing...`;
              break;
            case "investigation_complete":
              status = `  ✓ ${event.resource}: ${event.confidence} confidence`;
              break;
            case "error":
              status = `Error: ${event.message || "Unknown error"}`;
              break;
            case "scan_stopped":
              status = `${prefix} stopped (${event.duration_s}s)`;
              break;
            case "scan_complete":
              status = event.deep
                ? `Deep scan done: ${event.total_findings} findings, ${event.investigations_performed} investigated (${event.duration_s}s)`
                : `Done: ${event.total_findings} findings, ${event.investigations_performed} investigated (${event.duration_s}s)`;
              break;
          }
          return { scanning: true, deep: prev.deep, stopping: prev.stopping, status, events };
        });

        if (event.type === "scan_stopped" || event.type === "scan_complete") {
          break;
        }
      }
    } catch (e) {
      setScan((prev) => ({ ...prev, status: `Error: ${e instanceof Error ? e.message : "unknown"}` }));
    } finally {
      setScan((prev) => ({ ...prev, scanning: false, stopping: false }));
      runningRef.current = false;
    }
  })();
}

export function ScanProvider({ children }: { children: ReactNode }) {
  const [scan, setScan] = useState<ScanState>({
    scanning: false,
    deep: false,
    stopping: false,
    status: "",
    events: [],
  });
  const runningRef = useRef(false);

  const startScan = useCallback(() => {
    runScanStream(false, setScan, runningRef);
  }, []);

  const startDeepScan = useCallback(() => {
    runScanStream(true, setScan, runningRef);
  }, []);

  const handleStopScan = useCallback(() => {
    setScan((prev) => ({
      ...prev,
      stopping: true,
      status: prev.deep ? "Stopping deep scan..." : "Stopping scan...",
    }));
    stopScan().catch((e) => {
      setScan((prev) => ({
        ...prev,
        stopping: false,
        status: `Failed to stop: ${e instanceof Error ? e.message : "unknown"}`,
      }));
    });
  }, []);

  return (
    <ScanContext.Provider value={{ scan, startScan, startDeepScan, stopScan: handleStopScan }}>
      {children}
    </ScanContext.Provider>
  );
}

export function useScan() {
  const ctx = useContext(ScanContext);
  if (!ctx) throw new Error("useScan must be inside ScanProvider");
  return ctx;
}
