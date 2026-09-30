import type { Metadata } from "next";
import { ReadinessView } from "@/components/internal/ReadinessView";

export const metadata: Metadata = { title: "Readiness" };

export default function ReadinessPage() {
  return <ReadinessView />;
}
