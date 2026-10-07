import { fmt } from "@/lib/format";

/** Compact campaign coverage: one chip per campaign, data years folded into one chip. */
export function CampaignChips({ campaigns }: { campaigns: Record<string, number> }) {
  const entries = Object.entries(campaigns);
  const mc = entries.filter(([c]) => c.startsWith("mc"));
  const data = entries.filter(([c]) => c.startsWith("data")).map(([c]) => c.slice(4));
  const unknown = campaigns.unknown;
  if (entries.length === 0) return <span className="faint">none</span>;
  return (
    <div className="chips">
      {mc.map(([c, n]) => (
        <span key={c} className="chip" title={`${fmt(n)} datasets`}>
          {c}
        </span>
      ))}
      {data.length > 0 && (
        <span className="chip chip-data" title="data years">
          data{data.join(",")}
        </span>
      )}
      {unknown && (
        <span className="chip chip-warn" title="datasets whose r-tag is not in EWKit's campaign map">
          {fmt(unknown)} unknown
        </span>
      )}
    </div>
  );
}
