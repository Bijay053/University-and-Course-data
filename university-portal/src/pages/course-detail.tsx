import { useRoute } from "wouter";
import { useGetCourse, getGetCourseQueryKey } from "@workspace/api-client-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

export default function CourseDetail() {
  const [, params] = useRoute("/courses/:id");
  const id = params?.id ? parseInt(params.id) : 0;
  
  const { data: course, isLoading } = useGetCourse(id, { 
    query: { enabled: !!id, queryKey: getGetCourseQueryKey(id) } 
  });

  if (isLoading) return <div>Loading...</div>;
  if (!course) return <div>Course not found</div>;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">{course.name}</h1>
        <p className="text-muted-foreground">{course.universityName}</p>
      </div>

      <Tabs defaultValue="overview">
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="intakes">Intakes</TabsTrigger>
          <TabsTrigger value="fees">Fees</TabsTrigger>
          <TabsTrigger value="requirements">Requirements</TabsTrigger>
        </TabsList>
        <TabsContent value="overview" className="mt-4">
          <Card>
            <CardHeader><CardTitle>Details</CardTitle></CardHeader>
            <CardContent>
              <dl className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div><dt className="text-sm text-muted-foreground">Degree Level</dt><dd>{course.degreeLevel || "-"}</dd></div>
                <div><dt className="text-sm text-muted-foreground">Study Mode</dt><dd>{course.studyMode || "-"}</dd></div>
                <div><dt className="text-sm text-muted-foreground">Duration</dt><dd>{course.duration ? `${course.duration} ${course.durationTerm}` : "-"}</dd></div>
              </dl>
            </CardContent>
          </Card>
        </TabsContent>
        <TabsContent value="intakes">
          <Card><CardContent className="pt-6">Intakes list</CardContent></Card>
        </TabsContent>
        <TabsContent value="fees">
          <Card><CardContent className="pt-6">Fees list</CardContent></Card>
        </TabsContent>
        <TabsContent value="requirements">
          <Card><CardContent className="pt-6">Requirements list</CardContent></Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}
