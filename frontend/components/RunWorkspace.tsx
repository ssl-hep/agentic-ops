"use client";

import { useState, type ComponentProps } from "react";
import { DatasetAgentPanel, type ActiveSelection } from "@/components/DatasetAgentPanel";
import { RunForm } from "@/components/RunForm";

type AgentActions = Pick<
  ComponentProps<typeof DatasetAgentPanel>,
  "createSelection" | "sendSelectionMessage" | "getSelection"
>;

/** The run form beside the dataset agent, sharing which agent selection (if any) the run uses. */
export function RunWorkspace({
  agent,
  ...form
}: Omit<ComponentProps<typeof RunForm>, "selection"> & { agent: AgentActions }) {
  const [selection, setSelection] = useState<ActiveSelection | null>(null);
  return (
    <div className="run-layout">
      <RunForm {...form} selection={selection} />
      <DatasetAgentPanel
        folder={form.analysis.folder}
        sha={form.resolved.sha}
        active={selection}
        onActiveChange={setSelection}
        {...agent}
      />
    </div>
  );
}
