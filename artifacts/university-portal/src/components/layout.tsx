import { useState } from "react";
import { Link, useLocation } from "wouter";
import { LayoutDashboard, Building2, HardDrive, UploadCloud, Menu, X, Shield, Settings, Search as SearchIcon, LogOut } from "lucide-react";
import { cn } from "@/lib/utils";
import brandLogo from "@assets/image_1776917782083.png";
import { useAuth } from "@/context/auth";

const navigation = [
  { name: "Dashboard", href: "/", icon: LayoutDashboard },
  { name: "Course Search", href: "/search", icon: SearchIcon },
  { name: "Universities", href: "/universities", icon: Building2 },
  { name: "Scraping", href: "/scraping", icon: HardDrive },
  { name: "Bulk Upload", href: "/bulk", icon: UploadCloud },
  { name: "Data Backup", href: "/backup", icon: Shield },
  { name: "Settings", href: "/settings/academic-levels", icon: Settings },
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
  const { user, logout } = useAuth();

  const initials = user?.name
    ? user.name.split(" ").map((w) => w[0]).join("").slice(0, 2).toUpperCase()
    : "AD";

  return (
    <div className="min-h-[100dvh] flex w-full bg-muted/40">
      {/* Desktop Sidebar */}
      <div className="w-64 border-r bg-sidebar text-sidebar-foreground hidden md:flex flex-col flex-shrink-0">
        <div className="h-14 flex items-center gap-2 px-4 font-bold tracking-tight border-b border-sidebar-border">
          <img src={brandLogo} alt="Study Info Centre" className="h-8 w-auto" />
          <span className="text-sm leading-tight">Study Info Centre</span>
        </div>
        <div className="flex-1 py-4 overflow-y-auto">
          <NavLinks />
        </div>
        {user && (
          <div className="border-t border-sidebar-border px-3 py-3 flex items-center gap-2">
            <div className="w-7 h-7 bg-primary rounded-full flex items-center justify-center text-primary-foreground font-semibold text-xs flex-shrink-0">
              {initials}
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-xs font-medium text-sidebar-foreground truncate">{user.name}</p>
              <p className="text-xs text-sidebar-foreground/50 truncate">{user.email}</p>
            </div>
            <button
              onClick={logout}
              title="Sign out"
              className="p-1 rounded text-sidebar-foreground/50 hover:text-sidebar-foreground hover:bg-sidebar-accent/50 transition-colors"
            >
              <LogOut className="h-4 w-4" />
            </button>
          </div>
        )}
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
        <div className="h-14 flex items-center justify-between px-4 font-bold tracking-tight border-b border-sidebar-border">
          <div className="flex items-center gap-2">
            <img src={brandLogo} alt="Study Info Centre" className="h-8 w-auto" />
            <span className="text-sm leading-tight">Study Info Centre</span>
          </div>
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
        {user && (
          <div className="border-t border-sidebar-border px-3 py-3 flex items-center gap-2">
            <div className="w-7 h-7 bg-primary rounded-full flex items-center justify-center text-primary-foreground font-semibold text-xs flex-shrink-0">
              {initials}
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-xs font-medium text-sidebar-foreground truncate">{user.name}</p>
            </div>
            <button
              onClick={logout}
              title="Sign out"
              className="p-1 rounded text-sidebar-foreground/50 hover:text-sidebar-foreground hover:bg-sidebar-accent/50 transition-colors"
            >
              <LogOut className="h-4 w-4" />
            </button>
          </div>
        )}
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
            <div className="flex items-center gap-2 md:hidden">
              <img src={brandLogo} alt="Study Info Centre" className="h-7 w-auto" />
              <div className="font-semibold text-sm text-muted-foreground">Study Info Centre</div>
            </div>
          </div>
          <div className="flex items-center gap-3">
            {user && (
              <span className="text-sm text-muted-foreground hidden sm:inline">{user.name}</span>
            )}
            <div className="w-8 h-8 bg-primary rounded-full flex items-center justify-center text-primary-foreground font-semibold text-sm">
              {initials}
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
