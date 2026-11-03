import { Slider } from "@/components/ui/slider";

export type RangePreset = { label: string; range: [number, number] };
type Props = {
  idPrefix: string; unit: string; value: [number, number];
  sliderMax: number; inputMax: number; step: number; presets: RangePreset[];
  disabled?: boolean; onChange: (next: [number, number]) => void;
};

const inputCls = "w-full h-9 rounded-md border border-[hsl(var(--sic-line))] bg-white/90 px-2 text-sm tabular-nums disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary";

/** Min/max inputs + slider + presets, mirroring the staff search range controls. */
export function RangeFilter({ idPrefix, unit, value, sliderMax, inputMax, step, presets, disabled, onChange }: Props) {
  const [lo, hi] = value;
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2">
        <label className="block text-[11px] text-[hsl(var(--sic-muted))]">Min{unit ? ` (${unit})` : ""}
          <input type="number" min={0} max={hi} step={step} value={lo} disabled={disabled} data-testid={`input-${idPrefix}-min`}
            onChange={(e) => onChange([Math.max(0, Math.min(hi, Number(e.target.value) || 0)), hi])} className={`${inputCls} mt-1`} />
        </label>
        <label className="block text-[11px] text-[hsl(var(--sic-muted))]">Max{unit ? ` (${unit})` : ""}
          <input type="number" min={lo} max={inputMax} step={step} value={hi} disabled={disabled} data-testid={`input-${idPrefix}-max`}
            onChange={(e) => onChange([lo, Math.min(inputMax, Math.max(lo, Number(e.target.value) || 0))])} className={`${inputCls} mt-1`} />
        </label>
      </div>
      <Slider value={[Math.min(lo, sliderMax), Math.min(hi, sliderMax)]} min={0} max={sliderMax} step={step} disabled={disabled}
        aria-label={`${idPrefix} range`} data-testid={`slider-${idPrefix}`}
        onValueChange={(v) => onChange([v[0], v[1] >= sliderMax && hi > sliderMax ? hi : v[1]])} />
      <div className="flex flex-wrap gap-1.5">
        {presets.map((p) => {
          const on = lo === p.range[0] && hi === p.range[1];
          return (
            <button key={p.label} type="button" disabled={disabled} aria-pressed={on} onClick={() => onChange(p.range)} data-testid={`preset-${idPrefix}-${p.range.join("-")}`}
              className={`text-[11px] px-2 py-1 rounded-full border transition-colors disabled:opacity-50 ${on ? "bg-primary text-primary-foreground border-primary" : "bg-white text-[hsl(var(--sic-ink))] border-[hsl(var(--sic-line))] hover:border-primary hover:text-primary"}`}>
              {p.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}
