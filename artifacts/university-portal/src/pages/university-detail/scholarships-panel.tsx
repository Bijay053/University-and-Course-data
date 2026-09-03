import React from "react";
import type { ScholCourse } from "../university-detail";

interface ScholarshipsPanelProps {
  Award: React.ElementType; Badge: React.ElementType; Button: React.ElementType; Pencil: React.ElementType; Trash2: React.ElementType;
  DEGREE_COLORS: Record<string, string>;
  openBulk: (mode: "scholarships") => void;
  openScholDelete: (courseId: number, courseName: string, scholarshipId?: number) => Promise<void>;
  openScholEdit: (courseId: number, courseName: string) => Promise<void>;
  scholCourses: ScholCourse[];
  scholLoading: boolean;
}

export function ScholarshipsPanel(props: ScholarshipsPanelProps) {
  const { Award, Badge, Button, DEGREE_COLORS, Pencil, Trash2, openBulk, openScholDelete, openScholEdit, scholCourses, scholLoading } = props;
  return (
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <p className="text-sm text-muted-foreground">
              {scholLoading ? "Loading…" : `${scholCourses.length} course${scholCourses.length !== 1 ? "s" : ""} with scholarship information`}
            </p>
            <Button size="sm" variant="outline" onClick={() => openBulk("scholarships")} className="gap-1.5 text-amber-700 border-amber-200 hover:bg-amber-50">
              <Pencil className="w-3.5 h-3.5" /> Bulk Add Scholarship
            </Button>
          </div>
          {scholCourses.length === 0 && !scholLoading ? (
            <div className="border rounded-xl p-12 text-center text-muted-foreground">
              <Award className="w-10 h-10 mx-auto mb-3 opacity-30" />
              <p>No scholarship information available for this university.</p>
            </div>
          ) : (
            <div className="grid gap-3">
              {scholCourses.map((c) => (
                <div key={c.id} className="border rounded-xl p-4 hover:shadow-sm transition-shadow bg-white">
                  <div className="flex items-start justify-between gap-4">
                    <div className="min-w-0">
                      <span className="font-semibold text-blue-700">{c.name}</span>
                      <div className="flex flex-wrap items-center gap-2 mt-1">
                        {c.degreeLevel && (
                          <span className={`inline-flex px-2 py-0.5 rounded text-xs font-semibold ${DEGREE_COLORS[c.degreeLevel] ?? "bg-gray-100 text-gray-600"}`}>
                            {c.degreeLevel}
                          </span>
                        )}
                        {c.category && <Badge variant="secondary" className="text-xs">{c.category}</Badge>}
                      </div>
                    </div>
                    <button onClick={() => openScholEdit(c.id, c.name)} className="p-1.5 rounded hover:bg-blue-50 text-blue-600 cursor-pointer shrink-0" title="Add / edit scholarship">
                      <Pencil className="w-3.5 h-3.5" />
                    </button>
                  </div>
                  <div className="mt-3 space-y-2">
                    {c.scholarships.map((s) => (
                      <div key={s.id} className="rounded-lg bg-amber-50 border border-amber-100 px-3 py-2">
                        <div className="flex items-start justify-between gap-2">
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-2 flex-wrap mb-1">
                              <Award className="w-3.5 h-3.5 text-amber-600 shrink-0" />
                              <span className="text-xs font-semibold text-amber-700">{s.name}</span>
                              {s.percentage != null && (
                                <span className="inline-flex items-center gap-0.5 bg-amber-200 text-amber-800 text-xs font-bold px-2 py-0.5 rounded-full">
                                  {s.percentage}% off
                                </span>
                              )}
                              {s.amount != null && (
                                <span className="inline-flex items-center gap-0.5 bg-amber-200 text-amber-800 text-xs font-bold px-2 py-0.5 rounded-full">
                                  {s.currency ?? "AUD"} {s.amount.toLocaleString()}
                                </span>
                              )}
                            </div>
                            {s.details && <p className="text-sm text-amber-800">{s.details}</p>}
                            {s.eligibilityCriteria && <p className="text-xs text-amber-600 mt-1">Eligibility: {s.eligibilityCriteria}</p>}
                          </div>
                          <button onClick={() => openScholDelete(c.id, c.name, s.id)} className="p-1 rounded hover:bg-red-50 text-red-400 shrink-0" title="Delete">
                            <Trash2 className="w-3.5 h-3.5" />
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      );
}
