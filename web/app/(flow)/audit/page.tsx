import type { Metadata } from "next";
import { AuditView } from "@/components/flow/AuditView";
import { LiveAudit } from "@/components/live/pages";
import { browserEngine, getAudit } from "@/lib/api";

export const metadata: Metadata = { title: "Free business audit" };

export default async function AuditPage() {
  if (browserEngine) return <LiveAudit />;
  return <AuditView audit={await getAudit()} />;
}
