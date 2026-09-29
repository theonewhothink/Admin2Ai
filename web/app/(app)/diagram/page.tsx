import type { Metadata } from "next";
import { DiagramView } from "@/components/diagram/DiagramView";
import { DiagramLead, LiveDiagram } from "@/components/live/pages";
import { browserEngine, getPipeline } from "@/lib/api";

export const metadata: Metadata = { title: "Diagram" };

function Head() {
  return (
    <header className="page-head">
      <h1 className="h1">Diagram</h1>
      <p className="lead">What I am doing, step by step. Every item moves left to right and only closes with proof.</p>
    </header>
  );
}

export default async function DiagramPage() {
  if (browserEngine) {
    return (
      <div className="container page">
        <Head />
        <LiveDiagram />
      </div>
    );
  }
  const data = await getPipeline();
  return (
    <div className="container page">
      <Head />
      {data ? (
        <>
          <DiagramLead data={data} />
          <DiagramView data={data} />
        </>
      ) : (
        <p className="card card-pad meta">The diagram shows the engine’s real work. Connect the backend or open the live site to see it.</p>
      )}
    </div>
  );
}
