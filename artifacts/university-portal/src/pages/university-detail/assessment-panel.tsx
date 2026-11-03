import React from "react";
import type { AssessNote } from "../university-detail";
import { InsightCardsGrid } from "@/components/insights/insight-cards";

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
                {note.parsed_data && note.parsed_data.length > 0 ? (
                  <InsightCardsGrid cards={note.parsed_data} />
                ) : (
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
