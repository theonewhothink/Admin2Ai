import type { Metadata } from "next";
import { PlanView } from "@/components/settings/PlanView";

export const metadata: Metadata = { title: "Plan" };

export default function PlanPage() {
  return <PlanView />;
}
