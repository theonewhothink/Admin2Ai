import type { Metadata } from "next";
import { TargetsView } from "@/components/internal/TargetsView";

export const metadata: Metadata = { title: "Targets" };

export default function TargetsPage() {
  return <TargetsView />;
}
