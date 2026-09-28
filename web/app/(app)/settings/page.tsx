import type { Metadata } from "next";
import { LiveSettings } from "@/components/live/pages";
import { SettingsView } from "@/components/SettingsView";
import { browserEngine, getHome } from "@/lib/api";

export const metadata: Metadata = { title: "Settings" };

export default async function SettingsPage() {
  if (browserEngine) return <LiveSettings />;
  return <SettingsView home={await getHome()} />;
}
