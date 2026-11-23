import type { Metadata } from "next";
import { Suspense } from "react";

import { AuthFrame } from "@/components/auth-frame";
import { GuestOnly } from "@/components/guest-only";
import { RegisterForm } from "@/components/register-form";
import { readControlPlaneConfig } from "@/lib/control-plane";

export const metadata: Metadata = {
  title: "Register",
  description: "Create an AeroDeck account with email and password.",
};

export default function RegisterPage() {
  const controlPlane = readControlPlaneConfig() !== null;
  return (
    <AuthFrame
      eyebrow="Register"
      title="Create an account"
      description={
        controlPlane
          ? "Create an operator account. Stored by the control plane."
          : "Create an in-memory account for this session. A restart clears it."
      }
    >
      <Suspense fallback={<RegisterForm controlPlane={controlPlane} />}>
        <GuestOnly>
          <RegisterForm controlPlane={controlPlane} />
        </GuestOnly>
      </Suspense>
    </AuthFrame>
  );
}
