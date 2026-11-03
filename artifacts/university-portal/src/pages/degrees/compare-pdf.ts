import { jsPDF } from "jspdf";
import { autoTable } from "jspdf-autotable";
import { BASE, type CompareCourse, safeHttpUrl } from "./lib";
import { comparisonRows } from "./comparison-data";

let fonts: Promise<string[]> | null = null;
function loadFonts() {
  if (!fonts) fonts = Promise.all(["regular", "bold"].map(async (weight) => {
    const response = await fetch(`${BASE}/fonts/lato-${weight}.ttf`);
    if (!response.ok) throw new Error("The PDF font could not be loaded.");
    const bytes = new Uint8Array(await response.arrayBuffer());
    let binary = "";
    for (let i = 0; i < bytes.length; i += 16384) binary += String.fromCharCode(...bytes.subarray(i, i + 16384));
    return btoa(binary);
  })).catch((error) => { fonts = null; throw error; });
  return fonts;
}

/** Text-based, paginated PDF: preserves published currencies and periods. */
export async function createComparisonPdf(courses: CompareCourse[], requestedCount = courses.length, now = new Date()) {
  if (!courses.length) throw new Error("Load at least one course before downloading.");
  const [regular, bold] = await loadFonts();
  const doc = new jsPDF({ orientation: "landscape", unit: "mm", format: "a4", compress: true });
  doc.addFileToVFS("Lato-Regular.ttf", regular);
  doc.addFileToVFS("Lato-Bold.ttf", bold);
  doc.addFont("Lato-Regular.ttf", "Lato", "normal");
  doc.addFont("Lato-Bold.ttf", "Lato", "bold");
  const width = doc.internal.pageSize.getWidth();
  const rows = comparisonRows(courses);
  const omitted = Math.max(0, requestedCount - courses.length);
  doc.setProperties({ title: "Course comparison | Study Info Centre", author: "Study Info Centre", subject: "Published course fees and entry requirements" });
  autoTable(doc, {
    head: [["Detail", ...courses.map((c) => c.course_name)]],
    body: rows.map((r) => [r.label, ...r.values]),
    theme: "grid",
    margin: { top: 37, right: 12, bottom: 20, left: 12 },
    styles: { font: "Lato", fontSize: 8, cellPadding: 2.5, overflow: "linebreak", valign: "top", lineColor: [224, 227, 233], lineWidth: 0.15 },
    headStyles: { fillColor: [17, 22, 40], textColor: 255, fontStyle: "bold" },
    columnStyles: { 0: { cellWidth: 31, fontStyle: "bold", fillColor: [245, 246, 249] } },
    rowPageBreak: "avoid",
    showHead: "everyPage",
    didDrawPage: () => {
      doc.setFillColor(17, 22, 40);
      doc.rect(0, 0, width, 25, "F");
      doc.setTextColor(255);
      doc.setFont("Lato", "bold");
      doc.setFontSize(16);
      doc.text("Course comparison", 12, 12);
      doc.setFont("Lato", "normal");
      doc.setFontSize(8);
      doc.text("Study Info Centre | Course Toolkit", 12, 19);
      doc.setTextColor(70);
      doc.text("Fees are shown in their published currency and period, without conversion. Confirm details with the university.", 12, 31);
    },
    didDrawCell: (data) => {
      if (data.section !== "body" || data.row.index !== rows.length - 1 || data.column.index < 1) return;
      const url = safeHttpUrl(courses[data.column.index - 1]?.course_url);
      if (url) doc.link(data.cell.x, data.cell.y, data.cell.width, data.cell.height, { url });
    },
  });
  const pages = doc.getNumberOfPages();
  for (let page = 1; page <= pages; page++) {
    doc.setPage(page);
    doc.setTextColor(100);
    doc.setFont("Lato", "normal");
    doc.setFontSize(7);
    const bottom = doc.internal.pageSize.getHeight() - 10;
    doc.text(`Generated ${now.toISOString().slice(0, 10)}${omitted ? ` | ${omitted} unavailable shortlisted course(s) omitted.` : ""}`, 12, bottom);
    doc.text(`Page ${page} of ${pages}`, width - 12, bottom, { align: "right" });
  }
  return doc;
}
