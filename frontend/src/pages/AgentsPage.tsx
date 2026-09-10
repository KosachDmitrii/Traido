import { ScanFunnelCard } from "@/components/desk/ScanFunnelCard";
import { useDesk } from "@/context/DeskContext";
export function AgentsPage() {
  const { desk } = useDesk();
  return <><h1>ORB · Отбор и контроль</h1><ScanFunnelCard />
    <section className="panel" style={{padding:24,marginTop:20}}><h2>Последние события</h2>
      {(desk?.activity.events ?? []).slice(0,40).map((event,i)=><p key={`${event.ts}-${i}`}>
        <time>{new Date(event.ts).toLocaleTimeString()}</time>{" · "}<b>{event.symbol ?? event.agent}</b>{" · "}{event.message}
      </p>)}
    </section></>;
}
