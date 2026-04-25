import { Link, useLocation } from "wouter";
import { cn } from "@/lib/utils";

const tabs = [
  { label: "Academic Levels", href: "/settings/academic-levels" },
  { label: "Course Name Acronyms", href: "/settings/acronyms" },
];

export function SettingsTabs() {
  const [location] = useLocation();
  return (
    <div className="border-b">
      <nav className="-mb-px flex gap-6" aria-label="Settings sections">
        {tabs.map((t) => {
          const active = location === t.href;
          return (
            <Link
              key={t.href}
              href={t.href}
              className={cn(
                "border-b-2 py-2 text-sm font-medium transition-colors",
                active
                  ? "border-primary text-foreground"
                  : "border-transparent text-muted-foreground hover:border-muted-foreground/40 hover:text-foreground"
              )}
            >
              {t.label}
            </Link>
          );
        })}
      </nav>
    </div>
  );
}
