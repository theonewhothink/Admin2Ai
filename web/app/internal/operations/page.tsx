import type { Metadata } from "next";
import { OperationsView } from "@/components/internal/OperationsView";

export const metadata: Metadata = { title: "Operations" };

export default function OperationsPage() {
  return <OperationsView />;
}
