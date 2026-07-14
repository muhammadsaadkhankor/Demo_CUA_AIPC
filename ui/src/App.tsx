import React, { useState, useEffect, useRef } from "react";
import "./App.css";

// Browser speech recognition
const SpeechRecognition =
  (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;

const API = "http://localhost:5000";

type Status = "idle" | "running" | "success" | "done" | "error";

interface StatusResponse {
  running: boolean;
  status: Status;
  log: string[];
}

const MODEL_LABELS: Record<string, string> = {
  "2b":     "UI-TARS 2B (fast)",
  "7b-sft": "UI-TARS 7B SFT",
  "7b-dpo": "UI-TARS 7B DPO",
  "1.5-7b": "UI-TARS 1.5 7B",
};



export default function App() {
  const [appName, setAppName] = useState("");
  const [status, setStatus] = useState<Status>("idle");
  const [log, setLog] = useState<string[]>([]);
  const [error, setError] = useState("");

  const [listening, setListening] = useState(false);
  const [interim, setInterim] = useState("");
  const [showSettings, setShowSettings] = useState(false);
  const [maxSteps, setMaxSteps] = useState(20);
  const [profile, setProfile] = useState("2b");
  const [maxLongSide, setMaxLongSide] = useState(1280);
  const logRef = useRef<HTMLDivElement>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const recognitionRef = useRef<any>(null);

  useEffect(() => { document.title = "CUA AIPC"; }, []);

  // Load current config from backend on mount
  useEffect(() => {
    fetch(`${API}/config`).then(r => r.json()).then(d => {
      if (d.max_steps)    setMaxSteps(d.max_steps);
      if (d.max_long_side) setMaxLongSide(d.max_long_side);
    }).catch(() => {});
  }, []);

  const applySettings = async () => {
    try {
      await fetch(`${API}/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ max_steps: maxSteps, profile, max_long_side: maxLongSide }),
      });
      setShowSettings(false);
    } catch {}
  };

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [log]);

  useEffect(() => {
    if (status !== "running") return;
    pollRef.current = setInterval(async () => {
      try {
        const res = await fetch(`${API}/status`);
        const data: StatusResponse = await res.json();
        setLog(data.log);
        if (!data.running) { setStatus(data.status); clearInterval(pollRef.current!); }
      } catch {}
    }, 800);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [status]);

  const handleLaunch = async () => {
    if (!appName.trim() || status === "running") return;
    setError(""); setLog([]); setStatus("running");
    try {
      const res = await fetch(`${API}/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ app: appName.trim() }),
      });
      if (!res.ok) {
        const data = await res.json();
        setError(data.error || "Failed to start agent.");
        setStatus("error");
      }
    } catch {
      setError("Cannot reach backend. Is agent.py running on port 5000?");
      setStatus("error");
    }
  };

  const isRunning = status === "running";

  const handleMic = () => {
    if (!SpeechRecognition) {
      setError("Speech recognition not supported in this browser.");
      return;
    }
    if (listening) {
      recognitionRef.current?.stop();
      return;
    }
    const rec = new SpeechRecognition();
    rec.lang = "en-US";
    rec.interimResults = true;
    rec.continuous = true;
    rec.onstart = () => setListening(true);
    rec.onresult = (e: any) => {
      let final = "";
      let interimText = "";
      for (let i = 0; i < e.results.length; i++) {
        if (e.results[i].isFinal) final += e.results[i][0].transcript;
        else interimText += e.results[i][0].transcript;
      }
      if (final) setAppName(final);
      setInterim(interimText);
    };
    rec.onerror = () => { setListening(false); setInterim(""); };
    rec.onend = () => { setListening(false); setInterim(""); };
    recognitionRef.current = rec;
    rec.start();
  };

  const statusMeta: Record<Status, { label: string; icon: string }> = {
    idle:    { label: "Ready",              icon: "◉" },
    running: { label: "Agent running...",   icon: "⟳" },
    success: { label: "Successfully opened!", icon: "✓" },
    done:    { label: "Done",               icon: "✓" },
    error:   { label: "Error",              icon: "✕" },
  };

  return (
    <div className="shell">
      {/* Sidebar */}
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-icon">⬡</span>
          <div>
            <div className="brand-name">CUA AIPC</div>
            <div className="brand-sub">Computer Use Agent</div>
          </div>
        </div>

        <div className="section-label">LAUNCH APP</div>

        <div className="input-row">
          <div className={`search-wrap${listening ? " search-listening" : ""}`}>
            <span className="search-icon">⌕</span>
            <input
              className="search-input"
              type="text"
              placeholder="e.g. Open Calculator, Browse YouTube..."
              value={listening ? (appName + (interim ? " " + interim : "")) : appName}
              onChange={e => !listening && setAppName(e.target.value)}
              onKeyDown={e => e.key === "Enter" && handleLaunch()}
              disabled={isRunning}
              autoFocus
              readOnly={listening}
            />
            {listening && (
              <div className="waveform">
                {[...Array(5)].map((_, i) => (
                  <span key={i} className="wave-bar" style={{ animationDelay: `${i * 0.12}s` }} />
                ))}
              </div>
            )}
          </div>

          <button
            className={`mic-fab${listening ? " mic-fab-active" : ""}`}
            onClick={handleMic}
            disabled={isRunning}
            title={listening ? "Tap to stop" : "Tap to speak"}
          >
            {listening && <span className="ripple" />}
            {listening && <span className="ripple ripple-2" />}
            <svg viewBox="0 0 24 24" fill="currentColor">
              <rect x="9" y="2" width="6" height="12" rx="3" />
              <path d="M5 11a7 7 0 0 0 14 0" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
              <line x1="12" y1="18" x2="12" y2="22" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
              <line x1="9" y1="22" x2="15" y2="22" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
            </svg>
          </button>
        </div>

        <button
          className={`launch-btn${isRunning ? " running" : ""}`}
          onClick={handleLaunch}
          disabled={isRunning || !appName.trim()}
        >
          {isRunning ? <><span className="spinner" /> Running...</> : "▶  Launch"}
        </button>

        {error && <div className="error-pill">⚠ {error}</div>}

        {/* Settings panel */}
        <div className="settings-wrap">
          <button className="settings-toggle" onClick={() => setShowSettings(s => !s)}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="3"/>
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
            </svg>
            Agent Settings
            <span className="settings-arrow">{showSettings ? "▴" : "▾"}</span>
          </button>

          {showSettings && (
            <div className="settings-panel">
              <div className="setting-row">
                <label className="setting-label">
                  <div className="setting-label-row">
                    Max Steps
                    <span className="setting-value">{maxSteps}</span>
                  </div>
                  <span className="setting-hint">How many actions the agent can take to find &amp; open the app</span>
                </label>
                <input
                  className="setting-range"
                  type="range" min={1} max={50} step={1}
                  value={maxSteps}
                  onChange={e => setMaxSteps(Number(e.target.value))}
                  style={{ "--pct": `${((maxSteps - 1) / 49) * 100}%` } as any}
                />
                <div className="range-ticks">
                  <span>1</span><span>25</span><span>50</span>
                </div>
              </div>

              <div className="setting-row">
                <label className="setting-label">
                  Model
                  <span className="setting-hint">2B is faster, 7B is more accurate</span>
                </label>
                <select
                  className="setting-select"
                  value={profile}
                  onChange={e => setProfile(e.target.value)}
                >
                  {Object.entries(MODEL_LABELS).map(([k, v]) => (
                    <option key={k} value={k}>{v}</option>
                  ))}
                </select>
              </div>

              <div className="setting-row">
                <label className="setting-label">
                  <div className="setting-label-row">
                    Screenshot Width
                    <span className="setting-value">{maxLongSide}px</span>
                  </div>
                  <span className="setting-hint">Lower = faster model, Higher = more detail</span>
                </label>
                <input
                  className="setting-range"
                  type="range" min={640} max={1920} step={320}
                  value={maxLongSide}
                  onChange={e => setMaxLongSide(Number(e.target.value))}
                  style={{ "--pct": `${((maxLongSide - 640) / (1920 - 640)) * 100}%` } as any}
                />
                <div className="range-ticks">
                  <span>640</span><span>1280</span><span>1920</span>
                </div>
              </div>

              <button className="settings-apply" onClick={applySettings}>
                Apply
              </button>
            </div>
          )}
        </div>

        <div className="status-card" data-status={status}>
          <span className={`status-dot dot-${status}`} />
          <div>
            <div className="status-title">{statusMeta[status].label}</div>
            <div className="status-hint">
              {status === "idle" ? "Type or speak a task above" :
               status === "running" ? "Agent is working on screen..." :
               status === "success" ? "Task completed successfully" :
               status === "done" ? "Agent finished" :
               "Check backend logs"}
            </div>
          </div>
        </div>

        <div className="sidebar-footer">
          <span className="footer-dot" /> vLLM · UI-TARS-2B
        </div>
      </aside>

      {/* Main panel */}
      <main className="main">
        <div className="main-header">
          <div className="main-title">Activity Log</div>
          {log.length > 0 && (
            <button className="clear-btn" onClick={() => setLog([])}>Clear</button>
          )}
        </div>

        <div className="log-box" ref={logRef}>
          {log.length === 0 ? (
            <div className="log-empty">
              <div className="log-empty-icon">⬡</div>
              <div>No activity yet</div>
              <div className="log-empty-sub">Launch an app to see agent logs here</div>
            </div>
          ) : (
            log.map((line, i) => (
              <div key={i} className={`log-line${
                line.startsWith("[error]") ? " log-error" :
                line.startsWith("Successfully") ? " log-success" : ""
              }`}>
                <span className="log-num">{String(i + 1).padStart(2, "0")}</span>
                <span>{line}</span>
              </div>
            ))
          )}
        </div>
      </main>
    </div>
  );
}
