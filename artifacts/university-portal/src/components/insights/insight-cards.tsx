export type InsightField = { label: string; value: string; badge: string | null };
export type InsightCard = { title: string; emoji?: string | null; fields?: InsightField[] | null; sections?: { label: string; fields?: InsightField[] | null }[] | null };

/** Read-only renderer for structured Key Insight cards (shared by admin and public views). */
export function InsightCardsGrid({ cards }: { cards: InsightCard[] }) {
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
                      {cards.map((card, ci) => {
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
}
