import { Switch, Route, Router as WouterRouter } from "wouter";
import { aroundCourseEditNavigation } from "@/lib/course-edit-navigation-guard";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Toaster } from "@/components/ui/toaster";
import { TooltipProvider } from "@/components/ui/tooltip";
import NotFound from "@/pages/not-found";
import { Layout } from "@/components/layout";
import { AuthProvider } from "@/context/auth";
import { AuthGuard } from "@/components/auth-guard";

import Dashboard from "@/pages/dashboard";
import Universities from "@/pages/universities";
import UniversitiesBulkImport from "@/pages/universities-bulk-import";
import UniversityDetail from "@/pages/university-detail";
import Scraping from "@/pages/scraping";
import Bulk from "@/pages/bulk";
import Backup from "@/pages/backup";
import SettingsAcademicLevels from "@/pages/settings-academic-levels";
import SettingsAcronyms from "@/pages/settings-acronyms";
import SettingsScraperConfigs from "@/pages/settings-scraper-configs";
import SearchPage from "@/pages/search";
import ComparePage from "@/pages/compare";
import CourseDetail from "@/pages/course-detail";
import Login from "@/pages/login";
import DegreesPage from "@/pages/degrees";
import PublicUniversityPage from "@/pages/public-university";
import PublicCoursePage from "@/pages/public-course";
import ForgotPassword from "@/pages/forgot-password";
import ResetPassword from "@/pages/reset-password";
import UsersPage from "@/pages/users";
import PerformancePage from "@/pages/performance";
import MonitoringPage from "@/pages/monitoring";
import PublishingPage from "@/pages/publishing";
import ScrapeAgentPage from "@/pages/scrape-agent";
import RecipeEditorPage from "@/pages/recipe-editor";
import CertDashboardPage from "@/pages/cert-dashboard";

const queryClient = new QueryClient();

function Router() {
  return (
    <Switch>
      <Route path="/toolkit" component={DegreesPage} />
      <Route path="/toolkit/universities/:id" component={PublicUniversityPage} />
      <Route path="/toolkit/courses/:id" component={PublicCoursePage} />
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
              <Route path="/settings/scraper-configs" component={SettingsScraperConfigs} />
              <Route path="/search" component={SearchPage} />
              <Route path="/compare" component={ComparePage} />
              <Route path="/courses/:id" component={CourseDetail} />
              <Route path="/users" component={UsersPage} />
              <Route path="/performance" component={PerformancePage} />
              <Route path="/monitoring" component={MonitoringPage} />
              <Route path="/publishing" component={PublishingPage} />
              <Route path="/cert-dashboard" component={CertDashboardPage} />
              <Route path="/universities/:id/scrape-agent" component={ScrapeAgentPage} />
              <Route path="/universities/:id/recipe" component={RecipeEditorPage} />
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
        <WouterRouter base={import.meta.env.BASE_URL.replace(/\/$/, "")} aroundNav={aroundCourseEditNavigation}>
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
