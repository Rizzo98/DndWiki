import type { Metadata } from "next";
// Self-hosted type: a serif display face for titles, a grotesque for the UI.
// Loading the subsets here keeps the app offline-friendly (no Google Fonts CDN).
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/inter/700.css";
import "@fontsource/playfair-display/600.css";
import "@fontsource/playfair-display/700.css";
import "./globals.css";
import { AuthProvider } from "@/lib/auth";
import { Nav } from "@/components/nav";

export const metadata: Metadata = {
  title: "DnD Wiki",
  description: "Campaign wiki generated from your recorded sessions",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      {/* The app background is the parchment content surface; the sidebar is the
          only dark chrome in the frame (Ravenlore design system). */}
      <body className="rl-app min-h-screen antialiased">
        <AuthProvider>
          <Nav />
          <div className="lg:pl-64">
            <main className="mx-auto max-w-6xl px-4 py-8 sm:px-6">{children}</main>
          </div>
        </AuthProvider>
      </body>
    </html>
  );
}
