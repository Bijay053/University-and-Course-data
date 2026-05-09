import React, { useEffect } from "react";
import { Switch, Route, Router as WouterRouter, useLocation } from "wouter";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Toaster } from "@/components/ui/toaster";
import { TooltipProvider } from "@/components/ui/tooltip";
import NotFound from "@/pages/not-found";
import { Layout } from "@/components/layout";
import { AuthProvider, useAuth } from "@/context/auth";

import Dashboard from "@/pages/dashboard";
import Universities from "@/pages/universities";
import UniversitiesBulkImport from "@/pages/universities-bulk-import";
import UniversityDetail from "@/pages/university-detail";
import Scraping from "@/pages/scraping";
import Bulk from "@/pages/bulk";
import Backup from "@/pages/backup";
import SettingsAcademicLevels from "@/pages/settings-academic-levels";
import SettingsAcronyms from "@/pages/settings-acronyms";
import SearchPage from "@/pages/search";
import ComparePage from "@/pages/compare";
import CourseDetail from "@/pages/course-detail";
import Login from "@/pages/login";
import ForgotPassword from "@/pages/forgot-password";
import ResetPassword from "@/pages/reset-password";
import UsersPage from "@/pages/users";

const queryClient = new QueryClient();

function AuthGuard({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const [, navigate] = useLocation();

  useEffect(() => {
    if (!loading && !user) {
      navigate("/login");
    }
  }, [loading, user, navigate]);

  if (loading) {
    return (
      <div className="min-h-[100dvh] flex items-center justify-center text-muted-foreground text-sm">
        Loading…
      </div>
    );
  }

  if (!user) {
    return null;
  }

  return <>{children}</>;
}

function Router() {
  return (
    <Switch>
      <Route path="/login" component={Login} />
      <Route path="/forgot-password" component={ForgotPassword} />
      <Route path="/reset-password" component={ResetPassword} />
      <Route>
        <AuthGuard>
          <Layout>
            <Switch>
              <Route path="/" component={Dashboard} />
              <Route path="/universities" component={Universities} />
              <Route path="/universities/bulk-import" component={UniversitiesBulkImport} />
              <Route path="/universities/:id" component={UniversityDetail} />
              <Route path="/scraping" component={Scraping} />
              <Route path="/bulk" component={Bulk} />
              <Route path="/backup" component={Backup} />
              <Route path="/settings/academic-levels" component={SettingsAcademicLevels} />
              <Route path="/settings/acronyms" component={SettingsAcronyms} />
              <Route path="/search" component={SearchPage} />
              <Route path="/compare" component={ComparePage} />
              <Route path="/courses/:id" component={CourseDetail} />
              <Route path="/users" component={UsersPage} />
              <Route component={NotFound} />
            </Switch>
          </Layout>
        </AuthGuard>
      </Route>
    </Switch>
  );
}

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <WouterRouter base={import.meta.env.BASE_URL.replace(/\/$/, "")}>
          <AuthProvider>
            <Router />
          </AuthProvider>
        </WouterRouter>
        <Toaster />
      </TooltipProvider>
    </QueryClientProvider>
  );
}

export default App;
