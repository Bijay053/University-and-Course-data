import React from "react";

type EnglishCourse = {
  id: number; name: string; degreeLevel?: string | null;
  ieltsListening?: number | null; ieltsSpeaking?: number | null; ieltsWriting?: number | null; ieltsReading?: number | null; ieltsOverall?: number | null;
  pteListening?: number | null; pteSpeaking?: number | null; pteWriting?: number | null; pteReading?: number | null; pteOverall?: number | null;
  toeflListening?: number | null; toeflSpeaking?: number | null; toeflWriting?: number | null; toeflReading?: number | null; toeflOverall?: number | null;
  otherEnglishTestName?: string | null; otherEnglishReading?: number | null; otherEnglishListening?: number | null;
  otherEnglishSpeaking?: number | null; otherEnglishWriting?: number | null; otherEnglishOverall?: number | null;
};

interface EnglishPanelProps {
  Button: React.ElementType; Pencil: React.ElementType; Trash2: React.ElementType;
  DEGREE_COLORS: Record<string, string>;
  englishCourses: EnglishCourse[];
  num: (value: number | null | undefined) => number | "—";
  txt: (value: string | null | undefined) => string;
  openBulk: (mode: "english") => void;
  openEngEdit: (course: EnglishCourse) => void;
  setDeleteEngCourse: React.Dispatch<React.SetStateAction<{ id: number; name: string } | null>>;
  tableScrollRef: React.RefObject<HTMLDivElement | null>;
}

export function EnglishPanel(props: EnglishPanelProps) {
  const { Button, DEGREE_COLORS, Pencil, Trash2, englishCourses, num, openBulk, openEngEdit, setDeleteEngCourse, tableScrollRef, txt } = props;
  return (
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <p className="text-sm text-muted-foreground">{englishCourses.length} course{englishCourses.length !== 1 ? "s" : ""} with English test requirements</p>
            <Button size="sm" variant="outline" onClick={() => openBulk("english")} className="gap-1.5 text-purple-700 border-purple-200 hover:bg-purple-50">
              <Pencil className="w-3.5 h-3.5" /> Bulk Edit English
            </Button>
          </div>
          <div ref={tableScrollRef} className="border rounded-xl overflow-auto" style={{ maxHeight: "70vh" }}>
            <table className="text-xs whitespace-nowrap border-collapse w-full">
              <thead className="bg-gray-50 sticky top-0 z-10">
                <tr className="text-[10px] font-bold text-gray-500 uppercase tracking-wide border-b">
                  <th className="text-left px-4 py-2 border-r" colSpan={3}>Course</th>
                  <th className="text-center px-2 py-2 border-r" colSpan={5} style={{ background: "#fdf4ff", color: "#7e22ce" }}>IELTS</th>
                  <th className="text-center px-2 py-2 border-r" colSpan={5} style={{ background: "#fff7ed", color: "#c2410c" }}>PTE</th>
                  <th className="text-center px-2 py-2 border-r" colSpan={5} style={{ background: "#fef2f2", color: "#be123c" }}>TOEFL</th>
                  <th className="text-center px-2 py-2" colSpan={6} style={{ background: "#fdf2f8", color: "#be185d" }}>Other English Test</th>
                  <th className="px-2 py-2" />
                </tr>
                <tr className="border-b bg-gray-50">
                  <th className="px-2 py-2 text-center font-semibold text-gray-500 min-w-[40px]">SN.</th>
                  <th className="text-left px-4 py-2 font-semibold text-gray-700 min-w-[240px]">Course Name</th>
                  <th className="text-left px-2 py-2 font-semibold text-gray-600 min-w-[100px] border-r">Degree Level</th>
                  <th className="px-3 py-2 text-purple-700 font-semibold">L</th>
                  <th className="px-3 py-2 text-purple-700 font-semibold">S</th>
                  <th className="px-3 py-2 text-purple-700 font-semibold">W</th>
                  <th className="px-3 py-2 text-purple-700 font-semibold">R</th>
                  <th className="px-3 py-2 text-purple-700 font-bold border-r">Overall</th>
                  <th className="px-3 py-2 text-orange-600 font-semibold">L</th>
                  <th className="px-3 py-2 text-orange-600 font-semibold">S</th>
                  <th className="px-3 py-2 text-orange-600 font-semibold">W</th>
                  <th className="px-3 py-2 text-orange-600 font-semibold">R</th>
                  <th className="px-3 py-2 text-orange-600 font-bold border-r">Overall</th>
                  <th className="px-3 py-2 text-rose-600 font-semibold">L</th>
                  <th className="px-3 py-2 text-rose-600 font-semibold">S</th>
                  <th className="px-3 py-2 text-rose-600 font-semibold">W</th>
                  <th className="px-3 py-2 text-rose-600 font-semibold">R</th>
                  <th className="px-3 py-2 text-rose-600 font-bold border-r">Overall</th>
                  <th className="px-3 py-2 text-pink-600 font-semibold min-w-[80px]">Test</th>
                  <th className="px-3 py-2 text-pink-500 font-semibold">R</th>
                  <th className="px-3 py-2 text-pink-500 font-semibold">L</th>
                  <th className="px-3 py-2 text-pink-500 font-semibold">S</th>
                  <th className="px-3 py-2 text-pink-500 font-semibold">W</th>
                  <th className="px-3 py-2 text-pink-600 font-bold">Overall</th>
                  <th className="px-2 py-2 text-gray-500 font-semibold min-w-[80px]">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {englishCourses.length === 0 ? (
                  <tr><td colSpan={25} className="text-center py-12 text-muted-foreground">No English test requirements found</td></tr>
                ) : englishCourses.map((c, idx) => (
                  <tr key={c.id} className="hover:bg-blue-50/30">
                    <td className="px-2 py-2 text-center text-gray-400 font-mono text-[11px] min-w-[40px]">{idx + 1}</td>
                    <td className="px-4 py-2 font-medium text-blue-700">
                      <span className="line-clamp-1">{c.name}</span>
                    </td>
                    <td className="px-2 py-2 border-r">
                      {c.degreeLevel ? (
                        <span className={`inline-flex px-1.5 py-0.5 rounded text-[10px] font-semibold ${DEGREE_COLORS[c.degreeLevel] ?? "bg-gray-100 text-gray-600"}`}>
                          {c.degreeLevel}
                        </span>
                      ) : "—"}
                    </td>
                    <td className="px-3 py-2 text-center text-purple-700">{num(c.ieltsListening)}</td>
                    <td className="px-3 py-2 text-center text-purple-700">{num(c.ieltsSpeaking)}</td>
                    <td className="px-3 py-2 text-center text-purple-700">{num(c.ieltsWriting)}</td>
                    <td className="px-3 py-2 text-center text-purple-700">{num(c.ieltsReading)}</td>
                    <td className="px-3 py-2 text-center text-purple-700 font-bold border-r">{num(c.ieltsOverall)}</td>
                    <td className="px-3 py-2 text-center text-orange-600">{num(c.pteListening)}</td>
                    <td className="px-3 py-2 text-center text-orange-600">{num(c.pteSpeaking)}</td>
                    <td className="px-3 py-2 text-center text-orange-600">{num(c.pteWriting)}</td>
                    <td className="px-3 py-2 text-center text-orange-600">{num(c.pteReading)}</td>
                    <td className="px-3 py-2 text-center text-orange-600 font-bold border-r">{num(c.pteOverall)}</td>
                    <td className="px-3 py-2 text-center text-rose-600">{num(c.toeflListening)}</td>
                    <td className="px-3 py-2 text-center text-rose-600">{num(c.toeflSpeaking)}</td>
                    <td className="px-3 py-2 text-center text-rose-600">{num(c.toeflWriting)}</td>
                    <td className="px-3 py-2 text-center text-rose-600">{num(c.toeflReading)}</td>
                    <td className="px-3 py-2 text-center text-rose-600 font-bold border-r">{num(c.toeflOverall)}</td>
                    <td className="px-3 py-2 text-pink-600">{txt(c.otherEnglishTestName)}</td>
                    <td className="px-3 py-2 text-center text-pink-500">{num(c.otherEnglishReading)}</td>
                    <td className="px-3 py-2 text-center text-pink-500">{num(c.otherEnglishListening)}</td>
                    <td className="px-3 py-2 text-center text-pink-500">{num(c.otherEnglishSpeaking)}</td>
                    <td className="px-3 py-2 text-center text-pink-500">{num(c.otherEnglishWriting)}</td>
                    <td className="px-3 py-2 text-center text-pink-600 font-bold">{num(c.otherEnglishOverall)}</td>
                    <td className="px-2 py-2">
                      <div className="flex gap-1">
                        <button onClick={() => openEngEdit(c)} className="p-1 rounded hover:bg-blue-50 text-blue-600 cursor-pointer" title="Edit"><Pencil className="w-3.5 h-3.5" /></button>
                        <button onClick={() => setDeleteEngCourse({ id: c.id, name: c.name })} className="p-1 rounded hover:bg-red-50 text-red-500 cursor-pointer" title="Delete"><Trash2 className="w-3.5 h-3.5" /></button>
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
