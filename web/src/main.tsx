import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";
import { api, getApiKey, setApiKey, useApi } from "./api";
import { Errors } from "./pages/Errors";
import { DatasetView, EvalsHome, RunView } from "./pages/Evals";
import { RunCompare } from "./pages/RunCompare";
import { TraceCompare } from "./pages/TraceCompare";
import { Metrics } from "./pages/Metrics";
import { TraceList } from "./pages/TraceList";
import { TraceView } from "./pages/TraceView";
import "./styles.css";

function ApiKeyControl({ onChange }: { onChange: () => void }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(getApiKey());
  if (!editing) {
    return (
      <button className="link-button" onClick={() => setEditing(true)}>
        API key
      </button>
    );
  }
  return (
    <form
      className="key-form"
      onSubmit={(e) => {
        e.preventDefault();
        setApiKey(value.trim());
        setEditing(false);
        onChange();
      }}
    >
      <input
        type="password"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="obz_…"
        aria-label="Project API key"
        autoFocus
      />
      <button className="button" type="submit">Save</button>
    </form>
  );
}

function App() {
  const [keyVersion, setKeyVersion] = useState(0);
  const project = useApi(() => api.project(), [keyVersion]);

  return (
    <BrowserRouter>
      <header className="topbar">
        <div className="brand">
          <svg width="20" height="20" viewBox="0 0 16 16" aria-hidden="true">
            <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="2.5" />
            <circle cx="8" cy="8" r="2" fill="currentColor" />
          </svg>
          Obzerve
        </div>
        <nav>
          <NavLink to="/" end>Traces</NavLink>
          <NavLink to="/metrics">Metrics</NavLink>
          <NavLink to="/evals">Evals</NavLink>
          <NavLink to="/errors">Errors</NavLink>
        </nav>
        <div className="topbar-right">
          {project.data && <span className="project">{project.data.name}</span>}
          <ApiKeyControl onChange={() => setKeyVersion((v) => v + 1)} />
        </div>
      </header>
      <main key={keyVersion}>
        <Routes>
          <Route path="/" element={<TraceList />} />
          <Route path="/traces/:traceId" element={<TraceView />} />
          <Route path="/traces/:a/compare/:b" element={<TraceCompare />} />
          <Route path="/evals" element={<EvalsHome />} />
          <Route path="/evals/datasets/:name" element={<DatasetView />} />
          <Route path="/evals/runs/:id" element={<RunView />} />
          <Route path="/evals/compare/:baseline/:candidate" element={<RunCompare />} />
          <Route path="/metrics" element={<Metrics />} />
          <Route path="/errors" element={<Errors />} />
        </Routes>
      </main>
    </BrowserRouter>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
