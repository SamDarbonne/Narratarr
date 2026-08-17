import { getApiClient } from "../api";
import { usePolling } from "../hooks/usePolling";

// The top-right activity indicator. Shows the runner state and the queue
// depth, the servarr idiom's small "what is this app doing right now" tell.
export function TopBar() {
  const client = getApiClient();
  const { data } = usePolling(() => client.systemStatus(), 5000);

  return (
    <header className="topbar">
      <div className="topbar__spacer" />
      <div className="topbar__activity" aria-live="polite">
        {data ? (
          <>
            <span
              className={
                "topbar__dot" + (data.runner_state === "running" ? " topbar__dot--active" : "")
              }
              aria-hidden="true"
            />
            <span>
              Runner: {data.runner_state}
              {data.queue_depth > 0 ? ` · ${data.queue_depth} queued` : ""}
            </span>
          </>
        ) : (
          <span>Connecting…</span>
        )}
      </div>
    </header>
  );
}
