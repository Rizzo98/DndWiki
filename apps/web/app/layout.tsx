import type { Metadata } from "next";
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
      <body className="min-h-screen bg-slate-950 text-slate-100 antialiased">
        <AuthProvider>
          <Nav />
          <div className="mx-auto max-w-6xl px-4 py-8">{children}</div>
        </AuthProvider>
      </body>
    </html>
  );
}
