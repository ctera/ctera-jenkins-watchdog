import { createContext, useContext, useCallback, useRef, useState, type ReactNode } from "react";
import { streamScan, type ScanEvent } from "../services/api";

interface ScanState {
  scanning: boolean;
  status: string;
  events: ScanEvent[];
}

interface ScanContextValue {
  scan: ScanState;
  startScan: () => void;
}

const ScanContext = createContext<ScanContextValue | null>(null);

export function ScanProvider({ children }: { children: ReactNode }) {
  const [scan, setScan] = useState<ScanState>({ scanning: false, status: "", events: [] });
  const runningRef = useRef(false);

  const startScan = useCallback(() => {
    if (runningRef.current) return;
    runningRef.current = true;
    setScan({ scanning: true, status: "Starting scan...", events: [] });

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
              case "scan_complete":
                status = `Done: ${event.total_findings} findings, ${event.investigations_performed} investigated (${event.duration_s}s)`;
                break;
            }
            return { scanning: true, status, events };
          });
        }
      } catch (e) {
        setScan((prev) => ({ ...prev, status: `Error: ${e instanceof Error ? e.message : "unknown"}` }));
      } finally {
        setScan((prev) => ({ ...prev, scanning: false }));
        runningRef.current = false;
      }
    })();
  }, []);

  return <ScanContext.Provider value={{ scan, startScan }}>{children}</ScanContext.Provider>;
}

export function useScan() {
  const ctx = useContext(ScanContext);
  if (!ctx) throw new Error("useScan must be inside ScanProvider");
  return ctx;
}
