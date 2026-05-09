import { ReactNode } from "react";
import { useAuth } from "@/context/auth";

interface CanProps {
  permission?: string;
  anyOf?: string[];
  fallback?: ReactNode;
  children: ReactNode;
}

/**
 * Conditionally render children if the current user holds the required
 * permission. Super admins always pass.
 *
 *   <Can permission="universities.create"><Button>Add</Button></Can>
 *   <Can anyOf={["staged.approve", "staged.edit"]}>...</Can>
 *
 * Renders `fallback` (default: nothing) when the check fails.
 */
export function Can({ permission, anyOf, fallback = null, children }: CanProps) {
  const { can, canAny } = useAuth();
  const allowed = permission
    ? can(permission)
    : anyOf && anyOf.length > 0
    ? canAny(anyOf)
    : false;
  return <>{allowed ? children : fallback}</>;
}

export function useCan(): {
  can: (key: string) => boolean;
  canAny: (keys: string[]) => boolean;
} {
  const { can, canAny } = useAuth();
  return { can, canAny };
}
