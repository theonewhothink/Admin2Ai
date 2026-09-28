import type { Metadata } from "next";
import { ActivityView } from "@/components/ActivityView";
import { LiveActivity } from "@/components/live/pages";
import { browserEngine, getActivity } from "@/lib/api";

export const metadata: Metadata = { title: "Activity" };

export default async function ActivityPage() {
  if (browserEngine) return <LiveActivity />;
  return <ActivityView feed={await getActivity()} />;
}
