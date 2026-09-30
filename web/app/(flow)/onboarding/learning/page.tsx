import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { LearningFlow } from "@/components/flow/LearningFlow";
import { learningCounters, learningUnderstood, oneTapQuestions } from "@/lib/data";
import { production } from "@/lib/mode";

export const metadata: Metadata = { title: "Learning your business" };

export default function LearningPage() {
  // The counters here are sample figures: real owners go Home, where the real work shows.
  if (production) redirect("/");
  return (
    <div className="container">
      <LearningFlow counters={learningCounters} understood={learningUnderstood} questions={oneTapQuestions} />
    </div>
  );
}
