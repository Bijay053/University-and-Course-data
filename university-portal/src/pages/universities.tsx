import { useState } from "react";
import { Link } from "wouter";
import { useListUniversities, useCreateUniversity, getListUniversitiesQueryKey } from "@workspace/api-client-react";
import { useQueryClient } from "@tanstack/react-query";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from "@/components/ui/form";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import * as z from "zod";
import { Plus, Search, Building2, MapPin, Globe, ChevronRight } from "lucide-react";

const formSchema = z.object({
  name: z.string().min(1, "Name is required"),
  country: z.string().min(1, "Country is required"),
  city: z.string().min(1, "City is required"),
  website: z.string().url().optional().or(z.literal("")),
});

export default function Universities() {
  const [search, setSearch] = useState("");
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();

  const { data, isLoading } = useListUniversities({ search: search || undefined });
  const createUniversity = useCreateUniversity();

  const form = useForm<z.infer<typeof formSchema>>({
    resolver: zodResolver(formSchema),
    defaultValues: { name: "", country: "", city: "", website: "" },
  });

  const onSubmit = (values: z.infer<typeof formSchema>) => {
    createUniversity.mutate({ data: { ...values, website: values.website || null } }, {
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: getListUniversitiesQueryKey() });
        setOpen(false);
        form.reset();
      },
    });
  };

  const universities = data?.data ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-col sm:flex-row justify-between gap-3 items-start sm:items-center">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Universities</h1>
          <p className="text-muted-foreground text-sm">Manage partner universities and institutions.</p>
        </div>
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogTrigger asChild>
            <Button size="sm"><Plus className="mr-2 h-4 w-4" /> Add University</Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Add New University</DialogTitle>
            </DialogHeader>
            <Form {...form}>
              <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
                <FormField control={form.control} name="name" render={({ field }) => (
                  <FormItem><FormLabel>Name</FormLabel><FormControl><Input {...field} /></FormControl><FormMessage /></FormItem>
                )} />
                <FormField control={form.control} name="country" render={({ field }) => (
                  <FormItem><FormLabel>Country</FormLabel><FormControl><Input {...field} /></FormControl><FormMessage /></FormItem>
                )} />
                <FormField control={form.control} name="city" render={({ field }) => (
                  <FormItem><FormLabel>City</FormLabel><FormControl><Input {...field} /></FormControl><FormMessage /></FormItem>
                )} />
                <FormField control={form.control} name="website" render={({ field }) => (
                  <FormItem><FormLabel>Website</FormLabel><FormControl><Input {...field} /></FormControl><FormMessage /></FormItem>
                )} />
                <Button type="submit" className="w-full" disabled={createUniversity.isPending}>
                  {createUniversity.isPending ? "Creating..." : "Create"}
                </Button>
              </form>
            </Form>
          </DialogContent>
        </Dialog>
      </div>

      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center gap-2">
            <Search className="w-4 h-4 text-muted-foreground flex-shrink-0" />
            <Input
              placeholder="Search universities..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="border-0 focus-visible:ring-0 px-0 h-8"
            />
          </div>
        </CardHeader>

        <CardContent className="p-0">
          {isLoading ? (
            <div className="text-center py-10 text-muted-foreground text-sm">Loading...</div>
          ) : universities.length === 0 ? (
            <div className="text-center py-10 text-muted-foreground text-sm">No universities found</div>
          ) : (
            <>
              {/* Mobile card list */}
              <div className="md:hidden divide-y">
                {universities.map((uni) => (
                  <Link key={uni.id} href={`/universities/${uni.id}`}>
                    <div className="flex items-center gap-3 px-4 py-3 hover:bg-muted/50 active:bg-muted transition-colors">
                      <div className="flex-shrink-0 w-9 h-9 rounded-full bg-primary/10 flex items-center justify-center">
                        <Building2 className="h-4 w-4 text-primary" />
                      </div>
                      <div className="flex-1 min-w-0">
                        <p className="font-medium text-sm truncate">{uni.name}</p>
                        <div className="flex items-center gap-1 text-xs text-muted-foreground mt-0.5">
                          <MapPin className="h-3 w-3 flex-shrink-0" />
                          <span className="truncate">{uni.city}, {uni.country}</span>
                        </div>
                        {uni.website && (
                          <div className="flex items-center gap-1 text-xs text-blue-600 mt-0.5">
                            <Globe className="h-3 w-3 flex-shrink-0" />
                            <span className="truncate">{uni.website.replace(/^https?:\/\//, "")}</span>
                          </div>
                        )}
                      </div>
                      <ChevronRight className="h-4 w-4 text-muted-foreground flex-shrink-0" />
                    </div>
                  </Link>
                ))}
              </div>

              {/* Desktop table */}
              <div className="hidden md:block">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Name</TableHead>
                      <TableHead>Location</TableHead>
                      <TableHead>Website</TableHead>
                      <TableHead className="text-right">Actions</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {universities.map((uni) => (
                      <TableRow key={uni.id}>
                        <TableCell className="font-medium">
                          <Link href={`/universities/${uni.id}`} className="hover:underline flex items-center gap-2">
                            <Building2 className="h-4 w-4 text-muted-foreground" />
                            {uni.name}
                          </Link>
                        </TableCell>
                        <TableCell>{uni.city}, {uni.country}</TableCell>
                        <TableCell>
                          {uni.website
                            ? <a href={uni.website} target="_blank" rel="noreferrer" className="text-blue-600 hover:underline">{uni.website}</a>
                            : "-"}
                        </TableCell>
                        <TableCell className="text-right">
                          <Link href={`/universities/${uni.id}`}>
                            <Button variant="ghost" size="sm">View</Button>
                          </Link>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
