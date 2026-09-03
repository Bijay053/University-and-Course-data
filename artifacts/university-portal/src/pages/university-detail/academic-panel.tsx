import React from "react";

type AcademicPanelProps = Record<string, any> & {
  allAcademicReqs: any[];
};

export function AcademicPanel(props: AcademicPanelProps) {
  const { Button, DEGREE_COLORS, Pencil, Trash2, acadReqsLoading, allAcademicReqs, openAcadEdit, openBulk, setDeleteAcadRow, tableScrollRef, txt } = props;
  return (
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-muted-foreground">
                {acadReqsLoading ? "Loading…" : (
                  <>
                    <strong>{allAcademicReqs.length}</strong> requirement{allAcademicReqs.length !== 1 ? "s" : ""} across{" "}
                    <strong>{new Set(allAcademicReqs.map((r) => r.courseId)).size}</strong> course{new Set(allAcademicReqs.map((r) => r.courseId)).size !== 1 ? "s" : ""}
                  </>
                )}
              </p>
              <p className="text-xs text-muted-foreground mt-0.5">Each country shows as a separate row. Same course + same country cannot be added twice.</p>
            </div>
            <Button size="sm" variant="outline" onClick={() => openBulk("academic")} className="gap-1.5 text-cyan-700 border-cyan-200 hover:bg-cyan-50">
              <Pencil className="w-3.5 h-3.5" /> Bulk Add Academic
            </Button>
          </div>
          <div ref={tableScrollRef} className="border rounded-xl overflow-auto" style={{ maxHeight: "70vh" }}>
            <table className="text-sm border-collapse w-full">
              <thead className="bg-gray-50 sticky top-0 z-10 border-b">
                <tr>
                  <th className="px-2 py-3 text-center font-semibold text-gray-500 min-w-[40px]">SN.</th>
                  <th className="text-left px-4 py-3 font-semibold text-gray-700 min-w-[260px]">Course Name</th>
                  <th className="text-left px-3 py-3 font-semibold text-gray-700 min-w-[110px]">Degree Level</th>
                  <th className="text-left px-3 py-3 font-semibold text-cyan-700 min-w-[140px]">Academic Level</th>
                  <th className="text-left px-3 py-3 font-semibold text-cyan-700 min-w-[80px]">Score</th>
                  <th className="text-left px-3 py-3 font-semibold text-cyan-700 min-w-[90px]">Score Type</th>
                  <th className="text-left px-3 py-3 font-semibold text-cyan-700 min-w-[120px]">Country</th>
                  <th className="px-3 py-3 font-semibold text-gray-500 min-w-[80px]">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {acadReqsLoading ? (
                  <tr><td colSpan={8} className="text-center py-12 text-muted-foreground">Loading requirements…</td></tr>
                ) : allAcademicReqs.length === 0 ? (
                  <tr><td colSpan={8} className="text-center py-12 text-muted-foreground">No academic requirements found</td></tr>
                ) : allAcademicReqs.map((r, idx) => (
                  <tr key={r.id} className="hover:bg-blue-50/30">
                    <td className="px-2 py-2.5 text-center text-gray-400 font-mono text-[11px] min-w-[40px]">{idx + 1}</td>
                    <td className="px-4 py-2.5 font-medium text-blue-700">
                      <span>{r.courseName}</span>
                    </td>
                    <td className="px-3 py-2.5">
                      {r.degreeLevel ? (
                        <span className={`inline-flex px-2 py-0.5 rounded text-xs font-semibold ${DEGREE_COLORS[r.degreeLevel] ?? "bg-gray-100 text-gray-600"}`}>
                          {r.degreeLevel}
                        </span>
                      ) : "—"}
                    </td>
                    <td className="px-3 py-2.5 text-cyan-700">{txt(r.academicLevel)}</td>
                    <td className="px-3 py-2.5 text-cyan-700 font-semibold">{r.academicScore != null ? String(r.academicScore) : "—"}</td>
                    <td className="px-3 py-2.5 text-cyan-600">{txt(r.scoreType)}</td>
                    <td className="px-3 py-2.5">
                      {r.academicCountry ? (
                        <span className="inline-flex items-center bg-cyan-50 border border-cyan-200 text-cyan-700 text-xs px-2 py-0.5 rounded-full">
                          {r.academicCountry}
                        </span>
                      ) : <span className="text-gray-400">—</span>}
                    </td>
                    <td className="px-3 py-2.5">
                      <div className="flex gap-1">
                        <button onClick={() => openAcadEdit(r)} className="p-1 rounded hover:bg-blue-50 text-blue-600 cursor-pointer" title="Edit"><Pencil className="w-3.5 h-3.5" /></button>
                        <button onClick={() => setDeleteAcadRow(r)} className="p-1 rounded hover:bg-red-50 text-red-500 cursor-pointer" title="Delete"><Trash2 className="w-3.5 h-3.5" /></button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      );
}
