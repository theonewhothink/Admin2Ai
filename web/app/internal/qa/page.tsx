import type { Metadata } from "next";
import { QAView } from "@/components/internal/QAView";

export const metadata: Metadata = { title: "QA" };

export default function QAPage() {
  return <QAView />;
}
