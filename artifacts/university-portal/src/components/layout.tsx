import { useState } from "react";
import { Link, useLocation } from "wouter";
import { LayoutDashboard, Building2, GraduationCap, HardDrive, UploadCloud, Menu, X } from "lucide-react";
import { cn } from "@/lib/utils";

const navigation = [
  { name: "Dashboard", href: "/", icon: LayoutDashboard },
  { name: "Universities", href: "/universities", icon: Building2 },
  { name: "Courses", href: "/courses", icon: GraduationCap },
  { name: "Scraping", href: "/scraping", icon: HardDrive },
  { name: "Bulk Upload", href: "/bulk", icon: UploadCloud },
];

function NavLinks({ onNav }: { onNav?: () => void }) {
  const [location] = useLocation();
  return (
    <nav className="space-y-1 px-2">
      {navigation.map((item) => {
        const isActive = location === item.href || (item.href !== "/" && location.startsWith(item.href));
        return (
          <Link
            key={item.name}
            href={item.href}
            onClick={onNav}
            className={cn(
              "flex items-center px-3 py-2.5 text-sm font-medium rounded-md transition-colors",
              isActive
                ? "bg-sidebar-accent text-sidebar-accent-foreground"
                : "text-sidebar-foreground/70 hover:bg-sidebar-accent/50 hover:text-sidebar-foreground"
            )}
          >
            <item.icon
              className={cn(
                "mr-3 flex-shrink-0 h-5 w-5",
                isActive ? "text-sidebar-accent-foreground" : "text-sidebar-foreground/50"
              )}
              aria-hidden="true"
            />
            {item.name}
          </Link>
        );
      })}
    </nav>
  );
}

export function Layout({ children }: { children: React.ReactNode }) {
  const [mobileOpen, setMobileOpen] = useState(false);

  return (
    <div className="min-h-[100dvh] flex w-full bg-muted/40">
      {/* Desktop Sidebar */}
      <div className="w-64 border-r bg-sidebar text-sidebar-foreground hidden md:flex flex-col flex-shrink-0">
        <div className="h-14 flex items-center px-4 font-bold tracking-tight text-lg border-b border-sidebar-border">
          UniAdmin Portal
        </div>
        <div className="flex-1 py-4 overflow-y-auto">
          <NavLinks />
        </div>
      </div>

      {/* Mobile Drawer Overlay */}
      {mobileOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/50 md:hidden"
          onClick={() => setMobileOpen(false)}
        />
      )}

      {/* Mobile Drawer */}
      <div
        className={cn(
          "fixed inset-y-0 left-0 z-50 w-64 bg-sidebar text-sidebar-foreground flex flex-col transform transition-transform duration-200 ease-in-out md:hidden",
          mobileOpen ? "translate-x-0" : "-translate-x-full"
        )}
      >
        <div className="h-14 flex items-center justify-between px-4 font-bold tracking-tight text-lg border-b border-sidebar-border">
          UniAdmin Portal
          <button
            onClick={() => setMobileOpen(false)}
            className="p-1 rounded-md text-sidebar-foreground/70 hover:text-sidebar-foreground"
          >
            <X className="h-5 w-5" />
          </button>
        </div>
        <div className="flex-1 py-4 overflow-y-auto">
          <NavLinks onNav={() => setMobileOpen(false)} />
        </div>
      </div>

      {/* Main content */}
      <div className="flex-1 flex flex-col min-h-0 overflow-hidden">
        <header className="h-14 border-b bg-background flex items-center justify-between px-4 shrink-0">
          <div className="flex items-center gap-3">
            <button
              onClick={() => setMobileOpen(true)}
              className="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted md:hidden"
              aria-label="Open navigation"
            >
              <Menu className="h-5 w-5" />
            </button>
            <div className="font-semibold text-sm text-muted-foreground md:hidden">UniAdmin Portal</div>
          </div>
          <div className="flex items-center gap-4">
            <div className="w-8 h-8 bg-primary rounded-full flex items-center justify-center text-primary-foreground font-semibold text-sm">
              AD
            </div>
          </div>
        </header>

        <main className="flex-1 overflow-y-auto p-4 md:p-6 lg:p-8">
          {children}
        </main>
      </div>
    </div>
  );
}
