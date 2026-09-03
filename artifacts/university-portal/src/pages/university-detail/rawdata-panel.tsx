import React from "react";
import type { StagedCourse } from "../university-detail";

type Setter<T> = React.Dispatch<React.SetStateAction<T>>;
interface RawDataPanelProps {
  AlertTriangle: React.ElementType; Button: React.ElementType; CheckCircle2: React.ElementType; Database: React.ElementType;
  Dialog: React.ElementType; DialogContent: React.ElementType; DialogDescription: React.ElementType; DialogFooter: React.ElementType;
  DialogHeader: React.ElementType; DialogTitle: React.ElementType; ExternalLink: React.ElementType; GitMerge: React.ElementType;
  Input: React.ElementType; Loader2: React.ElementType; Pencil: React.ElementType; RefreshCw: React.ElementType; Search: React.ElementType;
  Select: React.ElementType; SelectContent: React.ElementType; SelectItem: React.ElementType; SelectTrigger: React.ElementType;
  SelectValue: React.ElementType; StatusBadge: React.ElementType; Textarea: React.ElementType; Trash2: React.ElementType;
  Upload: React.ElementType; XCircle: React.ElementType; DEGREE_COLORS: Record<string, string>;
  approvedCount: number; approvingId: number | null; bulkApproveProgress: { done: number; total: number };
  bulkApproveRunning: boolean; bulkDeleteRawRunning: boolean; bulkMapRunning: boolean; bulkRejectFieldKey: string;
  bulkRejectReason: string; bulkRejectRunning: boolean; deletingId: number | null; filteredRaw: StagedCourse[];
  forceApproveRowId: number | null; importingAll: boolean; mappedIds: Set<number>; pendingCount: number;
  rawData: StagedCourse[]; rawLoading: boolean; rawSearch: string; rawSelectedIds: Set<number>;
  rawStatus: "all" | "pending" | "approved"; showBulkRejectConfirm: boolean; showForceApproveConfirm: boolean;
  fetchRawData: () => Promise<void>; handleApprove: (id: number, force?: boolean) => Promise<void>;
  handleBulkApprove: (force?: boolean) => Promise<void>; handleBulkMap: (forceOverwrite: boolean) => Promise<void>;
  handleBulkRejectSelected: () => Promise<void>; handleDelete: (id: number) => void; handleImportAll: () => void;
  num: (value: number | null | undefined) => number | "—"; openBackupMap: (course: StagedCourse) => Promise<void>;
  openEdit: (course: StagedCourse) => void; tableScrollRef: React.RefObject<HTMLDivElement | null>;
  toggleRawSelect: (id: number) => void; toggleSelectAllRaw: () => void; txt: (value: string | null | undefined) => string;
  setBulkRejectFieldKey: Setter<string>; setBulkRejectReason: Setter<string>; setForceApproveRowId: Setter<number | null>;
  setRawSearch: Setter<string>; setRawSelectedIds: Setter<Set<number>>; setRawStatus: Setter<"all" | "pending" | "approved">;
  setShowBulkDeleteRawConfirm: Setter<boolean>; setShowBulkRejectConfirm: Setter<boolean>; setShowDeleteAllRawConfirm: Setter<boolean>;
  setShowForceApproveConfirm: Setter<boolean>;
}

export function RawDataPanel(props: RawDataPanelProps) {
  const { AlertTriangle, Button, CheckCircle2, DEGREE_COLORS, Database, Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, ExternalLink, GitMerge, Input, Loader2, Pencil, RefreshCw, Search, Select, SelectContent, SelectItem, SelectTrigger, SelectValue, StatusBadge, Textarea, Trash2, Upload, XCircle, approvedCount, approvingId, bulkApproveProgress, bulkApproveRunning, bulkDeleteRawRunning, bulkMapRunning, bulkRejectFieldKey, bulkRejectReason, bulkRejectRunning, deletingId, fetchRawData, filteredRaw, forceApproveRowId, handleApprove, handleBulkApprove, handleBulkMap, handleBulkRejectSelected, handleDelete, handleImportAll, importingAll, mappedIds, num, openBackupMap, openEdit, pendingCount, rawData, rawLoading, rawSearch, rawSelectedIds, rawStatus, setBulkRejectFieldKey, setBulkRejectReason, setForceApproveRowId, setRawSearch, setRawSelectedIds, setRawStatus, setShowBulkDeleteRawConfirm, setShowBulkRejectConfirm, setShowDeleteAllRawConfirm, setShowForceApproveConfirm, showBulkRejectConfirm, showForceApproveConfirm, tableScrollRef, toggleRawSelect, toggleSelectAllRaw, txt } = props;
  return (
        <div className="space-y-4">
          {/* Toolbar */}
          <div className="flex flex-wrap gap-2 items-center">
            {/* Status filter */}
            <div className="flex rounded-lg border overflow-hidden text-sm font-medium">
              {(["all", "pending", "approved"] as const).map((s) => (
                <button
                  key={s}
                  onClick={() => setRawStatus(s)}
                  className={`px-3 py-1.5 capitalize transition-colors ${
                    rawStatus === s
                      ? "bg-primary text-primary-foreground"
                      : "bg-white text-muted-foreground hover:bg-muted"
                  }`}
                >
                  {s}
                  {s === "pending" && pendingCount > 0 && (
                    <span className="ml-1.5 bg-amber-500 text-white text-[10px] px-1.5 py-0.5 rounded-full">{pendingCount}</span>
                  )}
                  {s === "approved" && approvedCount > 0 && (
                    <span className="ml-1.5 bg-green-600 text-white text-[10px] px-1.5 py-0.5 rounded-full">{approvedCount}</span>
                  )}
                </button>
              ))}
            </div>

            {/* Search */}
            <div className="flex items-center gap-1.5 border rounded-md px-2 h-9 flex-1 min-w-[180px] max-w-xs bg-white">
              <Search className="h-4 w-4 text-muted-foreground shrink-0" />
              <Input
                placeholder="Search courses..."
                value={rawSearch}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => setRawSearch(e.target.value)}
                className="border-0 focus-visible:ring-0 px-0 h-8 bg-transparent"
              />
            </div>

            <span className="text-sm text-muted-foreground">
              {filteredRaw.length} course{filteredRaw.length !== 1 ? "s" : ""}
            </span>

            <div className="ml-auto flex gap-2">
              <Button variant="outline" size="sm" onClick={fetchRawData} disabled={rawLoading}>
                <RefreshCw className={`h-4 w-4 mr-1.5 ${rawLoading ? "animate-spin" : ""}`} />
                Refresh
              </Button>
              {rawData.length > 0 && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setShowDeleteAllRawConfirm(true)}
                  disabled={bulkDeleteRawRunning}
                  className="border-red-300 text-red-600 hover:bg-red-50"
                >
                  <Trash2 className="h-4 w-4 mr-1.5" />
                  Remove All
                </Button>
              )}
              {pendingCount > 0 && (
                <Button
                  size="sm"
                  onClick={handleImportAll}
                  disabled={importingAll}
                  className="bg-green-600 hover:bg-green-700 text-white"
                >
                  <Upload className="h-4 w-4 mr-1.5" />
                  {importingAll ? "Importing…" : `Import All (${pendingCount})`}
                </Button>
              )}
            </div>
          </div>

          {/* Bulk actions bar */}
          {rawSelectedIds.size > 0 && (
            <div className="flex items-center gap-2 px-3 py-2 bg-red-50 border border-red-200 rounded-lg text-sm">
              <span className="font-medium text-red-700">
                {rawSelectedIds.size} course{rawSelectedIds.size !== 1 ? "s" : ""} selected
              </span>
              <div className="flex items-center gap-1 ml-1">
                <Button
                  size="sm"
                  variant="outline"
                  disabled={bulkMapRunning || bulkApproveRunning}
                  onClick={() => handleBulkMap(false)}
                  className="h-7 text-xs border-red-300 text-red-700 hover:bg-red-100"
                >
                  <GitMerge className="h-3.5 w-3.5 mr-1" />
                  {bulkMapRunning ? "Mapping…" : "Map Backup (Fill Empty)"}
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={bulkMapRunning || bulkApproveRunning}
                  onClick={() => handleBulkMap(true)}
                  className="h-7 text-xs border-amber-300 text-amber-700 hover:bg-amber-50"
                >
                  <GitMerge className="h-3.5 w-3.5 mr-1" />
                  {bulkMapRunning ? "Mapping…" : "Map Backup (Overwrite)"}
                </Button>
                <Button
                  size="sm"
                  disabled={bulkMapRunning || bulkApproveRunning}
                  onClick={() => handleBulkApprove(false)}
                  className="h-7 text-xs bg-green-600 hover:bg-green-700 text-white"
                >
                  {bulkApproveRunning
                    ? <RefreshCw className="h-3.5 w-3.5 mr-1 animate-spin" />
                    : <CheckCircle2 className="h-3.5 w-3.5 mr-1" />}
                  {bulkApproveRunning
                    ? `Approving ${bulkApproveProgress.done}/${bulkApproveProgress.total}…`
                    : `Approve (${rawSelectedIds.size})`}
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={bulkMapRunning || bulkApproveRunning || bulkRejectRunning}
                  onClick={() => setShowForceApproveConfirm(true)}
                  className="h-7 text-xs border-amber-400 text-amber-700 bg-amber-50 hover:bg-amber-100"
                  title="Approve even if confidence is below the 60-point minimum"
                >
                  <AlertTriangle className="h-3.5 w-3.5 mr-1" />
                  {`Force Approve (${rawSelectedIds.size})`}
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={bulkMapRunning || bulkApproveRunning || bulkRejectRunning}
                  onClick={() => setShowBulkRejectConfirm(true)}
                  className="h-7 text-xs border-red-400 text-red-700 bg-red-50 hover:bg-red-100"
                >
                  {bulkRejectRunning ? <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" /> : <XCircle className="h-3.5 w-3.5 mr-1" />}
                  {bulkRejectRunning ? "Rejecting…" : `Reject (${rawSelectedIds.size})`}
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={bulkDeleteRawRunning || bulkMapRunning || bulkApproveRunning || bulkRejectRunning}
                  onClick={() => setShowBulkDeleteRawConfirm(true)}
                  className="h-7 text-xs border-red-400 text-red-700 bg-red-50 hover:bg-red-100"
                >
                  {bulkDeleteRawRunning
                    ? <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />
                    : <Trash2 className="h-3.5 w-3.5 mr-1" />}
                  {bulkDeleteRawRunning ? "Deleting…" : `Delete (${rawSelectedIds.size})`}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => setRawSelectedIds(new Set())}
                  className="h-7 text-xs text-muted-foreground"
                >
                  Clear
                </Button>
              </div>
            </div>
          )}

          {/* Bulk reject with reason dialog */}
          <Dialog open={showForceApproveConfirm} onOpenChange={setShowForceApproveConfirm}>
            <DialogContent className="max-w-lg">
              <DialogHeader>
                <DialogTitle className="flex items-center gap-2 text-amber-700">
                  <AlertTriangle className="w-5 h-5 shrink-0" />
                  Force Approve {rawSelectedIds.size} Course{rawSelectedIds.size !== 1 ? "s" : ""}
                </DialogTitle>
                <DialogDescription>Confirm publishing the selected staged courses while bypassing the confidence gate.</DialogDescription>
              </DialogHeader>
              <div className="space-y-3 text-sm text-gray-600">
                <p>
                  This bypasses the <strong>60-point confidence gate</strong> and publishes the
                  selected course{rawSelectedIds.size !== 1 ? "s" : ""} to production
                  <strong> even if critical fields (fee, English test, intake, duration) are missing</strong>.
                </p>
                <p className="rounded-md bg-amber-50 border border-amber-200 px-3 py-2 text-amber-800 text-xs">
                  Use this only when you knowingly want to publish incomplete data. Courses that
                  already pass the gate will approve normally either way.
                </p>
              </div>
              <DialogFooter>
                <Button variant="outline" onClick={() => setShowForceApproveConfirm(false)} className="cursor-pointer">
                  Cancel
                </Button>
                <Button
                  onClick={() => { setShowForceApproveConfirm(false); void handleBulkApprove(true); }}
                  className="bg-amber-600 hover:bg-amber-700 text-white cursor-pointer"
                >
                  <AlertTriangle className="h-4 w-4 mr-1.5" />
                  Force Approve {rawSelectedIds.size}
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>

          {/* Per-row force approve confirm */}
          <Dialog open={forceApproveRowId !== null} onOpenChange={(o: boolean) => { if (!o) setForceApproveRowId(null); }}>
            <DialogContent className="max-w-md">
              <DialogHeader>
                <DialogTitle className="flex items-center gap-2 text-amber-700">
                  <AlertTriangle className="w-5 h-5 shrink-0" />
                  Force Approve Course
                </DialogTitle>
                <DialogDescription>Confirm publishing this staged course while bypassing the confidence gate.</DialogDescription>
              </DialogHeader>
              <div className="space-y-3 text-sm text-gray-600">
                <p>
                  This bypasses the <strong>60-point confidence gate</strong> and publishes the course to production
                  <strong> even if critical fields (fee, English test, intake, duration) are missing</strong>.
                </p>
                <p className="rounded-md bg-amber-50 border border-amber-200 px-3 py-2 text-amber-800 text-xs">
                  Use only when you knowingly want to publish incomplete data.
                </p>
              </div>
              <DialogFooter>
                <Button variant="outline" onClick={() => setForceApproveRowId(null)} className="cursor-pointer">
                  Cancel
                </Button>
                <Button
                  className="bg-amber-600 hover:bg-amber-700 text-white cursor-pointer"
                  onClick={() => {
                    const id = forceApproveRowId!;
                    setForceApproveRowId(null);
                    void handleApprove(id, true);
                  }}
                >
                  <AlertTriangle className="h-4 w-4 mr-1.5" />
                  Force Approve
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>

          <Dialog open={showBulkRejectConfirm} onOpenChange={(o: boolean) => {
            if (!o) {
              setShowBulkRejectConfirm(false);
              setBulkRejectReason("");
              setBulkRejectFieldKey("general");
            }
          }}>
            <DialogContent className="max-w-lg">
              <DialogHeader>
                <DialogTitle className="flex items-center gap-2 text-red-700">
                  <XCircle className="w-5 h-5 shrink-0" />
                  Reject {rawSelectedIds.size} Course{rawSelectedIds.size !== 1 ? "s" : ""} With Reason
                </DialogTitle>
                <DialogDescription>Choose a reason before rejecting the selected staged courses.</DialogDescription>
              </DialogHeader>
              <div className="space-y-4">
                <div className="text-sm text-muted-foreground">
                  Rejected courses can be re-staged on the very next scrape — no 7-day cooldown applies. Select the field that was wrong so the system knows what to look for on rerun.
                </div>
                <div>
                  <label className="text-sm font-medium">Field</label>
                  <Select value={bulkRejectFieldKey} onValueChange={setBulkRejectFieldKey}>
                    <SelectTrigger className="mt-1">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="general">General / whole course</SelectItem>
                      <SelectItem value="internationalFee">International Fee</SelectItem>
                      <SelectItem value="courseLocation">Course Location</SelectItem>
                      <SelectItem value="ieltsOverall">English Requirement</SelectItem>
                      <SelectItem value="courseName">Wrong Page / Course Match</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div>
                  <label className="text-sm font-medium">Note <span className="text-muted-foreground font-normal">(optional)</span></label>
                  <Textarea
                    rows={3}
                    className="mt-1"
                    value={bulkRejectReason}
                    onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) => setBulkRejectReason(e.target.value)}
                    placeholder="Optional note for your own reference (e.g. 'fee was from domestic page'). Courses will be re-stageable on the very next scrape."
                  />
                </div>
              </div>
              <DialogFooter>
                <Button
                  variant="outline"
                  onClick={() => {
                    setShowBulkRejectConfirm(false);
                    setBulkRejectReason("");
                    setBulkRejectFieldKey("general");
                  }}
                  disabled={bulkRejectRunning}
                >
                  Cancel
                </Button>
                <Button
                  variant="destructive"
                  className="bg-red-600 hover:bg-red-700"
                  onClick={handleBulkRejectSelected}
                  disabled={bulkRejectRunning}
                >
                  {bulkRejectRunning ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <XCircle className="w-4 h-4 mr-2" />}
                  Reject Selected
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>

          {/* Table */}
          {rawLoading ? (
            <div className="border rounded-xl overflow-hidden">
              <div className="flex items-center gap-2 px-4 py-2.5 border-b bg-gray-50 text-xs font-medium text-muted-foreground">
                <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                Loading {rawStatus === "all" ? "all" : rawStatus} courses…
              </div>
              {Array.from({ length: 8 }).map((_, i) => (
                <div
                  key={i}
                  className="flex items-center gap-4 px-4 py-3 border-b last:border-b-0 animate-pulse"
                  style={{ animationDelay: `${i * 60}ms` }}
                >
                  <div className="h-4 w-6 rounded bg-gray-200" />
                  <div className="h-4 flex-1 max-w-[260px] rounded bg-gray-200" />
                  <div className="h-4 w-20 rounded bg-gray-200" />
                  <div className="h-4 w-24 rounded bg-gray-200" />
                  <div className="h-4 w-16 rounded bg-gray-200" />
                  <div className="h-4 w-20 rounded bg-gray-200" />
                  <div className="h-4 w-28 rounded bg-gray-200" />
                </div>
              ))}
            </div>
          ) : filteredRaw.length === 0 ? (
            <div className="border rounded-xl py-16 text-center text-muted-foreground">
              <Database className="w-10 h-10 mx-auto mb-3 opacity-20" />
              {rawData.length === 0 ? (
                <>
                  <p>No raw scrape data for this university.</p>
                  <p className="text-xs mt-1">
                    Live courses (if any) were imported via Excel or an older path that doesn&apos;t
                    keep a staged copy. Run a scrape job to populate raw data.
                  </p>
                </>
              ) : (
                <>
                  <p>
                    No raw rows match the current filter
                    {rawStatus !== "all" ? ` (status: "${rawStatus}")` : ""}.
                  </p>
                  <p className="text-xs mt-1">
                    Try the &ldquo;All&rdquo; tab — this university has {rawData.length} staged row
                    {rawData.length === 1 ? "" : "s"} in other states.
                  </p>
                </>
              )}
            </div>
          ) : (
            <div ref={tableScrollRef} className="border rounded-xl overflow-auto" style={{ maxHeight: "70vh" }}>
              <table className="text-xs whitespace-nowrap border-collapse" style={{ minWidth: 2400 }}>
                <thead className="bg-gray-50 sticky top-0 z-20">
                  <tr className="text-[10px] font-bold text-gray-500 uppercase tracking-wide border-b">
                    <th className="sticky left-0 z-30 bg-gray-50 border-r px-3 py-2 text-left min-w-[52px]">
                      <div className="flex items-center gap-1.5">
                        <input
                          type="checkbox"
                          className="cursor-pointer rounded"
                          checked={filteredRaw.length > 0 && filteredRaw.every(c => rawSelectedIds.has(c.id))}
                          onChange={toggleSelectAllRaw}
                          title="Select all"
                        />
                        <span>#</span>
                      </div>
                    </th>
                    <th className="sticky bg-gray-50 border-r px-3 py-2 text-left min-w-[220px]" style={{ left: 52 }}>Course Name</th>
                    <th className="px-2 py-2 border-r text-center min-w-[80px]">Status</th>
                    <th className="px-2 py-2 text-gray-600 font-medium min-w-[110px]">Degree Level</th>
                    <th className="px-2 py-2 text-gray-600 font-medium min-w-[100px]">Category</th>
                    <th className="px-2 py-2 text-gray-600 font-medium min-w-[70px]">Duration</th>
                    <th className="px-2 py-2 text-gray-600 font-medium min-w-[60px]">Term</th>
                    <th className="px-2 py-2 text-gray-600 font-medium min-w-[80px]">Mode</th>
                    <th className="px-2 py-2 text-blue-600 font-medium min-w-[120px] border-r">Course Location</th>
                    <th className="px-2 py-2 text-amber-700 font-medium min-w-[80px]">Int'l Fee</th>
                    <th className="px-2 py-2 text-amber-700 font-medium min-w-[55px]">Term</th>
                    <th className="px-2 py-2 text-amber-700 font-medium min-w-[45px]">Year</th>
                    <th className="px-2 py-2 text-amber-700 font-medium min-w-[50px] border-r">Curr.</th>
                    <th className="px-2 py-2 text-blue-700 font-medium min-w-[90px] border-r">Intakes</th>
                    <th className="px-2 py-2 text-purple-700 font-medium min-w-[30px]">IL</th>
                    <th className="px-2 py-2 text-purple-700 font-medium min-w-[30px]">IS</th>
                    <th className="px-2 py-2 text-purple-700 font-medium min-w-[30px]">IW</th>
                    <th className="px-2 py-2 text-purple-700 font-medium min-w-[30px]">IR</th>
                    <th className="px-2 py-2 text-purple-700 font-semibold min-w-[30px] border-r">IO</th>
                    <th className="px-2 py-2 text-orange-600 font-medium min-w-[30px]">PL</th>
                    <th className="px-2 py-2 text-orange-600 font-medium min-w-[30px]">PS</th>
                    <th className="px-2 py-2 text-orange-600 font-medium min-w-[30px]">PW</th>
                    <th className="px-2 py-2 text-orange-600 font-medium min-w-[30px]">PR</th>
                    <th className="px-2 py-2 text-orange-600 font-semibold min-w-[30px] border-r">PO</th>
                    <th className="px-2 py-2 text-rose-600 font-medium min-w-[30px]">TL</th>
                    <th className="px-2 py-2 text-rose-600 font-medium min-w-[30px]">TS</th>
                    <th className="px-2 py-2 text-rose-600 font-medium min-w-[30px]">TW</th>
                    <th className="px-2 py-2 text-rose-600 font-medium min-w-[30px]">TR</th>
                    <th className="px-2 py-2 text-rose-600 font-semibold min-w-[30px] border-r">TO</th>
                    <th className="px-2 py-2 text-pink-600 font-medium min-w-[30px] border-r">CAE</th>
                    <th className="px-2 py-2 text-cyan-700 font-medium min-w-[100px]">Acad. Level</th>
                    <th className="px-2 py-2 text-cyan-700 font-medium min-w-[55px] border-r">Score</th>
                    <th className="px-2 py-2 text-gray-600 font-medium min-w-[50px]">%</th>
                    <th className="px-2 py-2 text-gray-600 font-medium min-w-[100px]">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {filteredRaw.map((c, idx) => (
                    <tr
                      key={c.id}
                      className={`transition-colors ${
                        c.status === "approved" ? "bg-green-50/30 hover:bg-green-50/50" :
                        c.status === "rejected" ? "bg-red-50/30 hover:bg-red-50/50" :
                        "hover:bg-blue-50/20"
                      }`}
                    >
                      <td className={`sticky left-0 border-r px-3 py-2 text-muted-foreground font-mono ${
                        c.status === "approved" ? "bg-green-50" :
                        c.status === "rejected" ? "bg-red-50" : "bg-white"
                      }`}>
                        <div className="flex items-center gap-1.5">
                          <input
                            type="checkbox"
                            className="cursor-pointer rounded shrink-0"
                            checked={rawSelectedIds.has(c.id)}
                            onChange={() => toggleRawSelect(c.id)}
                          />
                          <span>{idx + 1}</span>
                        </div>
                      </td>
                      <td className={`sticky border-r px-3 py-2 font-medium text-gray-800 min-w-[220px] ${
                        c.status === "approved" ? "bg-green-50" :
                        c.status === "rejected" ? "bg-red-50" : "bg-white"
                      }`} style={{ left: 52 }}>
                        <div className="flex items-center gap-1.5">
                          <span className="line-clamp-1 max-w-[200px]">{c.course_name}</span>
                          {c.course_website && (
                            <a href={c.course_website} target="_blank" rel="noreferrer" className="text-blue-400 shrink-0">
                              <ExternalLink className="w-3 h-3" />
                            </a>
                          )}
                        </div>
                      </td>
                      <td className="px-2 py-2 border-r text-center"><StatusBadge status={c.status} /></td>
                      <td className="px-2 py-2">
                        {c.degree_level ? (
                          <span className={`inline-flex px-1.5 py-0.5 rounded text-[10px] font-semibold ${DEGREE_COLORS[c.degree_level] ?? "bg-gray-100 text-gray-600"}`}>
                            {c.degree_level}
                          </span>
                        ) : "—"}
                      </td>
                      <td className="px-2 py-2 text-gray-500">{txt(c.category)}</td>
                      <td className="px-2 py-2 text-gray-600">{num(c.duration)}</td>
                      <td className="px-2 py-2 text-gray-500">{txt(c.duration_term)}</td>
                      <td className="px-2 py-2 text-gray-500">{txt(c.study_mode)}</td>
                      <td className="px-2 py-2 text-blue-600 border-r">{txt(c.course_location)}</td>
                      <td className="px-2 py-2 text-amber-700 font-medium">{c.international_fee ? c.international_fee.toLocaleString() : "—"}</td>
                      <td className="px-2 py-2 text-amber-600">{txt(c.fee_term)}</td>
                      <td className="px-2 py-2 text-amber-600">{c.fee_year ?? "—"}</td>
                      <td className="px-2 py-2 text-amber-600 border-r">{txt(c.currency)}</td>
                      <td className="px-2 py-2 text-blue-600 border-r">{Array.isArray(c.intake_months) ? c.intake_months.join(", ") : txt(c.intake_months as string | null)}</td>
                      <td className="px-2 py-2 text-purple-600">{num(c.ielts_listening)}</td>
                      <td className="px-2 py-2 text-purple-600">{num(c.ielts_speaking)}</td>
                      <td className="px-2 py-2 text-purple-600">{num(c.ielts_writing)}</td>
                      <td className="px-2 py-2 text-purple-600">{num(c.ielts_reading)}</td>
                      <td className="px-2 py-2 text-purple-700 font-semibold border-r">{num(c.ielts_overall)}</td>
                      <td className="px-2 py-2 text-orange-500">{num(c.pte_listening)}</td>
                      <td className="px-2 py-2 text-orange-500">{num(c.pte_speaking)}</td>
                      <td className="px-2 py-2 text-orange-500">{num(c.pte_writing)}</td>
                      <td className="px-2 py-2 text-orange-500">{num(c.pte_reading)}</td>
                      <td className="px-2 py-2 text-orange-600 font-semibold border-r">{num(c.pte_overall)}</td>
                      <td className="px-2 py-2 text-rose-500">{num(c.toefl_listening)}</td>
                      <td className="px-2 py-2 text-rose-500">{num(c.toefl_speaking)}</td>
                      <td className="px-2 py-2 text-rose-500">{num(c.toefl_writing)}</td>
                      <td className="px-2 py-2 text-rose-500">{num(c.toefl_reading)}</td>
                      <td className="px-2 py-2 text-rose-600 font-semibold border-r">{num(c.toefl_overall)}</td>
                      <td className="px-2 py-2 text-pink-600 font-semibold border-r">{num(c.cambridge_overall)}</td>
                      <td className="px-2 py-2 text-cyan-700">{txt(c.academic_level)}</td>
                      <td className="px-2 py-2 text-cyan-600 font-semibold border-r">{num(c.academic_score)}</td>
                      <td className="px-2 py-2 text-muted-foreground">
                        {c.completeness != null ? (
                          <span className={`font-semibold ${c.completeness >= 80 ? "text-green-600" : c.completeness >= 50 ? "text-amber-600" : "text-red-500"}`}>
                            {c.completeness}%
                          </span>
                        ) : "—"}
                      </td>
                      <td className="px-2 py-2">
                        <div className="flex items-center gap-1">
                          <button
                            onClick={() => openEdit(c)}
                            title="Edit"
                            className="p-1 rounded hover:bg-blue-100 text-blue-600 cursor-pointer"
                          >
                            <Pencil className="w-3.5 h-3.5" />
                          </button>
                          {c.status === "pending" && (
                            <>
                              <button
                                onClick={() => openBackupMap(c)}
                                title={mappedIds.has(c.id) ? "Backup mapped — map again" : "Map from Backup"}
                                className={`p-1 rounded cursor-pointer ${mappedIds.has(c.id) ? "text-teal-600 hover:bg-teal-100 bg-teal-50" : "text-red-500 hover:bg-red-100"}`}
                              >
                                <GitMerge className="w-3.5 h-3.5" />
                              </button>
                              <button
                                onClick={() => handleApprove(c.id)}
                                disabled={approvingId === c.id}
                                title="Approve & Import"
                                className="p-1 rounded hover:bg-green-100 text-green-600 disabled:opacity-40 cursor-pointer"
                              >
                                <CheckCircle2 className="w-3.5 h-3.5" />
                              </button>
                              <button
                                onClick={() => setForceApproveRowId(c.id)}
                                disabled={approvingId === c.id}
                                title="Force Approve (bypass confidence gate)"
                                className="p-1 rounded hover:bg-amber-100 text-amber-600 disabled:opacity-40 cursor-pointer"
                              >
                                <AlertTriangle className="w-3.5 h-3.5" />
                              </button>
                            </>
                          )}
                          <button
                            onClick={() => handleDelete(c.id)}
                            disabled={deletingId === c.id}
                            title="Delete"
                            className="p-1 rounded hover:bg-red-100 text-red-500 disabled:opacity-40 cursor-pointer"
                          >
                            <Trash2 className="w-3.5 h-3.5" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      );
}
