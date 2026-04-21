import { useState } from "react";
import { Link } from "wouter";
import { useListUniversities, useCreateUniversity, getListUniversitiesQueryKey } from "@workspace/api-client-react";
import { useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from "@/components/ui/form";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import * as z from "zod";
import { Plus, Search, MapPin, Globe, ArrowRight, Building2 } from "lucide-react";

const formSchema = z.object({
  name: z.string().min(1, "Name is required"),
  country: z.string().min(1, "Country is required"),
  city: z.string().min(1, "City is required"),
  website: z.string().url().optional().or(z.literal("")),
});

const AVATAR_COLORS = [
  "from-blue-500 to-blue-700",
  "from-violet-500 to-violet-700",
  "from-emerald-500 to-emerald-700",
  "from-orange-500 to-orange-700",
  "from-rose-500 to-rose-700",
  "from-cyan-500 to-cyan-700",
  "from-amber-500 to-amber-700",
  "from-indigo-500 to-indigo-700",
];

function avatarColor(name: string) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) & 0xffffff;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

function initials(name: string) {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("");
}

const COUNTRY_FLAGS: Record<string, string> = {
  Australia: "🇦🇺", "United Kingdom": "🇬🇧", UK: "🇬🇧", USA: "🇺🇸",
  "United States": "🇺🇸", Canada: "🇨🇦", "New Zealand": "🇳🇿",
  Germany: "🇩🇪", France: "🇫🇷", India: "🇮🇳", China: "🇨🇳",
  Japan: "🇯🇵", Singapore: "🇸🇬", Malaysia: "🇲🇾",
};

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
    <div className="space-y-5">
      {/* Header */}
      <div className="flex flex-col sm:flex-row justify-between gap-3 items-start sm:items-center">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-gray-900">Universities</h1>
          <p className="text-sm text-gray-500 mt-0.5">Manage partner universities and institutions.</p>
        </div>
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogTrigger asChild>
            <Button className="gap-1.5">
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
      </div>

      {/* Search + Table container */}
      <div className="bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
        {/* Search bar */}
        <div className="px-4 py-3 border-b border-gray-100 bg-gray-50/60">
          <div className="flex items-center gap-2.5 max-w-sm">
            <Search className="w-4 h-4 text-gray-400 shrink-0" />
            <input
              className="flex-1 text-sm bg-transparent outline-none placeholder:text-gray-400 text-gray-900"
              placeholder="Search universities..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
        </div>

        {isLoading ? (
          <div className="flex items-center justify-center py-16 text-sm text-gray-400">
            <div className="flex items-center gap-2">
              <div className="w-4 h-4 border-2 border-gray-300 border-t-blue-500 rounded-full animate-spin" />
              Loading universities…
            </div>
          </div>
        ) : universities.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-16 text-gray-400">
            <Building2 className="w-10 h-10 mb-3 opacity-30" />
            <p className="text-sm font-medium">No universities found</p>
            {search && <p className="text-xs mt-1">Try a different search term</p>}
          </div>
        ) : (
          <>
            {/* Table header */}
            <div className="hidden md:grid grid-cols-[40px_2.5fr_1.5fr_80px_2fr_auto] items-center px-5 py-2 text-xs font-semibold text-gray-400 uppercase tracking-wider border-b border-gray-100 bg-gray-50/40">
              <span>SN.</span>
              <span>Institution</span>
              <span>Location</span>
              <span>Courses</span>
              <span>Website</span>
              <span className="pr-1">Actions</span>
            </div>

            {/* Rows */}
            <div className="divide-y divide-gray-50">
              {universities.map((uni, idx) => {
                const flag = COUNTRY_FLAGS[uni.country] ?? "🏫";
                const grad = avatarColor(uni.name);
                const init = initials(uni.name);
                const isUnknown = uni.city === "Unknown" || uni.country === "Unknown";

                return (
                  <div key={uni.id}
                    className="group grid md:grid-cols-[40px_2.5fr_1.5fr_80px_2fr_auto] items-center px-5 py-3.5 hover:bg-blue-50/40 transition-colors">

                    {/* SN. */}
                    <div className="hidden md:block text-xs text-gray-400 font-mono">{idx + 1}</div>

                    {/* Name + avatar */}
                    <div className="flex items-center gap-3 min-w-0">
                      <div className={`hidden md:flex shrink-0 w-9 h-9 rounded-xl bg-gradient-to-br ${grad} items-center justify-center text-white text-xs font-bold shadow-sm`}>
                        {init}
                      </div>
                      <div className="min-w-0">
                        <Link href={`/universities/${uni.id}`}>
                          <span className="font-semibold text-gray-900 text-sm hover:text-blue-600 transition-colors cursor-pointer leading-tight line-clamp-1">
                            {uni.name}
                          </span>
                        </Link>
                        {/* Mobile location */}
                        <div className="flex items-center gap-1 mt-0.5 md:hidden">
                          <span className="text-xs">{flag}</span>
                          <span className="text-xs text-gray-500">{isUnknown ? uni.country : `${uni.city}, ${uni.country}`}</span>
                        </div>
                      </div>
                    </div>

                    {/* Location */}
                    <div className="hidden md:flex items-center gap-1.5">
                      <span className="text-base leading-none">{flag}</span>
                      {isUnknown ? (
                        <span className="text-xs text-gray-400 italic">Not set</span>
                      ) : (
                        <span className="text-sm text-gray-600">{uni.city}, {uni.country}</span>
                      )}
                    </div>

                    {/* Courses count */}
                    <div className="hidden md:flex items-center">
                      {uni.courseCount != null && uni.courseCount > 0 ? (
                        <span className="inline-flex items-center bg-blue-50 text-blue-700 border border-blue-100 text-xs font-semibold px-2 py-0.5 rounded-full">
                          {uni.courseCount}
                        </span>
                      ) : (
                        <span className="text-xs text-gray-300">—</span>
                      )}
                    </div>

                    {/* Website */}
                    <div className="hidden md:flex items-center gap-1.5 min-w-0">
                      {uni.website ? (
                        <>
                          <Globe className="w-3.5 h-3.5 text-gray-400 shrink-0" />
                          <a href={uni.website} target="_blank" rel="noreferrer"
                            className="text-sm text-blue-600 hover:text-blue-700 hover:underline truncate max-w-[200px]"
                            onClick={(e) => e.stopPropagation()}>
                            {uni.website.replace(/^https?:\/\/(www\.)?/, "")}
                          </a>
                        </>
                      ) : (
                        <span className="text-xs text-gray-300">—</span>
                      )}
                    </div>

                    {/* Action */}
                    <div className="flex justify-end">
                      <Link href={`/universities/${uni.id}`}>
                        <button className="flex items-center gap-1.5 text-xs font-medium text-gray-500 hover:text-blue-600 group-hover:text-blue-600 bg-white hover:bg-blue-50 border border-gray-200 hover:border-blue-200 rounded-lg px-3 py-1.5 transition-all cursor-pointer">
                          View
                          <ArrowRight className="w-3 h-3 transition-transform group-hover:translate-x-0.5" />
                        </button>
                      </Link>
                    </div>
                  </div>
                );
              })}
            </div>

            {/* Footer count */}
            <div className="px-5 py-2.5 border-t border-gray-100 bg-gray-50/40 text-xs text-gray-400">
              {universities.length} {universities.length === 1 ? "university" : "universities"}
              {search && ` matching "${search}"`}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
