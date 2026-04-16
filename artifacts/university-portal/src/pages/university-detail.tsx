import { useState } from "react";
import { useRoute, Link } from "wouter";
import { useGetUniversity, getGetUniversityQueryKey, useListCourses } from "@workspace/api-client-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Building2, MapPin, Globe, GraduationCap, Search, ChevronLeft, ChevronRight } from "lucide-react";

const DEGREE_COLORS: Record<string, string> = {
  "Bachelor": "bg-blue-100 text-blue-800",
  "Master": "bg-purple-100 text-purple-800",
  "PhD": "bg-red-100 text-red-800",
  "Certificate & Diploma": "bg-green-100 text-green-800",
  "Graduate Certificate & Diploma": "bg-teal-100 text-teal-800",
  "Associate Degree or Equivalent": "bg-orange-100 text-orange-800",
};

export default function UniversityDetail() {
  const [, params] = useRoute("/universities/:id");
  const id = params?.id ? parseInt(params.id) : 0;
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const limit = 20;

  const { data: uni, isLoading: uniLoading } = useGetUniversity(id, {
    query: { enabled: !!id, queryKey: getGetUniversityQueryKey(id) }
  });

  const { data: coursesData, isLoading: coursesLoading } = useListCourses(
    { universityId: id, search: search || undefined, page, limit },
    { query: { enabled: !!id } }
  );

  const courses = coursesData?.data ?? [];
  const total = coursesData?.total ?? 0;
  const totalPages = Math.ceil(total / limit);

  if (uniLoading) return <div className="py-12 text-center text-muted-foreground">Loading...</div>;
  if (!uni) return <div className="py-12 text-center text-muted-foreground">University not found</div>;

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-4">
        <div className="h-16 w-16 bg-primary/10 rounded-lg flex items-center justify-center flex-shrink-0">
          <Building2 className="h-8 w-8 text-primary" />
        </div>
        <div className="min-w-0">
          <h1 className="text-2xl font-bold tracking-tight">{uni.name}</h1>
          <div className="flex flex-wrap items-center gap-4 text-muted-foreground mt-1">
            <span className="flex items-center gap-1 text-sm">
              <MapPin className="h-4 w-4 flex-shrink-0" /> {uni.city}, {uni.country}
            </span>
            {uni.website && (
              <span className="flex items-center gap-1 text-sm">
                <Globe className="h-4 w-4 flex-shrink-0" />
                <a href={uni.website} target="_blank" rel="noreferrer" className="hover:underline truncate">
                  {uni.website}
                </a>
              </span>
            )}
          </div>
          {uni.description && (
            <p className="text-sm text-muted-foreground mt-2 max-w-2xl">{uni.description}</p>
          )}
        </div>
      </div>

      <Card>
        <CardHeader className="pb-3">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
            <CardTitle className="text-base font-semibold">
              Courses
              {total > 0 && (
                <span className="ml-2 text-sm font-normal text-muted-foreground">({total} total)</span>
              )}
            </CardTitle>
            <div className="flex items-center gap-2 max-w-xs w-full sm:w-auto">
              <Search className="h-4 w-4 text-muted-foreground flex-shrink-0" />
              <Input
                placeholder="Search courses..."
                value={search}
                onChange={(e) => { setSearch(e.target.value); setPage(1); }}
                className="border-0 focus-visible:ring-0 px-0 h-8"
              />
            </div>
          </div>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead className="pl-6">Course Name</TableHead>
                <TableHead>Category</TableHead>
                <TableHead>Level</TableHead>
                <TableHead>Study Mode</TableHead>
                <TableHead>Duration</TableHead>
                <TableHead className="text-right pr-6">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {coursesLoading ? (
                <TableRow>
                  <TableCell colSpan={6} className="text-center py-10 text-muted-foreground">
                    Loading courses...
                  </TableCell>
                </TableRow>
              ) : courses.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={6} className="text-center py-10 text-muted-foreground">
                    No courses found
                  </TableCell>
                </TableRow>
              ) : (
                courses.map((course) => (
                  <TableRow key={course.id} className="group">
                    <TableCell className="pl-6 font-medium">
                      <Link
                        href={`/courses/${course.id}`}
                        className="flex items-center gap-2 hover:underline group-hover:text-primary transition-colors"
                      >
                        <GraduationCap className="h-4 w-4 text-muted-foreground flex-shrink-0" />
                        <span className="line-clamp-2">{course.name}</span>
                      </Link>
                    </TableCell>
                    <TableCell>
                      <span className="text-sm text-muted-foreground">{course.category || "-"}</span>
                    </TableCell>
                    <TableCell>
                      {course.degreeLevel ? (
                        <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${DEGREE_COLORS[course.degreeLevel] ?? "bg-gray-100 text-gray-700"}`}>
                          {course.degreeLevel}
                        </span>
                      ) : "-"}
                    </TableCell>
                    <TableCell>
                      <span className="text-sm">{course.studyMode || "-"}</span>
                    </TableCell>
                    <TableCell>
                      <span className="text-sm">
                        {course.duration ? `${course.duration} ${course.durationTerm ?? ""}`.trim() : "-"}
                      </span>
                    </TableCell>
                    <TableCell className="text-right pr-6">
                      <Link href={`/courses/${course.id}`}>
                        <Button variant="ghost" size="sm">View</Button>
                      </Link>
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>

          {totalPages > 1 && (
            <div className="flex items-center justify-between px-6 py-4 border-t">
              <p className="text-sm text-muted-foreground">
                Showing {(page - 1) * limit + 1}–{Math.min(page * limit, total)} of {total} courses
              </p>
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  disabled={page === 1}
                >
                  <ChevronLeft className="h-4 w-4" />
                </Button>
                <span className="text-sm">
                  Page {page} of {totalPages}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                  disabled={page === totalPages}
                >
                  <ChevronRight className="h-4 w-4" />
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
