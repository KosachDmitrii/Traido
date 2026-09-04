import { AgentsPanel } from "@/components/desk/AgentsPanel";
import { PositionsReview } from "@/components/desk/PositionsReview";
import { SessionDecisionStrip } from "@/components/desk/SessionDecisionStrip";
import { StatsRow } from "@/components/desk/StatsRow";
import { useDesk } from "@/context/DeskContext";

export function DeskPage() {
  const { desk } = useDesk();
  return (
    <>
      <StatsRow desk={desk} />
      <SessionDecisionStrip desk={desk} />
      <PositionsReview desk={desk} />
      <AgentsPanel desk={desk} />
    </>
  );
}
