import { createFileRoute } from "@tanstack/react-router";
import { useEffect } from "react";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "Nexus.ai — AI Assistant for Office Documents & Data" },
      {
        name: "description",
        content:
          "Nexus.ai helps office teams summarize files, draft Word documents, build Excel sheets and query the office database from one AI chat workspace.",
      },
      { property: "og:title", content: "Nexus.ai — AI Assistant for Office Documents & Data" },
      {
        property: "og:description",
        content:
          "Summarize files, draft Word documents, build Excel sheets and query the office database from one AI chat workspace.",
      },
      { property: "og:type", content: "website" },
      { name: "twitter:card", content: "summary_large_image" },
    ],
  }),
  component: Index,
});

// The product itself is a standalone HTML/CSS/JS frontend served from
// /app/index.html so it can be dropped onto a Python backend later.
function Index() {
  useEffect(() => {
    window.location.replace("/app/index.html");
  }, []);

  return (
    <div className="flex min-h-screen items-center justify-center bg-background">
      <a href="/app/index.html" className="text-sm text-muted-foreground underline">
        Open Nexus.ai
      </a>
    </div>
  );
}
