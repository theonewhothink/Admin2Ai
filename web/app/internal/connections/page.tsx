import type { Metadata } from "next";
import { ConnectionsView } from "@/components/internal/ConnectionsView";

export const metadata: Metadata = { title: "Connections" };

export default function ConnectionsPage() {
  return <ConnectionsView />;
}
