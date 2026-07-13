import { useEffect, useState } from "react";
import { streamScanEvents, type ScanEventEnvelope } from "../services/api";

export function useScanEvents(scanId: string | undefined, onSettled?: () => void, resume = true) {
  const [events, setEvents] = useState<ScanEventEnvelope[]>([]);
  const [connection, setConnection] = useState<"idle" | "live" | "closed" | "error">("idle");

  useEffect(() => {
    if (!scanId) return;
    const controller = new AbortController();
    setEvents([]);
    setConnection("live");
    void (async () => {
      try {
        for await (const event of streamScanEvents(scanId, controller.signal, resume)) {
          if (event.type === "heartbeat") continue;
          setEvents((current) => [...current.filter((item) => item.sequence !== event.sequence), event].slice(-100));
        }
        if (!controller.signal.aborted) {
          setConnection("closed");
          onSettled?.();
        }
      } catch (error) {
        if (!controller.signal.aborted && (!(error instanceof DOMException) || error.name !== "AbortError")) {
          setConnection("error");
        }
      }
    })();
    return () => controller.abort();
  }, [scanId, onSettled, resume]);

  return { events, connection };
}
