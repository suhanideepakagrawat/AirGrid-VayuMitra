// VayuMitra dock - the live citizen assistant, one tap away on every operator
// page. Slides in from the right; the iframe mounts on first open and stays
// mounted so the conversation survives closing the panel.

import { useEffect, useRef, useState } from "react";
import { CITIZEN_APP_URL } from "@/lib/api";

export function VayuMitraDock() {
  const [open, setOpen] = useState(false);
  const [everOpened, setEverOpened] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  // Anywhere in the app (e.g. the your-location banner) can summon the dock.
  useEffect(() => {
    const onOpen = () => {
      setOpen(true);
      setEverOpened(true);
    };
    window.addEventListener("open-vayumitra", onOpen);
    return () => window.removeEventListener("open-vayumitra", onOpen);
  }, []);

  const toggle = () => {
    setOpen((o) => !o);
    setEverOpened(true);
  };

  return (
    <>
      <button
        onClick={toggle}
        aria-expanded={open}
        aria-controls="vayumitra-dock"
        className="fixed bottom-5 right-5 z-30 inline-flex items-center gap-2 rounded-full bg-accent py-2.5 pl-3.5 pr-4 text-[12.5px] font-semibold text-white shadow-[0_8px_28px_rgba(9,20,28,0.18)] transition-colors hover:bg-[#064a42]"
      >
        <ChatGlyph />
        {open ? "Close VayuMitra" : "Ask VayuMitra"}
      </button>

      <div
        id="vayumitra-dock"
        ref={panelRef}
        role="dialog"
        aria-label="VayuMitra citizen assistant"
        aria-hidden={!open}
        className="dock-panel fixed inset-y-0 right-0 z-40 flex w-full max-w-[420px] flex-col border-l border-border bg-panel shadow-[0_8px_28px_rgba(9,20,28,0.18)]"
        style={{ transform: open ? "translateX(0)" : "translateX(105%)" }}
      >
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
          <div>
            <div className="text-[14px] font-bold">VayuMitra - citizen advisory</div>
            <div className="text-[11px] text-text-dim">English · हिन्दी · voice · cited to CPCB / SAFAR / WHO / GRAP</div>
          </div>
          <div className="flex items-center gap-1.5">
            <a
              href={CITIZEN_APP_URL}
              target="_blank"
              rel="noopener"
              className="rounded-full border border-border px-3 py-1.5 text-[11px] font-semibold text-text-dim hover:border-accent-dim hover:text-accent"
            >
              Full app ↗
            </a>
            <button
              onClick={() => setOpen(false)}
              aria-label="Close VayuMitra panel"
              className="grid h-8 w-8 place-items-center rounded-full text-text-dim hover:bg-surface-1 hover:text-foreground"
            >
              <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden>
                <path d="M2 2l10 10M12 2L2 12" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
              </svg>
            </button>
          </div>
        </div>
        {everOpened ? (
          <iframe
            src={CITIZEN_APP_URL}
            title="VayuMitra citizen advisory (live)"
            className="min-h-0 flex-1 border-0 bg-white"
            allow="microphone; autoplay"
          />
        ) : (
          <div className="flex-1" />
        )}
      </div>
    </>
  );
}

function ChatGlyph() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden>
      <path
        d="M8 1.5c3.7 0 6.5 2.5 6.5 5.7 0 3.2-2.8 5.7-6.5 5.7-.7 0-1.4-.1-2-.3L2.6 14l.9-2.8C2.2 10.2 1.5 8.8 1.5 7.2 1.5 4 4.3 1.5 8 1.5z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
      />
      <circle cx="5.4" cy="7.2" r="0.9" fill="currentColor" />
      <circle cx="8" cy="7.2" r="0.9" fill="currentColor" />
      <circle cx="10.6" cy="7.2" r="0.9" fill="currentColor" />
    </svg>
  );
}
