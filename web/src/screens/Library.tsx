// Screen 1: Library / Jobs. The table of books.
//
// Warning: the three gate states must be visually loud. A book that waits
// for a person is the single most important thing on this screen. Refer to
// APP-CONTRACT.md section 9 and the frontend brief.

import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { getApiClient } from "../api";
import type { Job, JobState } from "../api/types";
import { GATE_STATES } from "../api/types";

/** States where the runner is not doing active work right now. The
 * indeterminate bar's moving stripes are the "something is happening"
 * signal, so a job with no active work gets a plain dash instead — a
 * queued or paused book must not look like it is already running. */
const INACTIVE_STATES: readonly JobState[] = ["queued", "paused", "cancelled", "failed"];
import { usePolling } from "../hooks/usePolling";
import { StatePill } from "../components/StatePill";
import { ProgressBar } from "../components/ProgressBar";
import { formatAge } from "../utils/time";

const STATE_FILTERS: Array<{ value: JobState | ""; label: string }> = [
  { value: "", label: "All states" },
  { value: "awaiting_sample_approval", label: "Waiting: sample" },
  { value: "awaiting_homograph_review", label: "Waiting: homograph" },
  { value: "awaiting_qc_review", label: "Waiting: QC" },
  { value: "running", label: "Running" },
  { value: "queued", label: "Queued" },
  { value: "delivering", label: "Delivering" },
  { value: "done", label: "Done" },
  { value: "failed", label: "Failed" },
  { value: "paused", label: "Paused" },
  { value: "cancelled", label: "Cancelled" },
];

function CoverThumb({ job }: { job: Job }) {
  const initials = (job.title ?? job.slug)
    .split(/\s+/)
    .map((w) => w[0])
    .slice(0, 2)
    .join("")
    .toUpperCase();
  return (
    <div className="cover-thumb" aria-hidden="true">
      {initials}
    </div>
  );
}

export function Library() {
  const client = useMemo(() => getApiClient(), []);
  const [stateFilter, setStateFilter] = useState<JobState | "">("");
  const [q, setQ] = useState("");

  const { data, loading, error } = usePolling(
    () => client.listJobs({ state: stateFilter || undefined, q: q || undefined, limit: 200 }),
    5000,
    [stateFilter, q],
  );

  const jobs = data?.items ?? [];
  const gateCount = jobs.filter((j) => (GATE_STATES as JobState[]).includes(j.state)).length;

  return (
    <div className="stack">
      <div className="panel__header">
        <h1>Library</h1>
        {gateCount > 0 && (
          <Link to="/gates" className="pill pill--gate">
            {gateCount} waiting for you
          </Link>
        )}
      </div>

      <div className="filter-bar">
        <label htmlFor="state-filter" style={{ margin: 0 }}>
          State
        </label>
        <select
          id="state-filter"
          value={stateFilter}
          onChange={(e) => setStateFilter(e.target.value as JobState | "")}
        >
          {STATE_FILTERS.map((f) => (
            <option key={f.value} value={f.value}>
              {f.label}
            </option>
          ))}
        </select>
        <label htmlFor="job-search" style={{ margin: 0 }}>
          Search
        </label>
        <input
          id="job-search"
          type="search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Title or author"
        />
      </div>

      {error && (
        <p className="callout callout--warn" role="alert">
          {error}
        </p>
      )}

      {loading && !data && <p className="muted">Loading the library…</p>}

      {data && jobs.length === 0 && (
        <div className="empty-state">No book matches this filter.</div>
      )}

      {jobs.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col" aria-hidden="true"></th>
              <th scope="col">Title</th>
              <th scope="col">Author</th>
              <th scope="col">State</th>
              <th scope="col">Stage</th>
              <th scope="col">Progress</th>
              <th scope="col">Targets</th>
              <th scope="col">Age</th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((job) => {
              const isGate = (GATE_STATES as JobState[]).includes(job.state);
              return (
                <tr key={job.id} className={isGate ? "data-table__row--gate" : ""}>
                  <td>
                    <CoverThumb job={job} />
                  </td>
                  <td>
                    <Link to={`/jobs/${job.id}`}>{job.title ?? job.slug}</Link>
                  </td>
                  <td>{job.author ?? "—"}</td>
                  <td>
                    <StatePill state={job.state} />
                  </td>
                  <td>{job.stage ?? "—"}</td>
                  <td>
                    {INACTIVE_STATES.includes(job.state) ? (
                      <span className="muted">—</span>
                    ) : (
                      <ProgressBar done={job.progress_done} total={job.progress_total} />
                    )}
                  </td>
                  <td className="muted">—</td>
                  <td>{formatAge(job.updated_at)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
