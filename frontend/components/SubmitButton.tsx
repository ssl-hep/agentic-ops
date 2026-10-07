"use client";

import type { ReactNode } from "react";
import { useFormStatus } from "react-dom";

/** A submit button that shows what is happening while its form's server action runs. */
export function SubmitButton({
  children,
  pendingText = "Working…",
  name,
  value,
  className,
  disabled,
}: {
  children: ReactNode;
  pendingText?: string;
  name?: string;
  value?: string;
  className?: string;
  disabled?: boolean;
}) {
  const { pending, data } = useFormStatus();
  // With several buttons in one form, only the one that was clicked says what is happening
  const clicked = pending && (!name || data?.get(name) === value);
  return (
    <button type="submit" name={name} value={value} className={className} disabled={disabled || pending}>
      {clicked ? pendingText : children}
    </button>
  );
}
