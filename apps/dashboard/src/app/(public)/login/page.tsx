import type { Metadata } from "next";
import { Suspense } from "react";

import { AuthFrame } from "@/components/auth-frame";
import { GuestOnly } from "@/components/guest-only";
import { LoginForm } from "@/components/login-form";
import { readControlPlaneConfig } from "@/lib/control-plane";

export const metadata: Metadata = {
  title: "Login",
  description: "Sign in to AeroDeck with email and password.",
};

export default function LoginPage() {
  const controlPlane = readControlPlaneConfig() !== null;
  return (
    <AuthFrame
      eyebrow="Login"
      title="Sign in"
      description={
        controlPlane
          ? "Email and password are checked against the control plane."
          : "Email and password, or Continue with Google. A configured OAuth client signs you in with your Google account. Operators follow docs/runbooks/google-sign-in.md."
      }
    >
      <Suspense fallback={<LoginForm controlPlane={controlPlane} />}>
        <GuestOnly>
          <LoginForm controlPlane={controlPlane} />
        </GuestOnly>
      </Suspense>
    </AuthFrame>
  );
}
