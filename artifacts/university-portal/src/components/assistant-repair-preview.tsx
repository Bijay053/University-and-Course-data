import { safeSourceUrl } from "./assistant-safe-url";

export type RepairCourseScope = { id: number; name: string; url: string };
export type RepairPreview = RepairCourseScope & {
  status: "supported" | "unverified";
  reason: string;
  changes: { field: string; oldValue: number | string | null; newValue: number | string }[];
  source?: { url: string; quote: string };
};
const labels: Record<string, string> = {
  ielts_overall: "IELTS overall", ielts_listening: "Listening",
  ielts_reading: "Reading", ielts_writing: "Writing", ielts_speaking: "Speaking",
  study_mode: "Study mode",
};

export function AssistantRepairPreview({ scope, previews, testId, applied = false }: {
  scope: RepairCourseScope[]; previews?: RepairPreview[]; testId: string; applied?: boolean;
}) {
  return <div className="mt-3 space-y-3 text-[12px]" data-testid={testId}>
    <p className="font-medium">Exact course scope: {scope.length}</p>
    <div className="max-h-80 space-y-2 overflow-y-auto pr-1" role="region"
      aria-label="Courses and proposed field values" tabIndex={0}>
    {scope.map(course => {
      const preview = previews?.find(p => p.id === course.id);
      const href = safeSourceUrl(course.url);
      const sourceHref = preview?.source && safeSourceUrl(preview.source.url);
      return <details key={course.id} open={scope.length <= 3}
        className="rounded-md border border-amber-200 bg-white p-2">
        <summary className="cursor-pointer font-medium">
          {course.name} <span className="font-normal text-slate-500">· #{course.id}</span>
        </summary>
        {href && <a href={href} target="_blank" rel="noopener noreferrer nofollow"
          className="mt-1 block break-all text-teal-800 underline">Official course</a>}
        {preview?.status === "supported" ? <>
          <div className="mt-2 overflow-x-auto">
            <table className="w-full text-left" aria-label={`${course.name} field comparison`}>
              <thead><tr className="border-b">
                <th className="py-1 pr-3">Field</th>
                <th className="py-1 pr-3">{applied ? "Previous" : "Current"}</th>
                <th className="py-1 pr-3">{applied ? "Saved" : "Proposed"}</th>
                <th className="py-1">Change</th>
              </tr></thead>
              <tbody>{preview.changes.map(change => <tr key={change.field} className="border-b last:border-0">
                <th scope="row" className="py-1 pr-3 font-normal">{labels[change.field] ?? change.field}</th>
                <td className="py-1 pr-3">{typeof change.oldValue === "number" ? change.oldValue.toFixed(1) : change.oldValue ?? "Missing"}</td>
                <td className="py-1 pr-3 font-medium">{typeof change.newValue === "number" ? change.newValue.toFixed(1) : change.newValue}</td>
                <td className="py-1">{change.oldValue === change.newValue ? "Unchanged" : applied ? "Updated" : "Proposed"}</td>
              </tr>)}</tbody>
            </table>
          </div>
          {preview.source && <blockquote className="mt-2 border-l-2 border-teal-300 pl-2 text-slate-600">
            {sourceHref && <a href={sourceHref} target="_blank" rel="noopener noreferrer nofollow"
              className="text-teal-800 underline">Source evidence</a>}
            <p className="mt-1 whitespace-pre-wrap">{preview.source.quote}</p>
          </blockquote>}
        </> : preview && <p className="mt-2 text-amber-900">Unverified: {preview.reason}</p>}
      </details>;
    })}
    </div>
    {previews?.length ? <p className="text-slate-600">
      {applied ? "These changes were saved and read back. No courses were published."
        : "These are proposals, not applied changes. Execution follows the action’s confirmation rules and must verify saved values. Publication remains separate."}
    </p> : null}
  </div>;
}
