// Screen 2: Job detail. The metadata, the per-stage status, the artifacts,
// the deliveries, and the live event log. Refer to APP-CONTRACT.md section
// 13.2.

import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getApiClient } from "../api";
import type { Artifact, Delivery, JobDetail as JobDetailType, JobEvent, JobStatus } from "../api/types";
import { usePolling } from "../hooks/usePolling";
import { StatePill } from "../components/StatePill";
import { ProgressBar } from "../components/ProgressBar";
import { formatBytes, formatStamp } from "../utils/time";

function StageStatusTable({ status }: { status: JobStatus | null }) {
  if (!status) return <p className="muted">Loading stage status…</p>;
  const stages = Object.keys(status) as Array<keyof JobStatus>;
  return (
    <table className="data-table">
      <thead>
        <tr>
          <th scope="col">Stage</th>
          <th scope="col">Fresh</th>
          <th scope="col">Stale</th>
          <th scope="col">Absent</th>
        </tr>
      </thead>
      <tbody>
        {stages.map((stage) => {
          const entry = status[stage];
          if (!entry) return null;
          return (
            <tr key={String(stage)}>
              <td>{stage}</td>
              <td>{entry.fresh}</td>
              <td>{entry.stale > 0 ? <span className="flag">{entry.stale} stale</span> : entry.stale}</td>
              <td>{entry.absent}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function ArtifactsTable({ artifacts }: { artifacts: Artifact[] }) {
  if (artifacts.length === 0) return <p className="muted">No artifact yet.</p>;
  return (
    <table className="data-table">
      <thead>
        <tr>
          <th scope="col">Artifact</th>
          <th scope="col">Path</th>
          <th scope="col">Size</th>
          <th scope="col">Present</th>
        </tr>
      </thead>
      <tbody>
        {artifacts.map((a) => (
          <tr key={a.name}>
            <td>{a.name}</td>
            <td className="muted">{a.path}</td>
            <td>{formatBytes(a.size_bytes)}</td>
            <td>{a.exists ? "yes" : "no"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function DeliveriesTable({ deliveries }: { deliveries: Delivery[] }) {
  if (deliveries.length === 0) return <p className="muted">No delivery yet.</p>;
  return (
    <table className="data-table">
      <thead>
        <tr>
          <th scope="col">Target</th>
          <th scope="col">State</th>
          <th scope="col">Bytes</th>
          <th scope="col">Delivered</th>
        </tr>
      </thead>
      <tbody>
        {deliveries.map((d) => (
          <tr key={d.id}>
            <td className="muted">{d.target_id}</td>
            <td>
              <span className={"pill " + (d.state === "delivered" ? "pill--ok" : d.state === "failed" ? "pill--error" : "pill--active")}>
                {d.state}
              </span>
            </td>
            <td>{formatBytes(d.bytes)}</td>
            <td>{formatStamp(d.delivered_at)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function EventLog({ events }: { events: JobEvent[] }) {
  if (events.length === 0) return <p className="muted">No event yet.</p>;
  return (
    <div className="event-log" role="log" aria-label="Job event log">
      {events.map((e) => (
        <div className="event-log__line" key={e.id}>
          <span className={`event-log__level event-log__level--${e.level}`}>{e.level}</span>
          <span className="muted">{formatStamp(e.created_at)}</span>
          {e.stage && <span className="muted">[{e.stage}]</span>}
          <span>{e.message}</span>
        </div>
      ))}
    </div>
  );
}

export function JobDetail() {
  const { id } = useParams<{ id: string }>();
  const client = useMemo(() => getApiClient(), []);
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [busyAction, setBusyAction] = useState<string | null>(null);

  const { data: job, error, refresh } = usePolling<JobDetailType>(
    () => client.getJob(id as string),
    3000,
    [id],
  );
  const { data: status } = usePolling(() => client.getJobStatus(id as string), 5000, [id]);
  const { data: artifactsResp } = usePolling(() => client.getJobArtifacts(id as string), 5000, [id]);

  useEffect(() => {
    if (!id) return undefined;
    setEvents([]);
    const unsubscribe = client.subscribeJobEvents(id, (event) => {
      setEvents((prev) => {
        if (prev.some((e) => e.id === event.id)) return prev;
        return [...prev, event].slice(-500);
      });
    });
    return unsubscribe;
  }, [client, id]);

  if (!id) return <p>No job id.</p>;
  if (error) return <p className="callout callout--warn" role="alert">{error}</p>;
  if (!job) return <p className="muted">Loading the job…</p>;

  async function run(action: string, fn: () => Promise<void>) {
    setBusyAction(action);
    try {
      await fn();
      refresh();
    } finally {
      setBusyAction(null);
    }
  }

  const canStart = job.state === "queued" || job.state === "failed";
  const canPause = job.state === "running";
  const canCancel = !["done", "cancelled", "failed"].includes(job.state);
  const canRetry = job.state === "failed";
  const canDeliver = job.state === "done" || job.state === "delivering";

  return (
    <div className="stack">
      <div className="panel__header">
        <div>
          <h1>{job.title ?? job.slug}</h1>
          <p className="muted">
            {job.author ?? "Unknown author"} {job.year ? `· ${job.year}` : ""} · {job.slug}
          </p>
        </div>
        <StatePill state={job.state} />
      </div>

      <div className="panel">
        <div className="panel__header">
          <h2 style={{ margin: 0, fontSize: 15 }}>Actions</h2>
          <Link to={`/jobs/${job.id}/config`} className="btn">
            Edit config
          </Link>
        </div>
        <div className="btn-row">
          <button className="btn btn--primary" disabled={!canStart || busyAction !== null} onClick={() => run("start", () => client.startJob(job.id))}>
            Start
          </button>
          <button className="btn" disabled={!canPause || busyAction !== null} onClick={() => run("pause", () => client.pauseJob(job.id))}>
            Pause
          </button>
          <button className="btn btn--danger" disabled={!canCancel || busyAction !== null} onClick={() => run("cancel", () => client.cancelJob(job.id))}>
            Cancel
          </button>
          <button className="btn" disabled={!canRetry || busyAction !== null} onClick={() => run("retry", () => client.retryJob(job.id))}>
            Retry
          </button>
          <button className="btn" disabled={!canDeliver || busyAction !== null} onClick={() => run("deliver", () => client.deliverJob(job.id))}>
            Deliver
          </button>
        </div>
        {job.error && (
          <p className="callout callout--warn" role="alert" style={{ marginTop: 12 }}>
            {job.error}
          </p>
        )}
      </div>

      <div className="panel">
        <h2 style={{ fontSize: 15 }}>Progress</h2>
        <ProgressBar done={job.progress_done} total={job.progress_total} startedAt={job.started_at} />
      </div>

      {job.gates.some((g) => g.state === "open") && (
        <div className="callout callout--warn">
          <p>
            This book waits for a person. Resolve it from the{" "}
            <Link to="/gates">review queue</Link>.
          </p>
        </div>
      )}

      <div className="grid-2">
        <div className="panel">
          <h2 style={{ fontSize: 15 }}>Stage status</h2>
          <StageStatusTable status={status ?? null} />
        </div>
        <div className="panel">
          <h2 style={{ fontSize: 15 }}>Artifacts</h2>
          <ArtifactsTable artifacts={artifactsResp?.items ?? []} />
        </div>
      </div>

      <div className="panel">
        <h2 style={{ fontSize: 15 }}>Deliveries</h2>
        <DeliveriesTable deliveries={job.deliveries} />
      </div>

      <div className="panel">
        <h2 style={{ fontSize: 15 }}>Event log</h2>
        <EventLog events={events} />
      </div>
    </div>
  );
}
