// Screen 3: Review queue. GET /gates. This is the heart of the product.
// Modeled on Radarr's manual import: show the evidence, offer a small set of
// actions, and record why. Refer to APP-CONTRACT.md section 9 and section
// 13.3.

import { useMemo } from "react";
import { getApiClient } from "../api";
import type { GateDetail } from "../api/types";
import { usePolling } from "../hooks/usePolling";
import { SampleGateItem } from "./gates/SampleGateItem";
import { HomographItemRow } from "./gates/HomographGateItem";
import { QcGateItemRow } from "./gates/QcGateItem";

export function ReviewQueue() {
  const client = useMemo(() => getApiClient(), []);

  const { data, error, refresh } = usePolling(async () => {
    const list = await client.listGates({ limit: 200 });
    const details = await Promise.all(list.items.map((g) => client.getGate(g.id)));
    return details;
  }, 5000);

  const gates: GateDetail[] = data ?? [];
  const sampleGates = gates.filter((g) => g.kind === "sample");
  const homographGates = gates.filter((g) => g.kind === "homograph");
  const qcGates = gates.filter((g) => g.kind === "qc");

  return (
    <div className="stack">
      <h1>Review queue</h1>
      <p className="muted">
        Every book that waits for a person, across the whole library. Sample first, then
        homographs, then QC — the order the pipeline stops in.
      </p>

      {error && (
        <p className="callout callout--warn" role="alert">
          {error}
        </p>
      )}

      {data && gates.length === 0 && (
        <div className="empty-state">Nothing waits for a person right now.</div>
      )}

      {sampleGates.length > 0 && (
        <section className="stack" aria-labelledby="sample-gates-heading">
          <h2 id="sample-gates-heading" style={{ fontSize: 16 }}>
            Sample approval ({sampleGates.length})
          </h2>
          {sampleGates.map((gate) => (
            <SampleGateItem key={gate.id} client={client} gate={gate} onResolved={refresh} />
          ))}
        </section>
      )}

      {homographGates.length > 0 && (
        <section className="stack" aria-labelledby="homograph-gates-heading">
          <h2 id="homograph-gates-heading" style={{ fontSize: 16 }}>
            Homograph review ({homographGates.reduce((n, g) => n + g.review_items.length, 0)})
          </h2>
          {homographGates.map((gate) => (
            <div key={gate.id} className="panel panel--gate">
              <h3 style={{ marginTop: 0 }}>{gate.job_title ?? gate.job_slug ?? gate.job_id}</h3>
              <div className="stack">
                {gate.review_items.map((item) => (
                  <HomographItemRow key={item.id} client={client} item={item} onResolved={refresh} />
                ))}
              </div>
            </div>
          ))}
        </section>
      )}

      {qcGates.length > 0 && (
        <section className="stack" aria-labelledby="qc-gates-heading">
          <h2 id="qc-gates-heading" style={{ fontSize: 16 }}>
            QC review ({qcGates.reduce((n, g) => n + g.review_items.length, 0)})
          </h2>
          {qcGates.map((gate) => (
            <div key={gate.id} className="panel panel--gate">
              <h3 style={{ marginTop: 0 }}>{gate.job_title ?? gate.job_slug ?? gate.job_id}</h3>
              <div className="stack">
                {gate.review_items.map((item) => (
                  <QcGateItemRow key={item.id} client={client} item={item} jobId={gate.job_id} onResolved={refresh} />
                ))}
              </div>
            </div>
          ))}
        </section>
      )}
    </div>
  );
}
