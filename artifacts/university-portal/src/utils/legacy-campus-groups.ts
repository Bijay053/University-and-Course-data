type Row = Record<string, unknown>;

export type LogicalCourseGroup<T extends Row> = {
  course: T;
  members: T[];
  ids: number[];
};

export type CampusReviewFee = {
  location: string;
  amount: number | null;
  currency: string;
  term: string;
  year: unknown;
};

function record(value: unknown): Row | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Row
    : null;
}

function read(row: Row, camel: string, snake: string): unknown {
  return row[camel] ?? row[snake];
}

function normalized(value: unknown): string {
  return typeof value === "string" ? value.trim().toLowerCase().replace(/\s+/g, " ") : "";
}

function extraction(row: Row): Row | null {
  return record(read(row, "extractionMethod", "extraction_method"));
}

function campusScope(row: Row): Row | null {
  return record(extraction(row)?.campus_fee_scope ?? extraction(row)?.campusFeeScope);
}

function selectedVariants(row: Row): string[] {
  const feeVariants = record(extraction(row)?.fee_variants ?? extraction(row)?.feeVariants);
  const selected = Array.isArray(feeVariants?.selected) ? feeVariants.selected : [];
  return [...new Set(selected.map(value => {
    const option = record(value);
    return normalized(option?.study_variant ?? option?.studyVariant);
  }).filter(Boolean))].sort();
}

function selectedFeeCohort(row: Row): { year: unknown; period: string; currency: string } {
  const feeVariants = record(extraction(row)?.fee_variants ?? extraction(row)?.feeVariants);
  const selected = Array.isArray(feeVariants?.selected) ? feeVariants.selected : [];
  const option = selected.length === 1 ? record(selected[0]) : null;
  return {
    year: read(row, "feeYear", "fee_year") ?? option?.year ?? null,
    period: normalized(read(row, "feeTerm", "fee_term") ?? option?.period),
    currency: normalized(row.currency ?? option?.currency),
  };
}

function compatibilityKey(row: Row): string | null {
  const scope = campusScope(row);
  const splitFromId = scope?.split_from_id ?? scope?.splitFromId;
  const originalName = normalized(scope?.original_name ?? scope?.originalName);
  const universityId = read(row, "universityId", "university_id");
  const jobId = read(row, "scrapeJobId", "scrape_job_id") ?? read(row, "jobId", "job_id");
  const sourceUrl = read(row, "courseWebsite", "course_website") ?? read(row, "sourceUrl", "source_url");
  const degree = normalized(read(row, "degreeLevel", "degree_level"));
  const variants = selectedVariants(row);
  if (splitFromId == null || !originalName || universityId == null || jobId == null
    || typeof sourceUrl !== "string" || !sourceUrl.trim() || !degree || variants.length !== 1) return null;

  // Campus siblings must be the same explicit legacy split, source run, source
  // page, award, selected route, and billing cohort. A shared title or URL
  // alone is deliberately insufficient.
  const feeCohort = selectedFeeCohort(row);
  const duration = read(row, "duration", "duration");
  const durationTerm = normalized(read(row, "durationTerm", "duration_term"));
  const studyMode = normalized(read(row, "studyMode", "study_mode"));
  const source = sourceUrl.trim().replace(/#.*$/, "").replace(/\/+$/, "").toLowerCase();
  return JSON.stringify([
    String(splitFromId), originalName, String(universityId), String(jobId), source,
    degree, variants[0], feeCohort.year, feeCohort.period, feeCohort.currency,
    duration ?? null, durationTerm, studyMode, normalized(row.status),
  ]);
}

/**
 * Collapses only explicitly-linked legacy campus splits. Unlinked, incomplete,
 * or cohort-ambiguous rows remain individual logical courses.
 */
export function groupLegacyCampusRows<T extends { id: number }>(rows: T[]): LogicalCourseGroup<T>[] {
  const keyed = new Map<string, T[]>();
  const ungrouped: T[] = [];
  for (const row of rows) {
    const key = compatibilityKey(row as Row);
    if (!key) {
      ungrouped.push(row);
      continue;
    }
    const bucket = keyed.get(key) ?? [];
    bucket.push(row);
    keyed.set(key, bucket);
  }
  const groups: LogicalCourseGroup<T>[] = [...ungrouped.map(course => ({ course, members: [course], ids: [course.id] }))];
  for (const members of keyed.values()) {
    if (members.length < 2) {
      const course = members[0];
      groups.push({ course, members, ids: [course.id] });
    } else {
      const sorted = [...members].sort((a, b) => a.id - b.id);
      groups.push({ course: sorted[0], members: sorted, ids: sorted.map(row => row.id) });
    }
  }
  return groups.sort((a, b) => a.course.id - b.course.id);
}

export function legacyCampusGroupName(members: Row[]): string | null {
  if (members.length < 2) return null;
  const names = members.map(member => campusScope(member)?.original_name ?? campusScope(member)?.originalName);
  return names.every(name => typeof name === "string" && normalized(name) === normalized(names[0]))
    ? names[0] as string : null;
}

/** The selected options describe fee regions, not additional campus rows.
 * Each split row owns its persisted fee and its explicit scoped locations. */
export function legacyCampusReviewFees(members: Row[]): CampusReviewFee[] {
  const seen = new Set<string>();
  const result: CampusReviewFee[] = [];
  for (const member of members) {
    const scoped = campusScope(member)?.locations;
    const locations = Array.isArray(scoped) && scoped.length
      && scoped.every(location => typeof location === "string" && location.trim())
      ? scoped as string[]
      : [read(member, "courseLocation", "course_location")];
    const amount = read(member, "internationalFee", "international_fee");
    const fee: Omit<CampusReviewFee, "location"> = {
      amount: typeof amount === "number" && Number.isFinite(amount) ? amount : null,
      currency: String(member.currency ?? "AUD"),
      term: String(read(member, "feeTerm", "fee_term") ?? ""),
      year: read(member, "feeYear", "fee_year"),
    };
    for (const value of locations) {
      const location = typeof value === "string" && value.trim() ? value.trim() : "Location unresolved";
      const key = JSON.stringify([normalized(location), fee.amount, fee.currency, fee.term, fee.year]);
      if (!seen.has(key)) {
        seen.add(key);
        result.push({ location, ...fee });
      }
    }
  }
  return result;
}
