import { OpportunityRail } from "@/components/desk/OpportunityRail";
import { useDesk } from "@/context/DeskContext";
export function OpportunitiesPage() {
  const { desk, showFlash, refreshAll } = useDesk();
  return <OpportunityRail layout="page" desk={desk} onFlash={showFlash} onRefresh={refreshAll} />;
}
