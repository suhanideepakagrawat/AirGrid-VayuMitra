import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  Outlet,
  Link,
  createRootRouteWithContext,
  useRouter,
  HeadContent,
  Scripts,
} from "@tanstack/react-router";
import { useEffect, type ReactNode } from "react";

import appCss from "../styles.css?url";
import { reportLovableError } from "../lib/lovable-error-reporting";

function NotFoundComponent() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <div className="max-w-md text-center">
        <div className="chip mx-auto mb-6">404 · No signal</div>
        <h1 className="font-display text-4xl text-foreground">Location not indexed</h1>
        <p className="mono mt-3 text-sm text-muted-foreground">
          The route you requested is not part of the current pipeline.
        </p>
        <div className="mt-6">
          <Link
            to="/"
            className="mono inline-flex items-center justify-center border border-accent px-4 py-2 text-xs text-accent transition-colors hover:bg-accent hover:text-accent-foreground"
          >
            Return to overview
          </Link>
        </div>
      </div>
    </div>
  );
}

function ErrorComponent({ error, reset }: { error: Error; reset: () => void }) {
  console.error(error);
  const router = useRouter();
  useEffect(() => {
    reportLovableError(error, { boundary: "tanstack_root_error_component" });
  }, [error]);

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <div className="max-w-md text-center">
        <div className="chip mx-auto mb-6" style={{ borderColor: "var(--source-burning)", color: "var(--source-burning)" }}>Pipeline error</div>
        <h1 className="font-display text-2xl text-foreground">This view failed to load</h1>
        <p className="mono mt-3 text-sm text-muted-foreground">
          A component in the request chain did not respond. Retry or return to the overview.
        </p>
        <div className="mt-6 flex flex-wrap justify-center gap-2">
          <button
            onClick={() => { router.invalidate(); reset(); }}
            className="mono border border-accent bg-accent px-4 py-2 text-xs text-accent-foreground"
          >
            Retry
          </button>
          <a href="/" className="mono border border-border px-4 py-2 text-xs text-foreground hover:border-accent-dim">
            Overview
          </a>
        </div>
      </div>
    </div>
  );
}

export const Route = createRootRouteWithContext<{ queryClient: QueryClient }>()({
  head: () => ({
    meta: [
      { charSet: "utf-8" },
      { name: "viewport", content: "width=device-width, initial-scale=1" },
      { title: "AirGrid NCR - Urban Air Quality Intelligence" },
      { name: "description", content: "Hyperlocal air quality forecasts for Delhi-NCR with source attribution, wind-corridor evidence, ward-level enforcement priorities, and a multilingual citizen advisory." },
      { name: "author", content: "PS5 Urban Air Quality Intelligence team" },
      { property: "og:title", content: "AirGrid NCR - Urban Air Quality Intelligence" },
      { property: "og:description", content: "Which source is polluting you right now, what the air will be in 24–72 hours, where to send inspectors, and what you personally should do - in your language." },
      { property: "og:type", content: "website" },
      { name: "twitter:card", content: "summary" },
      { name: "twitter:title", content: "AirGrid NCR - Urban Air Quality Intelligence" },
      { name: "twitter:description", content: "Forecast · Attribution · Enforcement · Citizen advisory. Delhi-NCR, ward by ward." },
    ],
    links: [
      { rel: "stylesheet", href: appCss },
      // One system sans family (DESIGN.md One Family Rule) - no webfonts to load.
    ],
  }),
  shellComponent: RootShell,
  component: RootComponent,
  notFoundComponent: NotFoundComponent,
  errorComponent: ErrorComponent,
});

function RootShell({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <head>
        <HeadContent />
      </head>
      <body>
        {children}
        <Scripts />
      </body>
    </html>
  );
}

function RootComponent() {
  const { queryClient } = Route.useRouteContext();
  return (
    <QueryClientProvider client={queryClient}>
      <Outlet />
    </QueryClientProvider>
  );
}
