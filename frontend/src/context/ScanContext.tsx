import { createContext, useContext, useCallback, useRef, useState, type ReactNode } from "react";
import { stopScan, streamScan, type ScanEvent } from "../services/api";

interface ScanState {
  scanning: boolean;
  stopping: boolean;
  status: string;
  events: ScanEvent[];
}

interface ScanContextValue {
  scan: ScanState;
  startScan: () => void;
  stopScan: () => void;
}

const ScanContext = createContext<ScanContextValue | null>(null);

export function ScanProvider({ children }: { children: ReactNode }) {
  const [scan, setScan] = useState<ScanState>({ scanning: false, stopping: false, status: "", events: [] });
  const runningRef = useRef(false);

  const startScan = useCallback(() => {
    if (runningRef.current) return;
    runningRef.current = true;
    setScan({ scanning: true, stopping: false, status: "Starting scan...", events: [] });

    (async () => {
      try {
        for await (const event of streamScan(false)) {
          setScan((prev) => {
            const events = [...prev.events, event];
            let status = prev.status;
            switch (event.type) {
              case "scan_started":
                status = `Scan ${event.scan_id} started`;
                break;
              case "detection_complete":
                status = `Detection complete: ${event.total_findings} findings`;
                break;
              case "investigation_plan":
                status = `Investigating ${event.count} findings...`;
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
                status = `Scan stopped (${event.duration_s}s)`;
                break;
              case "scan_complete":
                status = `Done: ${event.total_findings} findings, ${event.investigations_performed} investigated (${event.duration_s}s)`;
                break;
            }
            return { scanning: true, stopping: prev.stopping, status, events };
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
  }, []);

  const handleStopScan = useCallback(() => {
    setScan((prev) => ({ ...prev, stopping: true, status: "Stopping scan..." }));
    stopScan().catch((e) => {
      setScan((prev) => ({
        ...prev,
        stopping: false,
        status: `Failed to stop: ${e instanceof Error ? e.message : "unknown"}`,
      }));
    });
  }, []);

  return (
    <ScanContext.Provider value={{ scan, startScan, stopScan: handleStopScan }}>
      {children}
    </ScanContext.Provider>
  );
}

export function useScan() {
  const ctx = useContext(ScanContext);
  if (!ctx) throw new Error("useScan must be inside ScanProvider");
  return ctx;
}
