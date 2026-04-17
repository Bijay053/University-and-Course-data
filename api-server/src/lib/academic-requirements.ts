export type AcademicCountryRequirement = {
  country: string | null;
  academicLevel: string | null;
  academicScore: string | null;
  scoreType: string | null;
  otherRequirement: string | null;
  confidence: number;
  source: "static" | "browser" | "shared" | "pdf" | "ai" | "none";
};

export type AcademicRequirementEngineResult = {
  rows: AcademicCountryRequirement[];
  generalRequirement: {
    academicLevel: string | null;
    academicScore: string | null;
    scoreType: string | null;
    otherRequirement: string | null;
    confidence: number;
    source: "static" | "browser" | "shared" | "pdf" | "ai" | "none";
  } | null;
};

export function normalizeWhitespace(input: string | null | undefined): string {
  return (input || "").replace(/\s+/g, " ").trim();
}

export function normalizeAcademicText(input: string | null | undefined): string {
  return normalizeWhitespace(input)
    .replace(/[•●▪◦]/g, " ")
    .replace(/\u00a0/g, " ")
    .trim();
}

export const COUNTRY_SYNONYMS: Record<string, string[]> = {
  Nepal: ["nepal", "nepalese"],
  Bangladesh: ["bangladesh", "bangladeshi"],
  India: ["india", "indian"],
  Pakistan: ["pakistan", "pakistani"],
  "Sri Lanka": ["sri lanka", "sri lankan"],
  Bhutan: ["bhutan", "bhutanese"],
  China: ["china", "chinese"],
  Vietnam: ["vietnam", "vietnamese"],
  Philippines: ["philippines", "philippine", "filipino"],
  Kenya: ["kenya", "kenyan"],
  Nigeria: ["nigeria", "nigerian"],
  Ghana: ["ghana", "ghanaian"],
  UAE: ["uae", "united arab emirates"],
  UK: ["uk", "united kingdom", "britain", "british"],
  USA: ["usa", "united states", "us", "american"],
  Canada: ["canada", "canadian"],
  Australia: ["australia", "australian"],
  "New Zealand": ["new zealand", "new zealander"],
};

export function detectCountriesInText(textRaw: string): string[] {
  const text = normalizeAcademicText(textRaw).toLowerCase();
  const found: string[] = [];
  for (const [country, synonyms] of Object.entries(COUNTRY_SYNONYMS)) {
    if (synonyms.some((s) => text.includes(s))) found.push(country);
  }
  return [...new Set(found)];
}

export function detectAcademicLevel(textRaw: string): string | null {
  const text = normalizeAcademicText(textRaw).toLowerCase();
  const patterns: Array<[RegExp, string]> = [
    [/\bgrade\s*12\b|\byear\s*12\b/, "Year 12"],
    [/\bhsc\b|\bhigher secondary\b|\bhigher secondary certificate\b/, "Higher Secondary"],
    [/\ba level\b|\bgce a level\b/, "A Level"],
    [/\bo level\b|\bgce o level\b/, "O Level"],
    [/\bib\b|\binternational baccalaureate\b/, "IB"],
    [/\bbachelor(?:'s)? degree\b|\bcompleted bachelor\b|\bundergraduate degree\b/, "Bachelor Degree"],
    [/\bmaster(?:'s)? degree\b/, "Master Degree"],
    [/\bdiploma\b/, "Diploma"],
    [/\bcertificate\b/, "Certificate"],
  ];
  for (const [regex, label] of patterns) {
    if (regex.test(text)) return label;
  }
  return null;
}

export function extractGpa(textRaw: string): { score: string | null; scoreType: string | null } {
  const text = normalizeAcademicText(textRaw).toLowerCase();
  let m = text.match(/\bgpa\s*(?:of|out of|:)?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:out of\s*([0-9]+(?:\.[0-9]+)?))?/i);
  if (m) return { score: m[1], scoreType: m[2] ? `GPA out of ${m[2]}` : "GPA" };
  m = text.match(/([0-9]+(?:\.[0-9]+)?)\s*gpa\s*(?:out of\s*([0-9]+(?:\.[0-9]+)?))?/i);
  if (m) return { score: m[1], scoreType: m[2] ? `GPA out of ${m[2]}` : "GPA" };
  m = text.match(/cgpa\s*(?:of|out of|:)?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:out of\s*([0-9]+(?:\.[0-9]+)?))?/i);
  if (m) return { score: m[1], scoreType: m[2] ? `CGPA out of ${m[2]}` : "CGPA" };
  return { score: null, scoreType: null };
}

export function extractPercentage(textRaw: string): { score: string | null; scoreType: string | null } {
  const text = normalizeAcademicText(textRaw).toLowerCase();
  const m = text.match(/([0-9]{2,3}(?:\.[0-9]+)?)\s*%/) || text.match(/\b([0-9]{2,3}(?:\.[0-9]+)?)\s*percent\b/i);
  if (m) return { score: m[1], scoreType: "Percentage" };
  return { score: null, scoreType: null };
}

export function extractDivision(textRaw: string): { score: string | null; scoreType: string | null } {
  const text = normalizeAcademicText(textRaw).toLowerCase();
  const m = text.match(/\b(first|second|third)\s+division\b/i);
  if (m) return { score: m[1], scoreType: "Division" };
  return { score: null, scoreType: null };
}

export function extractLetterGrade(textRaw: string): { score: string | null; scoreType: string | null } {
  const text = normalizeAcademicText(textRaw);
  const m = text.match(/\b([ABCDEF]{2,4})\b/);
  if (m) return { score: m[1], scoreType: "Letter Grade" };
  return { score: null, scoreType: null };
}

export function extractSubjectRequirements(textRaw: string): string | null {
  const text = normalizeAcademicText(textRaw).toLowerCase();
  const keywords = [
    "mathematics",
    "maths",
    "math",
    "science",
    "chemistry",
    "physics",
    "biology",
    "english",
    "accounting",
    "business",
    "computer science",
    "programming",
  ];
  const hits = keywords.filter((k) => text.includes(k));
  return hits.length ? [...new Set(hits)].join(", ") : null;
}

export function extractEquivalentRequirement(textRaw: string): string | null {
  const text = normalizeAcademicText(textRaw).toLowerCase();
  const phrases = [
    "or equivalent",
    "recognized institution",
    "equivalent qualification",
    "relevant bachelor degree",
    "completed bachelor degree",
    "qualification deemed equivalent",
  ];
  const hits = phrases.filter((p) => text.includes(p));
  return hits.length ? hits.join("; ") : null;
}

export function parseAcademicRequirementBlock(
  textRaw: string,
  source: AcademicCountryRequirement["source"],
  forcedCountry: string | null = null,
): AcademicCountryRequirement {
  const text = normalizeAcademicText(textRaw);
  const gpa = extractGpa(text);
  const percentage = extractPercentage(text);
  const division = extractDivision(text);
  const letter = extractLetterGrade(text);
  const level = detectAcademicLevel(text);
  const countries = forcedCountry ? [forcedCountry] : detectCountriesInText(text);
  const subjectReq = extractSubjectRequirements(text);
  const equivalentReq = extractEquivalentRequirement(text);
  const scoreData = gpa.score
    ? gpa
    : percentage.score
      ? percentage
      : division.score
        ? division
        : letter.score
          ? letter
          : { score: null, scoreType: null };
  const otherReqParts = [subjectReq, equivalentReq].filter(Boolean);
  return {
    country: countries[0] || forcedCountry || null,
    academicLevel: level,
    academicScore: scoreData.score,
    scoreType: scoreData.scoreType,
    otherRequirement: otherReqParts.length ? otherReqParts.join("; ") : null,
    confidence:
      (countries.length || forcedCountry ? 25 : 0) +
      (level ? 25 : 0) +
      (scoreData.score ? 30 : 0) +
      (otherReqParts.length ? 10 : 0),
    source,
  };
}

export function splitAcademicRequirementBlocks(textRaw: string): string[] {
  const text = normalizeAcademicText(textRaw);
  return text
    .split(/(?=(?:Nepal|Bangladesh|India|Pakistan|Sri Lanka|China|Vietnam|Philippines|Kenya|Nigeria|Ghana|Canada|USA|UK|Australia|New Zealand)\b)/i)
    .map((s) => s.trim())
    .filter(Boolean);
}

export function parseAcademicRequirementsFromText(
  textRaw: string,
  source: AcademicCountryRequirement["source"],
): AcademicRequirementEngineResult {
  const text = normalizeAcademicText(textRaw);
  const blocks = splitAcademicRequirementBlocks(text);
  const rows: AcademicCountryRequirement[] = [];
  for (const block of blocks) {
    const countries = detectCountriesInText(block);
    if (countries.length) {
      for (const country of countries) {
        const row = parseAcademicRequirementBlock(block, source, country);
        if (row.academicLevel || row.academicScore || row.otherRequirement) rows.push(row);
      }
    }
  }
  let generalRequirement: AcademicRequirementEngineResult["generalRequirement"] = null;
  if (!rows.length) {
    const general = parseAcademicRequirementBlock(text, source, null);
    if (general.academicLevel || general.academicScore || general.otherRequirement) {
      generalRequirement = {
        academicLevel: general.academicLevel,
        academicScore: general.academicScore,
        scoreType: general.scoreType,
        otherRequirement: general.otherRequirement,
        confidence: general.confidence,
        source,
      };
    }
  }
  return { rows, generalRequirement };
}

function requirementCompletenessScore(row: AcademicCountryRequirement): number {
  return (
    (row.country ? 20 : 0) +
    (row.academicLevel ? 20 : 0) +
    (row.academicScore ? 20 : 0) +
    (row.scoreType ? 15 : 0) +
    (row.otherRequirement ? 10 : 0) +
    (row.confidence || 0)
  );
}

export function mergeAcademicRequirementRows(
  baseRows: AcademicCountryRequirement[],
  incomingRows: AcademicCountryRequirement[],
): AcademicCountryRequirement[] {
  const map = new Map<string, AcademicCountryRequirement>();
  for (const row of [...baseRows, ...incomingRows]) {
    const key = (row.country || "general").toLowerCase();
    const existing = map.get(key);
    if (!existing || requirementCompletenessScore(row) > requirementCompletenessScore(existing)) {
      map.set(key, row);
    }
  }
  return Array.from(map.values());
}

export function mergeAcademicRequirementResults(
  base: AcademicRequirementEngineResult,
  incoming: AcademicRequirementEngineResult,
): AcademicRequirementEngineResult {
  const rows = mergeAcademicRequirementRows(base.rows, incoming.rows);
  let generalRequirement = base.generalRequirement;
  if (incoming.generalRequirement) {
    const incomingScore = requirementCompletenessScore({
      country: null,
      academicLevel: incoming.generalRequirement.academicLevel,
      academicScore: incoming.generalRequirement.academicScore,
      scoreType: incoming.generalRequirement.scoreType,
      otherRequirement: incoming.generalRequirement.otherRequirement,
      confidence: incoming.generalRequirement.confidence,
      source: incoming.generalRequirement.source,
    });
    const baseScore = generalRequirement
      ? requirementCompletenessScore({
          country: null,
          academicLevel: generalRequirement.academicLevel,
          academicScore: generalRequirement.academicScore,
          scoreType: generalRequirement.scoreType,
          otherRequirement: generalRequirement.otherRequirement,
          confidence: generalRequirement.confidence,
          source: generalRequirement.source,
        })
      : -1;
    if (incomingScore > baseScore) generalRequirement = incoming.generalRequirement;
  }
  return { rows, generalRequirement };
}

export function pickPrimaryAcademicRequirement(rows: AcademicCountryRequirement[]): AcademicCountryRequirement | null {
  if (!rows.length) return null;
  const priorityCountries = ["Nepal", "Bangladesh", "India", "Pakistan", "Sri Lanka"];
  for (const country of priorityCountries) {
    const found = rows.find((r) => r.country === country);
    if (found) return found;
  }
  return rows.sort((a, b) => requirementCompletenessScore(b) - requirementCompletenessScore(a))[0] || null;
}
