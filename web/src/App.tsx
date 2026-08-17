import { HashRouter, Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { Library } from "./screens/Library";
import { JobDetail } from "./screens/JobDetail";
import { ReviewQueue } from "./screens/ReviewQueue";
import { ConfigEditor } from "./screens/ConfigEditor";
import { Targets } from "./screens/Targets";
import { Settings } from "./screens/Settings";
import { SystemStatus } from "./screens/SystemStatus";

// HashRouter keeps every route working from a plain `file://` preview and
// from the FastAPI static file mount with no server-side route rewriting.
// Refer to APP-CONTRACT.md section 2 — the SPA is served as static files.
export function App() {
  return (
    <HashRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<Navigate to="/jobs" replace />} />
          <Route path="/jobs" element={<Library />} />
          <Route path="/jobs/:id" element={<JobDetail />} />
          <Route path="/jobs/:id/config" element={<ConfigEditor />} />
          <Route path="/gates" element={<ReviewQueue />} />
          <Route path="/targets" element={<Targets />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/system" element={<SystemStatus />} />
          <Route path="*" element={<Navigate to="/jobs" replace />} />
        </Route>
      </Routes>
    </HashRouter>
  );
}
