import { Switch, Route, Router as WouterRouter } from "wouter";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Toaster } from "@/components/ui/toaster";
import { TooltipProvider } from "@/components/ui/tooltip";
import NotFound from "@/pages/not-found";
import { Layout } from "@/components/layout";

import Dashboard from "@/pages/dashboard";
import Universities from "@/pages/universities";
import UniversityDetail from "@/pages/university-detail";
import Scraping from "@/pages/scraping";
import Bulk from "@/pages/bulk";
import Backup from "@/pages/backup";
import SettingsAcademicLevels from "@/pages/settings-academic-levels";

const queryClient = new QueryClient();

function Router() {
  return (
    <Layout>
      <Switch>
        <Route path="/" component={Dashboard} />
        <Route path="/universities" component={Universities} />
        <Route path="/universities/:id" component={UniversityDetail} />
        <Route path="/scraping" component={Scraping} />
        <Route path="/bulk" component={Bulk} />
        <Route path="/backup" component={Backup} />
        <Route path="/settings/academic-levels" component={SettingsAcademicLevels} />
        <Route component={NotFound} />
      </Switch>
    </Layout>
  );
}

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <WouterRouter base={import.meta.env.BASE_URL.replace(/\/$/, "")}>
          <Router />
        </WouterRouter>
        <Toaster />
      </TooltipProvider>
    </QueryClientProvider>
  );
}

export default App;
