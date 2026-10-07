import type { Metadata } from "next";
import { DeadlinesView } from "@/components/settings/DeadlinesView";

export const metadata: Metadata = { title: "Deadlines" };

export default function DeadlinesPage() {
  return <DeadlinesView />;
}
