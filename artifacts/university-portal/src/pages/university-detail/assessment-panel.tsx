import React from "react";
import type { AssessNote } from "../university-detail";

type Setter<T> = React.Dispatch<React.SetStateAction<T>>;
type Toast = (options: { title: string; description?: string; variant?: "destructive" }) => unknown;

interface AssessmentPanelProps {
  BASE: string; id: number; COUNTRIES: string[];
  Button: React.ElementType; ClipboardList: React.ElementType; Dialog: React.ElementType; DialogContent: React.ElementType;
  DialogDescription: React.ElementType; DialogFooter: React.ElementType; DialogHeader: React.ElementType;
  DialogTitle: React.ElementType; Label: React.ElementType; Pencil: React.ElementType; Plus: React.ElementType; Trash2: React.ElementType;
  assessAddCountry: string; assessAddText: string; assessAdding: boolean; assessCountry: string;
  assessDeleteNote: AssessNote | null; assessDeleting: boolean; assessEditCountry: string; assessEditNote: AssessNote | null;
  assessEditText: string; assessEditing: boolean; assessLoading: boolean; assessNotes: AssessNote[]; assessShowAdd: boolean;
  loadAssessNotes: () => Promise<void>; toast: Toast;
  setAssessAddCountry: Setter<string>; setAssessAddText: Setter<string>; setAssessAdding: Setter<boolean>; setAssessCountry: Setter<string>;
  setAssessDeleteNote: Setter<AssessNote | null>; setAssessDeleting: Setter<boolean>; setAssessEditCountry: Setter<string>;
  setAssessEditNote: Setter<AssessNote | null>; setAssessEditText: Setter<string>; setAssessEditing: Setter<boolean>; setAssessShowAdd: Setter<boolean>;
}

export function AssessmentPanel(props: AssessmentPanelProps) {
  const { BASE, Button, COUNTRIES, ClipboardList, Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, Label, Pencil, Plus, Trash2, assessAddCountry, assessAddText, assessAdding, assessCountry, assessDeleteNote, assessDeleting, assessEditCountry, assessEditNote, assessEditText, assessEditing, assessLoading, assessNotes, assessShowAdd, id, loadAssessNotes, setAssessAddCountry, setAssessAddText, setAssessAdding, setAssessCountry, setAssessDeleteNote, setAssessDeleting, setAssessEditCountry, setAssessEditNote, setAssessEditText, setAssessEditing, setAssessShowAdd, toast } = props;
  return (
        <div className="space-y-4">
          {/* Header row */}
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <p className="text-sm text-muted-foreground">
              {assessNotes.length} insight{assessNotes.length !== 1 ? "s" : ""} across {new Set(assessNotes.map(n => n.country)).size} countr{new Set(assessNotes.map(n => n.country)).size !== 1 ? "ies" : "y"}
            </p>
            <Button size="sm" onClick={() => { setAssessAddCountry(""); setAssessAddText(""); setAssessShowAdd(true); }}
              className="gap-1.5 bg-red-600 hover:bg-red-700 text-white">
              <Plus className="w-3.5 h-3.5" /> Add Key Insight
            </Button>
          </div>

          {/* Country filter pills */}
          {assessNotes.length > 0 && (
            <div className="flex flex-wrap gap-2">
              <button
                onClick={() => setAssessCountry("__all__")}
                className={`px-3 py-1 rounded-full text-xs font-medium border transition-colors cursor-pointer ${assessCountry === "__all__" ? "bg-red-600 text-white border-red-600" : "bg-white text-gray-600 border-gray-200 hover:border-red-300"}`}>
                All countries
              </button>
              {Array.from(new Set(assessNotes.map(n => n.country))).sort().map(c => (
                <button key={c}
                  onClick={() => setAssessCountry(c)}
                  className={`px-3 py-1 rounded-full text-xs font-medium border transition-colors cursor-pointer ${assessCountry === c ? "bg-red-600 text-white border-red-600" : "bg-white text-gray-600 border-gray-200 hover:border-red-300"}`}>
                  {c}
                </button>
              ))}
            </div>
          )}

          {/* Loading */}
          {assessLoading && <div className="py-12 text-center text-muted-foreground text-sm">Loading...</div>}

          {/* Empty state */}
          {!assessLoading && assessNotes.length === 0 && (
            <div className="border rounded-xl p-12 text-center text-muted-foreground">
              <ClipboardList className="w-10 h-10 mx-auto mb-3 opacity-30" />
              <p>No insights yet. Click "Add Key Insight" to get started.</p>
            </div>
          )}

          {/* Notes list */}
          {!assessLoading && assessNotes
            .filter(n => assessCountry === "__all__" || n.country === assessCountry)
            .map(note => (
              <div key={note.id} className="border rounded-xl overflow-hidden bg-white">
                {/* Note header */}
                <div className="flex items-center justify-between px-4 py-2.5 bg-red-50 border-b border-red-100">
                  <div className="flex items-center gap-2">
                    <ClipboardList className="w-4 h-4 text-red-600" />
                    <span className="font-semibold text-sm text-red-800">{note.country}</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <button onClick={() => { setAssessEditNote(note); setAssessEditCountry(note.country); setAssessEditText(note.raw_text); }}
                      className="p-1.5 rounded hover:bg-red-100 text-red-600 cursor-pointer" title="Edit note">
                      <Pencil className="w-3.5 h-3.5" />
                    </button>
                    <button onClick={() => setAssessDeleteNote(note)}
                      className="p-1.5 rounded hover:bg-red-50 text-red-500 cursor-pointer" title="Delete note">
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>

                {/* Cards grid */}
                {note.parsed_data && note.parsed_data.length > 0 ? (() => {
                  /* ── Per-category themes ── */
                  const THEMES: Record<string, { a: string; b: string; glow: string; tint: string; accent: string }> = {
                    "🏦": { a:"#0f4c9e", b:"#3b9eff", glow:"#3b9eff30", tint:"#f0f7ff", accent:"#3b9eff" },
                    "👤": { a:"#065f3e", b:"#00c48c", glow:"#00c48c30", tint:"#f0fff9", accent:"#00c48c" },
                    "👨‍👩‍👧": { a:"#b94b00", b:"#ff8c42", glow:"#ff8c4230", tint:"#fff7f0", accent:"#ff8c42" },
                    "💳": { a:"#5b1fa8", b:"#c17aff", glow:"#c17aff30", tint:"#f9f3ff", accent:"#9b59f5" },
                    "🎓": { a:"#0e4786", b:"#4ea8ff", glow:"#4ea8ff30", tint:"#eef6ff", accent:"#4ea8ff" },
                    "💍": { a:"#9b1b6e", b:"#ff6eb4", glow:"#ff6eb430", tint:"#fff0f8", accent:"#ff6eb4" },
                    "⏱":  { a:"#0b5c73", b:"#00c8e0", glow:"#00c8e030", tint:"#f0fdff", accent:"#00c8e0" },
                    "📅": { a:"#92400e", b:"#f97316", glow:"#f9731630", tint:"#fff7ed", accent:"#f97316" },
                    "ℹ️": { a:"#2d3748", b:"#718096", glow:"#71809630", tint:"#f7f8fa", accent:"#718096" },
                  };
                  const T = (e: string) => THEMES[e] ?? THEMES["ℹ️"];

                  const Badge = ({ badge, value }: { badge: string | null; value: string }) => {
                    if (badge === "yes")  return <span className="shrink-0 inline-flex items-center gap-1 text-[11px] font-bold px-2.5 py-1 rounded-full text-white whitespace-nowrap" style={{ background:"linear-gradient(135deg,#16a34a,#4ade80)", boxShadow:"0 2px 8px #16a34a40" }}>✓ Yes</span>;
                    if (badge === "no")   return <span className="shrink-0 inline-flex items-center gap-1 text-[11px] font-bold px-2.5 py-1 rounded-full text-white whitespace-nowrap" style={{ background:"linear-gradient(135deg,#dc2626,#f87171)", boxShadow:"0 2px 8px #dc262640" }}>✕ No</span>;
                    if (badge === "case") return <span className="shrink-0 inline-flex items-center gap-1 text-[11px] font-bold px-2.5 py-1 rounded-full text-white whitespace-nowrap" style={{ background:"linear-gradient(135deg,#d97706,#fbbf24)", boxShadow:"0 2px 8px #d9770640" }}>⚡ Case by case</span>;
                    return <span className="text-[13px] font-semibold text-gray-800 text-right leading-snug">{value}</span>;
                  };

                  return (
                    <div className="p-5" style={{ columns:"2 280px", gap:"16px" }}>
                      {note.parsed_data!.map((card, ci) => {
                        const t = T(card.emoji ?? "ℹ️");
                        const totalFields = (card.fields?.length ?? 0) + (card.sections?.reduce((a, s) => a + (s.fields?.length ?? 0), 0) ?? 0);
                        return (
                          <div key={ci} className="rounded-2xl overflow-hidden"
                            style={{ breakInside:"avoid", marginBottom:"16px", boxShadow:`0 4px 24px -4px ${t.glow},0 1px 3px rgba(0,0,0,0.08)`, border:"1px solid rgba(0,0,0,0.07)" }}>
                            {/* ── Gradient header ── */}
                            <div className="relative overflow-hidden px-4 py-4 flex items-center gap-3"
                              style={{ background:`linear-gradient(135deg,${t.a} 0%,${t.b} 100%)` }}>
                              {/* decorative circle */}
                              <div className="absolute -right-4 -top-4 w-24 h-24 rounded-full opacity-20"
                                style={{ background:"rgba(255,255,255,0.4)" }} />
                              <div className="w-11 h-11 rounded-xl flex items-center justify-center text-2xl shrink-0 relative"
                                style={{ background:"rgba(255,255,255,0.25)", backdropFilter:"blur(8px)" }}>
                                {card.emoji ?? "ℹ️"}
                              </div>
                              <div className="relative">
                                <p className="text-white font-bold text-sm leading-tight drop-shadow-sm">{card.title}</p>
                                <p className="text-white/70 text-[11px] mt-0.5 font-medium">{totalFields} details</p>
                              </div>
                            </div>
                            {/* ── Body ── */}
                            <div className="bg-white">
                              {card.fields?.map((f, fi) => (
                                <div key={fi} className="flex justify-between items-center gap-4 px-4 py-2.5"
                                  style={{ borderBottom:"1px solid #f1f5f9" }}>
                                  <span className="text-[13px] text-slate-400 shrink-0 max-w-[44%] leading-snug">{f.label}</span>
                                  <Badge badge={f.badge} value={f.value} />
                                </div>
                              ))}
                              {card.sections?.map((sec, si) => (
                                <div key={si}>
                                  <div className="px-4 py-2 flex items-center gap-2" style={{ background: t.tint }}>
                                    <div className="h-px flex-1 rounded-full" style={{ background: t.accent + "40" }} />
                                    <span className="text-[10px] font-bold uppercase tracking-widest" style={{ color: t.accent }}>{sec.label}</span>
                                    <div className="h-px flex-1 rounded-full" style={{ background: t.accent + "40" }} />
                                  </div>
                                  {sec.fields?.map((f, fi) => (
                                    <div key={fi} className="flex justify-between items-center gap-4 px-4 py-2.5"
                                      style={{ borderBottom:"1px solid #f1f5f9" }}>
                                      <span className="text-[13px] text-slate-400 shrink-0 max-w-[44%] leading-snug">{f.label}</span>
                                      <Badge badge={f.badge} value={f.value} />
                                    </div>
                                  ))}
                                </div>
                              ))}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  );
                })() : (
                  <div className="p-4 text-sm text-muted-foreground italic">
                    <p className="font-medium text-gray-700 mb-1">Raw notes:</p>
                    <pre className="whitespace-pre-wrap text-xs text-gray-600 font-mono bg-gray-50 rounded p-3 border">{note.raw_text}</pre>
                  </div>
                )}
              </div>
            ))}

          {/* ── Add Note Dialog ── */}
          <Dialog open={assessShowAdd} onOpenChange={setAssessShowAdd}>
            <DialogContent className="max-w-2xl">
              <DialogHeader>
                <DialogTitle className="flex items-center gap-2"><ClipboardList className="w-5 h-5 text-red-600" /> Add Key Insight</DialogTitle>
                <DialogDescription>Add a country-specific assessment insight for this university.</DialogDescription>
              </DialogHeader>
              <div className="space-y-4 py-2">
                <div>
                  <Label className="text-sm font-medium mb-1.5 block">Country</Label>
                  <select value={assessAddCountry} onChange={e => setAssessAddCountry(e.target.value)}
                    className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-red-300 cursor-pointer">
                    <option value="">Select country...</option>
                    {COUNTRIES.map(c => <option key={c} value={c}>{c}</option>)}
                  </select>
                </div>
                <div>
                  <Label className="text-sm font-medium mb-1.5 block">Key Insights (plain text)</Label>
                  <p className="text-xs text-muted-foreground mb-2">Paste any plain text — structured or unstructured. AI will extract the cards automatically (banks, sponsors, scholarship, turnaround times, etc.).</p>
                  <textarea value={assessAddText} onChange={e => setAssessAddText(e.target.value)}
                    rows={12} placeholder={"Example:\nAcceptable banks:\nAll A-class banks — accepted\n\nUnder 18:\nNot allowed\n\nSponsor requirements:\nTypes: Parents, Siblings, Grandparents\nMin income: AUD 30,000/yr\nBank statement: 1 year\n\nTurnaround times:\nOffer: 48 hours\nGTE: 4 days\nCoE: 4 days"}
                    className="w-full border rounded-lg px-3 py-2 text-sm font-mono resize-none focus:outline-none focus:ring-2 focus:ring-red-300" />
                </div>
              </div>
              <DialogFooter>
                <Button variant="outline" onClick={() => setAssessShowAdd(false)} disabled={assessAdding}>Cancel</Button>
                <Button disabled={!assessAddCountry || !assessAddText.trim() || assessAdding}
                  className="bg-red-600 hover:bg-red-700 text-white"
                  onClick={async () => {
                    setAssessAdding(true);
                    try {
                      const res = await fetch(`${BASE}/api/universities/${id}/assessment-notes`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ country: assessAddCountry, rawText: assessAddText }),
                      });
                      if (!res.ok) throw new Error(await res.text());
                      toast({ title: "Assessment note added", description: `Note for ${assessAddCountry} saved successfully.` });
                      setAssessShowAdd(false);
                      await loadAssessNotes();
                    } catch (err) {
                      toast({ title: "Error", description: String(err), variant: "destructive" });
                    } finally { setAssessAdding(false); }
                  }}>
                  {assessAdding ? (
                    <span className="flex items-center gap-2">
                      <svg className="animate-spin h-4 w-4 text-white" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/>
                        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"/>
                      </svg>
                      Parsing &amp; saving...
                    </span>
                  ) : "Save"}
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>

          {/* ── Edit Note Dialog ── */}
          <Dialog open={!!assessEditNote} onOpenChange={(v: boolean) => { if (!v) setAssessEditNote(null); }}>
            <DialogContent className="max-w-2xl">
              <DialogHeader>
                <DialogTitle className="flex items-center gap-2"><Pencil className="w-4 h-4 text-red-600" /> Edit Key Insight</DialogTitle>
                <DialogDescription>Update the country and assessment insight text.</DialogDescription>
              </DialogHeader>
              <div className="space-y-4 py-2">
                <div>
                  <Label className="text-sm font-medium mb-1.5 block">Country</Label>
                  <select value={assessEditCountry} onChange={e => setAssessEditCountry(e.target.value)}
                    className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-red-300 cursor-pointer">
                    <option value="">Select country...</option>
                    {COUNTRIES.map(c => <option key={c} value={c}>{c}</option>)}
                  </select>
                </div>
                <div>
                  <Label className="text-sm font-medium mb-1.5 block">Key Insights (plain text)</Label>
                  <textarea value={assessEditText} onChange={e => setAssessEditText(e.target.value)}
                    rows={12}
                    className="w-full border rounded-lg px-3 py-2 text-sm font-mono resize-none focus:outline-none focus:ring-2 focus:ring-red-300" />
                </div>
              </div>
              <DialogFooter>
                <Button variant="outline" onClick={() => setAssessEditNote(null)} disabled={assessEditing}>Cancel</Button>
                <Button disabled={!assessEditCountry || !assessEditText.trim() || assessEditing}
                  className="bg-red-600 hover:bg-red-700 text-white"
                  onClick={async () => {
                    if (!assessEditNote) return;
                    setAssessEditing(true);
                    try {
                      const res = await fetch(`${BASE}/api/assessment-notes/${assessEditNote.id}`, {
                        method: "PUT",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ country: assessEditCountry, rawText: assessEditText }),
                      });
                      if (!res.ok) throw new Error(await res.text());
                      toast({ title: "Note updated", description: `Note for ${assessEditCountry} updated successfully.` });
                      setAssessEditNote(null);
                      await loadAssessNotes();
                    } catch (err) {
                      toast({ title: "Error", description: String(err), variant: "destructive" });
                    } finally { setAssessEditing(false); }
                  }}>
                  {assessEditing ? (
                    <span className="flex items-center gap-2">
                      <svg className="animate-spin h-4 w-4 text-white" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/>
                        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"/>
                      </svg>
                      Parsing &amp; saving...
                    </span>
                  ) : "Save"}
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>

          {/* ── Delete Note Dialog ── */}
          <Dialog open={!!assessDeleteNote} onOpenChange={(v: boolean) => { if (!v) setAssessDeleteNote(null); }}>
            <DialogContent className="max-w-sm">
              <DialogHeader>
                <DialogTitle className="flex items-center gap-2 text-red-600"><Trash2 className="w-4 h-4" /> Delete Note</DialogTitle>
                <DialogDescription>Confirm permanent removal of this assessment insight.</DialogDescription>
              </DialogHeader>
              <p className="text-sm text-gray-600 py-2">
                Are you sure you want to delete the insight for <strong>{assessDeleteNote?.country}</strong>? This cannot be undone.
              </p>
              <DialogFooter>
                <Button variant="outline" onClick={() => setAssessDeleteNote(null)} disabled={assessDeleting}>Cancel</Button>
                <Button variant="destructive" disabled={assessDeleting}
                  onClick={async () => {
                    if (!assessDeleteNote) return;
                    setAssessDeleting(true);
                    try {
                      const res = await fetch(`${BASE}/api/assessment-notes/${assessDeleteNote.id}`, { method: "DELETE" });
                      if (!res.ok) throw new Error(await res.text());
                      toast({ title: "Note deleted" });
                      setAssessDeleteNote(null);
                      await loadAssessNotes();
                    } catch (err) {
                      toast({ title: "Error", description: String(err), variant: "destructive" });
                    } finally { setAssessDeleting(false); }
                  }}>
                  {assessDeleting ? "Deleting..." : "Delete"}
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </div>
      );
}
