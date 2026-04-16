import { useRoute } from "wouter";
import { useGetUniversity, getGetUniversityQueryKey } from "@workspace/api-client-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Building2, MapPin, Globe } from "lucide-react";

export default function UniversityDetail() {
  const [, params] = useRoute("/universities/:id");
  const id = params?.id ? parseInt(params.id) : 0;
  
  const { data: uni, isLoading } = useGetUniversity(id, { 
    query: { enabled: !!id, queryKey: getGetUniversityQueryKey(id) } 
  });

  if (isLoading) return <div>Loading...</div>;
  if (!uni) return <div>University not found</div>;

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-4">
        <div className="h-16 w-16 bg-primary/10 rounded-lg flex items-center justify-center">
          <Building2 className="h-8 w-8 text-primary" />
        </div>
        <div>
          <h1 className="text-2xl font-bold tracking-tight">{uni.name}</h1>
          <div className="flex items-center gap-4 text-muted-foreground mt-1">
            <span className="flex items-center gap-1 text-sm"><MapPin className="h-4 w-4" /> {uni.city}, {uni.country}</span>
            {uni.website && <span className="flex items-center gap-1 text-sm"><Globe className="h-4 w-4" /> <a href={uni.website} target="_blank" rel="noreferrer" className="hover:underline">{uni.website}</a></span>}
          </div>
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Courses</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-muted-foreground">Course listing goes here (filtering by university ID).</p>
        </CardContent>
      </Card>
    </div>
  );
}
