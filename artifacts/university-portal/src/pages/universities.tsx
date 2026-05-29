import { useState, useMemo } from "react";
import { Link, useLocation } from "wouter";
import { useListUniversities, useCreateUniversity, getListUniversitiesQueryKey } from "@workspace/api-client-react";
import { useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger, DialogFooter } from "@/components/ui/dialog";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from "@/components/ui/form";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import * as z from "zod";
import { Plus, Search, Globe, Building2, Trash2, Pencil, MoreHorizontal, ExternalLink, BookOpen, Star, Upload, ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight } from "lucide-react";
import { Can, useCan } from "@/components/can";
import { useToast } from "@/hooks/use-toast";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

const formSchema = z.object({
  name: z.string().min(1, "Name is required"),
  country: z.string()
    .min(2, "Country is required")
    .refine((v) => v.toLowerCase() !== "unknown", "Country must be specified (cannot be 'Unknown')"),
  city: z.string()
    .min(2, "City is required")
    .refine((v) => v.toLowerCase() !== "unknown", "City must be specified (cannot be 'Unknown')"),
  website: z.string().url().optional().or(z.literal("")),
});

const COUNTRY_FLAGS: Record<string, string> = {
  Australia: "🇦🇺", "United Kingdom": "🇬🇧", UK: "🇬🇧", USA: "🇺🇸",
  "United States": "🇺🇸", Canada: "🇨🇦", "New Zealand": "🇳🇿",
  Germany: "🇩🇪", France: "🇫🇷", India: "🇮🇳", China: "🇨🇳",
  Japan: "🇯🇵", Singapore: "🇸🇬", Malaysia: "🇲🇾",
};

const PAGE_SIZES = [10, 25, 50, 100];

export default function Universities() {
  const [search, setSearch] = useState("");
  const [open, setOpen] = useState(false);
  const { can } = useCan();
  const canEdit = can("universities.edit");
  const canDelete = can("universities.delete");
  const [deleteId, setDeleteId] = useState<number | null>(null);
  const [deleteName, setDeleteName] = useState("");
  const [deleteLoading, setDeleteLoading] = useState(false);
  const [editId, setEditId] = useState<number | null>(null);
  const [editName, setEditName] = useState("");
  const [editCity, setEditCity] = useState("");
  const [editCountry, setEditCountry] = useState("");
  const [editSaving, setEditSaving] = useState(false);
  const [featuredSavingId, setFeaturedSavingId] = useState<number | null>(null);

  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);

  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [, navigate] = useLocation();

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
        setPage(1);
      },
    });
  };

  const openEdit = (uni: { id: number; name: string; city: string; country: string }) => {
    setEditId(uni.id);
    setEditName(uni.name);
    setEditCity(uni.city === "Unknown" ? "" : uni.city);
    setEditCountry(uni.country === "Unknown" ? "" : uni.country);
  };

  const saveEdit = async () => {
    if (!editId) return;
    setEditSaving(true);
    try {
      const res = await fetch(`${BASE}/api/universities/${editId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: editName, city: editCity || "Unknown", country: editCountry || "Unknown" }),
      });
      if (!res.ok) throw new Error(await res.text());
      toast({ title: "University updated" });
      setEditId(null);
      queryClient.invalidateQueries({ queryKey: getListUniversitiesQueryKey() });
    } catch (err) {
      toast({ title: "Error", description: String(err), variant: "destructive" });
    } finally {
      setEditSaving(false);
    }
  };

  const toggleFeatured = async (uniId: number, current: boolean) => {
    setFeaturedSavingId(uniId);
    try {
      const res = await fetch(`${BASE}/api/universities/${uniId}/featured`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ featured: !current, featuredPriority: !current ? 100 : 0 }),
      });
      if (!res.ok) throw new Error(await res.text());
      toast({ title: !current ? "Marked as Featured" : "Removed from Featured" });
      queryClient.invalidateQueries({ queryKey: getListUniversitiesQueryKey() });
    } catch (err) {
      toast({ title: "Error", description: String(err), variant: "destructive" });
    } finally {
      setFeaturedSavingId(null);
    }
  };

  const confirmDelete = async () => {
    if (!deleteId) return;
    setDeleteLoading(true);
    try {
      const res = await fetch(`${BASE}/api/universities/${deleteId}`, { method: "DELETE" });
      if (!res.ok) throw new Error(await res.text());
      toast({ title: "University deleted" });
      setDeleteId(null);
      queryClient.invalidateQueries({ queryKey: getListUniversitiesQueryKey() });
    } catch (err) {
      toast({ title: "Error", description: String(err), variant: "destructive" });
    } finally {
      setDeleteLoading(false);
    }
  };

  const allUniversities = data?.data ?? [];

  const totalPages = Math.max(1, Math.ceil(allUniversities.length / pageSize));
  const safePage = Math.min(page, totalPages);
  const universities = useMemo(() => {
    const start = (safePage - 1) * pageSize;
    return allUniversities.slice(start, start + pageSize);
  }, [allUniversities, safePage, pageSize]);

  const globalStart = (safePage - 1) * pageSize;

  const handleSearchChange = (v: string) => {
    setSearch(v);
    setPage(1);
  };

  const handlePageSizeChange = (v: string) => {
    setPageSize(Number(v));
    setPage(1);
  };

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex flex-col sm:flex-row justify-between gap-3 items-start sm:items-center">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-gray-900">Universities</h1>
          <p className="text-sm text-gray-500 mt-0.5">Manage partner universities and institutions.</p>
        </div>
        <div className="flex gap-2 shrink-0">
          <Can permission="bulk.import">
            <Link href="/universities/bulk-import">
              <Button variant="outline" size="sm" className="gap-1.5">
                <Upload className="h-4 w-4" /> Bulk Import (CSV)
              </Button>
            </Link>
          </Can>
          <Can permission="universities.create">
            <Dialog open={open} onOpenChange={setOpen}>
              <DialogTrigger asChild>
                <Button size="sm" className="gap-1.5">
                  <Plus className="h-4 w-4" /> Add University
                </Button>
              </DialogTrigger>
              <DialogContent>
                <DialogHeader>
                  <DialogTitle>Add New University</DialogTitle>
                </DialogHeader>
                <Form {...form}>
                  <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4 pt-1">
                    <FormField control={form.control} name="name" render={({ field }) => (
                      <FormItem><FormLabel>Name</FormLabel><FormControl><Input placeholder="e.g. University of Sydney" {...field} /></FormControl><FormMessage /></FormItem>
                    )} />
                    <div className="grid grid-cols-2 gap-3">
                      <FormField control={form.control} name="country" render={({ field }) => (
                        <FormItem><FormLabel>Country</FormLabel><FormControl><Input placeholder="e.g. Australia" {...field} /></FormControl><FormMessage /></FormItem>
                      )} />
                      <FormField control={form.control} name="city" render={({ field }) => (
                        <FormItem><FormLabel>City</FormLabel><FormControl><Input placeholder="e.g. Sydney" {...field} /></FormControl><FormMessage /></FormItem>
                      )} />
                    </div>
                    <FormField control={form.control} name="website" render={({ field }) => (
                      <FormItem><FormLabel>Website</FormLabel><FormControl><Input placeholder="https://..." {...field} /></FormControl><FormMessage /></FormItem>
                    )} />
                    <Button type="submit" className="w-full" disabled={createUniversity.isPending}>
                      {createUniversity.isPending ? "Creating..." : "Create University"}
                    </Button>
                  </form>
                </Form>
              </DialogContent>
            </Dialog>
          </Can>
        </div>
      </div>

      {/* Card */}
      <div className="bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">

        {/* Toolbar */}
        <div className="flex items-center gap-3 px-4 py-3 border-b border-gray-100 bg-gray-50/60">
          <div className="relative flex-1 max-w-xs">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-400 pointer-events-none" />
            <input
              className="w-full pl-8 pr-3 py-1.5 text-sm bg-white border border-gray-200 rounded-lg outline-none focus:border-blue-400 focus:ring-1 focus:ring-blue-100 placeholder:text-gray-400 text-gray-900 transition"
              placeholder="Search universities..."
              value={search}
              onChange={(e) => handleSearchChange(e.target.value)}
            />
          </div>
          {!isLoading && allUniversities.length > 0 && (
            <p className="text-xs text-gray-400 ml-auto shrink-0">
              {allUniversities.length} {allUniversities.length === 1 ? "university" : "universities"}
              {search && ` matching "${search}"`}
            </p>
          )}
        </div>

        {isLoading ? (
          <div className="flex items-center justify-center py-20 text-sm text-gray-400">
            <div className="flex items-center gap-2">
              <div className="w-4 h-4 border-2 border-gray-300 border-t-blue-500 rounded-full animate-spin" />
              Loading universities…
            </div>
          </div>
        ) : allUniversities.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-20 text-gray-400">
            <Building2 className="w-10 h-10 mb-3 opacity-30" />
            <p className="text-sm font-medium">No universities found</p>
            {search && <p className="text-xs mt-1">Try a different search term</p>}
          </div>
        ) : (
          <>
            {/* Table */}
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-100 bg-gray-50/40">
                    <th className="w-10 px-4 py-2.5 text-left text-xs font-semibold text-gray-400 uppercase tracking-wider">#</th>
                    <th className="px-4 py-2.5 text-left text-xs font-semibold text-gray-400 uppercase tracking-wider">Institution</th>
                    <th className="px-4 py-2.5 text-left text-xs font-semibold text-gray-400 uppercase tracking-wider">Location</th>
                    <th className="w-24 px-4 py-2.5 text-center text-xs font-semibold text-gray-400 uppercase tracking-wider">Courses</th>
                    <th className="w-28 px-4 py-2.5 text-center text-xs font-semibold text-gray-400 uppercase tracking-wider">Featured</th>
                    <th className="px-4 py-2.5 text-left text-xs font-semibold text-gray-400 uppercase tracking-wider">Website</th>
                    <th className="w-28 px-4 py-2.5 text-right text-xs font-semibold text-gray-400 uppercase tracking-wider">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-50">
                  {universities.map((uni, idx) => {
                    const flag = COUNTRY_FLAGS[uni.country] ?? "🏫";
                    const isUnknown = uni.city === "Unknown" || uni.country === "Unknown";
                    const isFeatured = !!(uni as { featured?: boolean }).featured;

                    return (
                      <tr key={uni.id} className="group hover:bg-blue-50/30 transition-colors">

                        {/* Row number */}
                        <td className="px-4 py-3 text-xs text-gray-400 font-mono tabular-nums align-middle">
                          {globalStart + idx + 1}
                        </td>

                        {/* Name */}
                        <td className="px-4 py-3 align-middle max-w-[240px]">
                          <Link href={`/universities/${uni.id}`}>
                            <span className="font-semibold text-gray-900 hover:text-blue-600 transition-colors cursor-pointer leading-tight line-clamp-1 block">
                              {uni.name}
                            </span>
                          </Link>
                        </td>

                        {/* Location */}
                        <td className="px-4 py-3 align-middle whitespace-nowrap">
                          <div className="flex items-center gap-1.5">
                            <span className="text-base leading-none">{flag}</span>
                            {isUnknown ? (
                              <span className="text-xs text-gray-400 italic">Not set</span>
                            ) : (
                              <span className="text-sm text-gray-600">{uni.city}, {uni.country}</span>
                            )}
                          </div>
                        </td>

                        {/* Courses */}
                        <td className="px-4 py-3 align-middle text-center">
                          {uni.courseCount != null && uni.courseCount > 0 ? (
                            <span className="inline-flex items-center justify-center bg-blue-50 text-blue-700 border border-blue-100 text-xs font-semibold px-2.5 py-0.5 rounded-full">
                              {uni.courseCount}
                            </span>
                          ) : (
                            <span className="text-xs text-gray-300">—</span>
                          )}
                        </td>

                        {/* Featured */}
                        <td className="px-4 py-3 align-middle text-center">
                          <button
                            type="button"
                            disabled={featuredSavingId === uni.id}
                            onClick={() => toggleFeatured(uni.id, isFeatured)}
                            title={isFeatured ? "Featured — click to disable" : "Mark as Featured"}
                            className={`inline-flex items-center gap-1 text-[11px] font-semibold px-2.5 py-1 rounded-full border transition-all cursor-pointer disabled:opacity-50 ${
                              isFeatured
                                ? "bg-amber-50 text-amber-700 border-amber-200 hover:bg-amber-100"
                                : "bg-gray-50 text-gray-400 border-gray-200 hover:bg-gray-100 hover:text-gray-600"
                            }`}
                          >
                            <Star className={`w-3 h-3 ${isFeatured ? "fill-amber-500 text-amber-500" : ""}`} />
                            {isFeatured ? "ON" : "OFF"}
                          </button>
                        </td>

                        {/* Website */}
                        <td className="px-4 py-3 align-middle max-w-[200px]">
                          {uni.website ? (
                            <a
                              href={uni.website}
                              target="_blank"
                              rel="noreferrer"
                              className="flex items-center gap-1.5 text-sm text-blue-600 hover:text-blue-700 hover:underline truncate"
                              onClick={(e) => e.stopPropagation()}
                            >
                              <Globe className="w-3.5 h-3.5 shrink-0 text-gray-400" />
                              <span className="truncate">{uni.website.replace(/^https?:\/\/(www\.)?/, "")}</span>
                            </a>
                          ) : (
                            <span className="text-xs text-gray-300">—</span>
                          )}
                        </td>

                        {/* Actions */}
                        <td className="px-4 py-3 align-middle">
                          <div className="flex items-center justify-end gap-1">
                            <Link href={`/universities/${uni.id}`}>
                              <button className="text-xs font-medium text-gray-500 hover:text-blue-600 bg-white hover:bg-blue-50 border border-gray-200 hover:border-blue-200 rounded-lg px-3 py-1.5 transition-all cursor-pointer whitespace-nowrap">
                                View →
                              </button>
                            </Link>
                            <DropdownMenu>
                              <DropdownMenuTrigger asChild>
                                <button className="flex items-center justify-center w-7 h-7 rounded-lg text-gray-400 hover:text-gray-700 hover:bg-gray-100 border border-transparent hover:border-gray-200 transition-all opacity-0 group-hover:opacity-100 cursor-pointer">
                                  <MoreHorizontal className="w-4 h-4" />
                                </button>
                              </DropdownMenuTrigger>
                              <DropdownMenuContent align="end" className="w-44">
                                <DropdownMenuItem onClick={() => navigate(`/universities/${uni.id}`)} className="gap-2 cursor-pointer">
                                  <BookOpen className="w-3.5 h-3.5 text-blue-500" />
                                  View Details
                                </DropdownMenuItem>
                                {uni.website && (
                                  <DropdownMenuItem asChild>
                                    <a href={uni.website} target="_blank" rel="noreferrer" className="flex items-center gap-2 cursor-pointer">
                                      <ExternalLink className="w-3.5 h-3.5 text-gray-400" />
                                      Open Website
                                    </a>
                                  </DropdownMenuItem>
                                )}
                                {(canEdit || canDelete) && <DropdownMenuSeparator />}
                                {canEdit && (
                                  <DropdownMenuItem onClick={() => openEdit(uni)} className="gap-2 cursor-pointer">
                                    <Pencil className="w-3.5 h-3.5 text-amber-500" />
                                    Edit Details
                                  </DropdownMenuItem>
                                )}
                                {canDelete && (
                                  <>
                                    <DropdownMenuSeparator />
                                    <DropdownMenuItem
                                      onClick={() => { setDeleteId(uni.id); setDeleteName(uni.name); }}
                                      className="gap-2 cursor-pointer text-red-600 focus:text-red-600 focus:bg-red-50"
                                    >
                                      <Trash2 className="w-3.5 h-3.5" />
                                      Delete
                                    </DropdownMenuItem>
                                  </>
                                )}
                              </DropdownMenuContent>
                            </DropdownMenu>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {/* Pagination footer */}
            <div className="flex flex-col sm:flex-row items-center justify-between gap-3 px-4 py-3 border-t border-gray-100 bg-gray-50/40">
              {/* Page size selector */}
              <div className="flex items-center gap-2 text-sm text-gray-500">
                <span>Rows per page</span>
                <Select value={String(pageSize)} onValueChange={handlePageSizeChange}>
                  <SelectTrigger className="h-7 w-16 text-xs">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {PAGE_SIZES.map((s) => (
                      <SelectItem key={s} value={String(s)} className="text-xs">{s}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              {/* Range info */}
              <p className="text-xs text-gray-400 order-first sm:order-none">
                {globalStart + 1}–{Math.min(globalStart + pageSize, allUniversities.length)} of {allUniversities.length}
              </p>

              {/* Nav buttons */}
              <div className="flex items-center gap-1">
                <button
                  onClick={() => setPage(1)}
                  disabled={safePage === 1}
                  className="flex items-center justify-center w-7 h-7 rounded border border-gray-200 text-gray-500 hover:bg-gray-100 disabled:opacity-30 disabled:cursor-not-allowed transition"
                  title="First page"
                >
                  <ChevronsLeft className="w-3.5 h-3.5" />
                </button>
                <button
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  disabled={safePage === 1}
                  className="flex items-center justify-center w-7 h-7 rounded border border-gray-200 text-gray-500 hover:bg-gray-100 disabled:opacity-30 disabled:cursor-not-allowed transition"
                  title="Previous page"
                >
                  <ChevronLeft className="w-3.5 h-3.5" />
                </button>

                {/* Page number pills */}
                {(() => {
                  const pages: number[] = [];
                  const delta = 2;
                  for (let i = Math.max(1, safePage - delta); i <= Math.min(totalPages, safePage + delta); i++) {
                    pages.push(i);
                  }
                  return pages.map((p) => (
                    <button
                      key={p}
                      onClick={() => setPage(p)}
                      className={`flex items-center justify-center w-7 h-7 rounded border text-xs font-medium transition ${
                        p === safePage
                          ? "bg-blue-600 border-blue-600 text-white"
                          : "border-gray-200 text-gray-600 hover:bg-gray-100"
                      }`}
                    >
                      {p}
                    </button>
                  ));
                })()}

                <button
                  onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                  disabled={safePage === totalPages}
                  className="flex items-center justify-center w-7 h-7 rounded border border-gray-200 text-gray-500 hover:bg-gray-100 disabled:opacity-30 disabled:cursor-not-allowed transition"
                  title="Next page"
                >
                  <ChevronRight className="w-3.5 h-3.5" />
                </button>
                <button
                  onClick={() => setPage(totalPages)}
                  disabled={safePage === totalPages}
                  className="flex items-center justify-center w-7 h-7 rounded border border-gray-200 text-gray-500 hover:bg-gray-100 disabled:opacity-30 disabled:cursor-not-allowed transition"
                  title="Last page"
                >
                  <ChevronsRight className="w-3.5 h-3.5" />
                </button>
              </div>
            </div>
          </>
        )}
      </div>

      {/* Edit dialog */}
      <Dialog open={editId !== null} onOpenChange={(o) => { if (!o) setEditId(null); }}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Edit University</DialogTitle>
          </DialogHeader>
          <div className="space-y-4 py-1">
            <div className="space-y-1">
              <Label>Name</Label>
              <Input value={editName} onChange={e => setEditName(e.target.value)} placeholder="University name" />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1">
                <Label>City</Label>
                <Input value={editCity} onChange={e => setEditCity(e.target.value)} placeholder="City" />
              </div>
              <div className="space-y-1">
                <Label>Country</Label>
                <Input value={editCountry} onChange={e => setEditCountry(e.target.value)} placeholder="Country" />
              </div>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditId(null)}>Cancel</Button>
            <Button onClick={saveEdit} disabled={editSaving}>
              {editSaving ? "Saving…" : "Save Changes"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete confirmation dialog */}
      <Dialog open={deleteId !== null} onOpenChange={(o) => { if (!o) setDeleteId(null); }}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>Delete University</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            Are you sure you want to delete <span className="font-semibold text-foreground">{deleteName}</span>?
            This will permanently remove the university and all its associated courses, scholarships, and requirements.
          </p>
          <DialogFooter className="gap-2">
            <Button variant="outline" onClick={() => setDeleteId(null)}>Cancel</Button>
            <Button variant="destructive" onClick={confirmDelete} disabled={deleteLoading}>
              {deleteLoading ? "Deleting…" : "Delete"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
