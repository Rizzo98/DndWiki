// Landing page: sign-in gate. Authenticated users land on /campaigns.

"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { LoadingScreen, SignInPrompt, useAuth } from "@/lib/auth";

export default function Home() {
  const { initialized, authenticated, login } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (initialized && authenticated) router.replace("/campaigns");
  }, [initialized, authenticated, router]);

  if (!initialized) return <LoadingScreen />;
  if (authenticated) return <LoadingScreen />;
  return <SignInPrompt onLogin={login} />;
}
