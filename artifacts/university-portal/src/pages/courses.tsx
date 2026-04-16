import { useState } from "react";
import { Link } from "wouter";
import { useListCourses, getListCoursesQueryKey } from "@workspace/api-client-react";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Plus, Search, GraduationCap } from "lucide-react";

export default function Courses() {
  const [search, setSearch] = useState("");
  
  const { data, isLoading } = useListCourses({ search: search || undefined });

  return (
    <div className="space-y-6">
      <div className="flex flex-col sm:flex-row justify-between gap-4 items-start sm:items-center">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Courses</h1>
          <p className="text-muted-foreground">Manage academic programs across all universities.</p>
        </div>
        <Link href="/courses/new">
          <Button><Plus className="mr-2 h-4 w-4" /> Add Course</Button>
        </Link>
      </div>

      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center space-x-2">
            <Search className="w-4 h-4 text-muted-foreground" />
            <Input 
              placeholder="Search courses..." 
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="max-w-sm border-0 focus-visible:ring-0 px-0 h-8"
            />
          </div>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Course Name</TableHead>
                <TableHead>University</TableHead>
                <TableHead>Level</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {isLoading ? (
                <TableRow><TableCell colSpan={4} className="text-center py-8">Loading...</TableCell></TableRow>
              ) : data?.data?.length === 0 ? (
                <TableRow><TableCell colSpan={4} className="text-center py-8 text-muted-foreground">No courses found</TableCell></TableRow>
              ) : (
                data?.data?.map((course) => (
                  <TableRow key={course.id}>
                    <TableCell className="font-medium">
                      <Link href={`/courses/${course.id}`} className="hover:underline flex items-center gap-2">
                        <GraduationCap className="h-4 w-4 text-muted-foreground" />
                        {course.name}
                      </Link>
                    </TableCell>
                    <TableCell>{course.universityName}</TableCell>
                    <TableCell>{course.degreeLevel || "-"}</TableCell>
                    <TableCell className="text-right">
                      <Link href={`/courses/${course.id}`}>
                        <Button variant="ghost" size="sm">View</Button>
                      </Link>
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
