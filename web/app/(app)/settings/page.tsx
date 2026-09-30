import type { Metadata } from "next";
import { LiveSettings } from "@/components/live/pages";
import { SettingsView } from "@/components/SettingsView";
import { clientRendered, getHome } from "@/lib/api";

export const metadata: Metadata = { title: "Settings" };

export default async function SettingsPage() {
  if (clientRendered) return <LiveSettings />;
  return <SettingsView home={await getHome()} />;
}
