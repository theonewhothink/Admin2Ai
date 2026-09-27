import type { Metadata } from "next";
import { AskClient } from "@/components/ask/AskClient";
import { askExamples } from "@/lib/data";

export const metadata: Metadata = { title: "Ask" };

export default async function AskPage({
  searchParams,
}: {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
  const { q } = await searchParams;
  const initialQuestion = typeof q === "string" && q.trim() ? q.trim().slice(0, 500) : undefined;
  return (
    <div className="container-narrow page">
      <header className="page-head">
        <h1 className="h1">Ask</h1>
        <p className="lead">Anything about your business. I answer from your email, bank and documents, and show you where.</p>
      </header>
      <AskClient key={initialQuestion ?? "empty"} initialQuestion={initialQuestion} examples={askExamples} />
    </div>
  );
}
