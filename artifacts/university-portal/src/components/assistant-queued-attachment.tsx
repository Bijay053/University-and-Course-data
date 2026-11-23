import { useEffect, useState } from "react";
import { FileText, Loader2, X } from "lucide-react";
import type { QueuedAttachment } from "./use-assistant-attachments";

export function AssistantQueuedAttachment({ attachment, remove, disabled }: {
  attachment: QueuedAttachment; remove: () => void; disabled: boolean;
}) {
  const [preview, setPreview] = useState<string | null>(null);
  useEffect(() => {
    if (!attachment.file.type.startsWith("image/") || !URL.createObjectURL) return;
    const url = URL.createObjectURL(attachment.file); setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [attachment.file]);
  return <div className={`mb-2 flex items-center gap-2 rounded-xl border px-3 py-1.5 text-[12px] ${
    attachment.status === "error" ? "border-red-200 bg-red-50 text-red-800" : "border-teal-200 bg-teal-50/60 text-teal-950"
  }`} data-testid="assistant-attachment" data-status={attachment.status}>
    {attachment.status === "uploading" ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : <FileText className="h-3.5 w-3.5" aria-hidden />}
    {preview && <img src={preview} alt={`Preview: ${attachment.name}`} className="h-12 w-12 rounded object-cover" />}
    <span className="min-w-0 flex-1 truncate">{attachment.name}</span>
    <span role="status" data-testid="text-attachment-status">
      {attachment.status === "uploading" ? "Uploading" : attachment.status === "ready"
        ? `${(attachment.size / 1_000_000).toFixed(2)} MB · Ready` : attachment.error}
    </span>
    <button type="button" onClick={remove} aria-label="Remove attachment"
      title={`Remove ${attachment.name}`} disabled={disabled} data-testid="button-remove-attachment">
      <X className="h-3.5 w-3.5" aria-hidden />
    </button>
  </div>;
}
