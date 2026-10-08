import type { Metadata } from "next";
import { CommandCenter } from "@/components/internal/CommandCenter";

export const metadata: Metadata = { title: "Command Center" };

export default function CommandCenterPage() {
  return <CommandCenter />;
}
