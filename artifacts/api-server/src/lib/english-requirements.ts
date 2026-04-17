/**
 * Universal English Requirements Engine
 *
 * Single, reusable parser for extracting and normalising English test
 * requirements (IELTS, PTE, TOEFL, CAE/Cambridge, DET/Duolingo, OET, TOEIC)
 * from any text source — static HTML body text, browser-rendered text, or a
 * shared university requirements page.
 *
 * Usage:
 *   const result = parseEnglishRequirementsFromText(bodyText, "browser");
 *   const merged = mergeEnglishResults(existing, result);
 *   applyEnglishResultToCourse(courseData, merged);
 */

// ── Types ─────────────────────────────────────────────────────────────────────

export type BandScore = {
  overall: number | null;
  listening: number | null;
  reading: number | null;
  writing: number | null;
  speaking: number | null;
  /** 0–100: how confident the parser is in this result. */
  confidence: number;
};

export type SimpleScore = {
  overall: number | null;
  confidence: number;
};

export type OtherTest = {
  name: string;
  score: string;
  notes?: string | null;
};

export type EnglishRequirementResult = {
  source: "static" | "browser" | "shared" | "ai" | "none";
  ielts: BandScore;
  pte: BandScore;
  toefl: BandScore;
  cae: BandScore;
  det: SimpleScore;
  otherTests: OtherTest[];
};

// ── Minimal CourseData interface (only the fields we touch) ───────────────────

export interface EnglishCourseFields {
  ieltsOverall?: number | null;
  ieltsListening?: number | null;
  ieltsReading?: number | null;
  ieltsWriting?: number | null;
  ieltsSpeaking?: number | null;
  pteOverall?: number | null;
  pteListening?: number | null;
  pteReading?: number | null;
  pteWriting?: number | null;
  pteSpeaking?: number | null;
  toeflOverall?: number | null;
  toeflListening?: number | null;
  toeflReading?: number | null;
  toeflWriting?: number | null;
  toeflSpeaking?: number | null;
  cambridgeOverall?: number | null;
  duolingoOverall?: number | null;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

export function normalizeWhitespace(input: string | null | undefined): string {
  return (input ?? "").replace(/\s+/g, " ").trim();
}

export function hasKeyword(text: string | null | undefined, keywords: string[]): boolean {
  const t = normalizeWhitespace(text).toLowerCase();
  return keywords.some((k) => t.includes(k.toLowerCase()));
}

/** Quick check — is any English test mentioned in this text at all? */
export function hasEnglishTestKeyword(text: string | null | undefined): boolean {
  return hasKeyword(text, ["ielts", "pte", "toefl", "cambridge", "cae", "duolingo", "det"]);
}

// ── Empty constructors ────────────────────────────────────────────────────────

export function emptyBandScore(): BandScore {
  return { overall: null, listening: null, reading: null, writing: null, speaking: null, confidence: 0 };
}

export function emptySimpleScore(): SimpleScore {
  return { overall: null, confidence: 0 };
}

export function emptyEnglishResult(
  source: EnglishRequirementResult["source"] = "none",
): EnglishRequirementResult {
  return {
    source,
    ielts: emptyBandScore(),
    pte: emptyBandScore(),
    toefl: emptyBandScore(),
    cae: emptyBandScore(),
    det: emptySimpleScore(),
    otherTests: [],
  };
}

// ── IELTS Parser ──────────────────────────────────────────────────────────────

export function parseIelts(rawText: string): BandScore {
  const text = normalizeWhitespace(rawText);
  if (!text.toLowerCase().includes("ielts")) return emptyBandScore();

  // P1: "IELTS[Academic]: Overall X with no [individual] band below Y"
  //     Also handles "IELTS: Overall score X, with no band below Y" (VIT format)
  let m = text.match(
    /ielts(?:\s+academic)?[^a-z0-9]{0,40}overall\s+(?:score\s+)?([\d.]+)[^.]{0,60}?no\s+(?:individual\s+)?band\s+(?:score\s+)?(?:below|less\s+than|lower\s+than)\s*([\d.]+)/i,
  );
  if (m) {
    const overall = Number(m[1]);
    const min = Number(m[2]);
    if (overall >= 4 && overall <= 9 && min >= 4 && min <= 9)
      return { overall, listening: min, reading: min, writing: min, speaking: min, confidence: 95 };
  }

  // P2: "IELTS X.X overall with Y.Y in each band/component/skill"
  m = text.match(
    /ielts(?:\s+academic)?[^a-z0-9]{0,40}([\d.]+)\s*overall[^a-z0-9]{0,40}(?:with\s*)?([\d.]+)\s*(?:in\s+each\s+(?:band|component|skill)|each\s+(?:band|component|skill))/i,
  );
  if (m) {
    const overall = Number(m[1]);
    const each = Number(m[2]);
    if (overall >= 4 && overall <= 9 && each >= 4 && each <= 9)
      return { overall, listening: each, reading: each, writing: each, speaking: each, confidence: 90 };
  }

  // P3: "IELTS overall X with no score less than Y" (alternate phrasing)
  m = text.match(
    /ielts(?:\s+academic)?[^a-z0-9]{0,40}overall\s*([\d.]+)[^.]{0,60}?(?:minimum\s+of|no\s+score\s+less\s+than|no\s+section\s+below)\s*([\d.]+)/i,
  );
  if (m) {
    const overall = Number(m[1]);
    const min = Number(m[2]);
    if (overall >= 4 && overall <= 9 && min >= 4 && min <= 9)
      return { overall, listening: min, reading: min, writing: min, speaking: min, confidence: 90 };
  }

  // P4: All four subscores explicitly listed in order
  m = text.match(
    /ielts(?:\s+academic)?.*?overall\s*([\d.]+).*?listening\s*([\d.]+).*?reading\s*([\d.]+).*?writing\s*([\d.]+).*?speaking\s*([\d.]+)/i,
  );
  if (m) {
    return {
      overall: Number(m[1]),
      listening: Number(m[2]),
      reading: Number(m[3]),
      writing: Number(m[4]),
      speaking: Number(m[5]),
      confidence: 92,
    };
  }

  // P5: overall score near "IELTS" + possibly individual bands nearby
  const overallM = text.match(/ielts(?:\s+academic)?.{0,150}?overall\s*([\d.]+)/i);
  const listenM = text.match(/listening\s*([\d.]+)/i);
  const readM = text.match(/reading\s*([\d.]+)/i);
  const writeM = text.match(/writing\s*([\d.]+)/i);
  const speakM = text.match(/speaking\s*([\d.]+)/i);
  if (overallM) {
    const overall = Number(overallM[1]);
    if (overall >= 4 && overall <= 9) {
      const hasBands = listenM || readM || writeM || speakM;
      return {
        overall,
        listening: listenM ? Number(listenM[1]) : null,
        reading: readM ? Number(readM[1]) : null,
        writing: writeM ? Number(writeM[1]) : null,
        speaking: speakM ? Number(speakM[1]) : null,
        confidence: hasBands ? 80 : 65,
      };
    }
  }

  // P6: broad catch-all — "IELTS minimum 6.0", "IELTS 6.0", "minimum IELTS 6.0",
  //     "IELTS score of 6.0", "IELTS of 6.0", "IELTS 6.0 or higher", "IELTS: 6.5"
  const broadM =
    text.match(/(?:minimum\s+)?ielts(?:\s+academic)?[^a-z0-9]{0,60}?([4-9](?:\.[05])?)/i) ||
    text.match(
      /ielts[^a-z0-9]{0,80}?([4-9](?:\.[05])?)\s*(?:or\s+(?:above|higher|more)|minimum|overall|and\s+above|plus)/i,
    );
  if (broadM) {
    const overall = Number(broadM[1]);
    if (overall >= 4 && overall <= 9) {
      return {
        overall,
        listening: listenM ? Number(listenM[1]) : null,
        reading: readM ? Number(readM[1]) : null,
        writing: writeM ? Number(writeM[1]) : null,
        speaking: speakM ? Number(speakM[1]) : null,
        confidence: 55,
      };
    }
  }

  return emptyBandScore();
}

// ── PTE Parser ────────────────────────────────────────────────────────────────

export function parsePte(rawText: string): BandScore {
  const text = normalizeWhitespace(rawText);
  if (!text.toLowerCase().includes("pte")) return emptyBandScore();

  // P1: "PTE Academic 58 with no communicative skill below 50"
  let m = text.match(
    /pte(?:\s+academic)?[^a-z0-9]{0,40}(?:overall\s*)?([\d.]+)\s*(?:with\s*)?(?:no\s+(?:communicative\s+)?skill\s+below|minimum\s+of|no\s+score\s+less\s+than)\s*([\d.]+)/i,
  );
  if (m) {
    const overall = Number(m[1]);
    const min = Number(m[2]);
    if (overall >= 10 && overall <= 90 && min >= 10 && min <= 90)
      return { overall, listening: min, reading: min, writing: min, speaking: min, confidence: 95 };
  }

  // P2: "PTE 65 overall with 58 in each skill/band/component"
  m = text.match(
    /pte(?:\s+academic)?[^a-z0-9]{0,40}([\d.]+)\s*overall[^a-z0-9]{0,40}(?:with\s*)?([\d.]+)\s*(?:in\s+each\s+(?:band|skill|component)|each\s+(?:band|skill|component))/i,
  );
  if (m) {
    const overall = Number(m[1]);
    const each = Number(m[2]);
    if (overall >= 10 && overall <= 90 && each >= 10 && each <= 90)
      return { overall, listening: each, reading: each, writing: each, speaking: each, confidence: 90 };
  }

  // P3: All four subscores in order
  m = text.match(
    /pte(?:\s+academic)?.*?overall\s*([\d.]+).*?listening\s*([\d.]+).*?reading\s*([\d.]+).*?writing\s*([\d.]+).*?speaking\s*([\d.]+)/i,
  );
  if (m) {
    return {
      overall: Number(m[1]),
      listening: Number(m[2]),
      reading: Number(m[3]),
      writing: Number(m[4]),
      speaking: Number(m[5]),
      confidence: 92,
    };
  }

  // P4: "PTE overall X" + individual bands
  const overallM = text.match(/pte(?:\s+academic)?.{0,150}?overall\s*([\d.]+)/i);
  if (overallM) {
    const overall = Number(overallM[1]);
    if (overall >= 10 && overall <= 90) {
      const listenM = text.match(/listening\s*([\d.]+)/i);
      const readM = text.match(/reading\s*([\d.]+)/i);
      const writeM = text.match(/writing\s*([\d.]+)/i);
      const speakM = text.match(/speaking\s*([\d.]+)/i);
      return {
        overall,
        listening: listenM ? Number(listenM[1]) : null,
        reading: readM ? Number(readM[1]) : null,
        writing: writeM ? Number(writeM[1]) : null,
        speaking: speakM ? Number(speakM[1]) : null,
        confidence: 70,
      };
    }
  }

  // P5: "PTE: 58" or "PTE Academic: 58"
  const plainM = text.match(/pte(?:\s+academic)?[:\s]+([\d.]+)/i);
  if (plainM) {
    const overall = Number(plainM[1]);
    if (overall >= 10 && overall <= 90)
      return { overall, listening: null, reading: null, writing: null, speaking: null, confidence: 60 };
  }

  return emptyBandScore();
}

// ── TOEFL Parser ──────────────────────────────────────────────────────────────

export function parseToefl(rawText: string): BandScore {
  const text = normalizeWhitespace(rawText);
  if (!text.toLowerCase().includes("toefl")) return emptyBandScore();

  // P1: "TOEFL iBT 79 with no section/band below 18"
  let m = text.match(
    /toefl(?:\s+ibt)?[^a-z0-9]{0,40}(?:overall\s*)?([\d.]+)\s*(?:with\s*)?(?:no\s+(?:band|section|subscore)\s+below|minimum\s+of|no\s+score\s+less\s+than)\s*([\d.]+)/i,
  );
  if (m) {
    const overall = Number(m[1]);
    const min = Number(m[2]);
    if (overall >= 0 && overall <= 120 && min >= 0 && min <= 30)
      return { overall, listening: min, reading: min, writing: min, speaking: min, confidence: 95 };
  }

  // P2: "TOEFL 94 overall with 20 in each section"
  m = text.match(
    /toefl(?:\s+ibt)?[^a-z0-9]{0,40}([\d.]+)\s*overall[^a-z0-9]{0,40}(?:with\s*)?([\d.]+)\s*(?:in\s+each\s+(?:section|component|band)|each\s+(?:section|component|band))/i,
  );
  if (m) {
    const overall = Number(m[1]);
    const each = Number(m[2]);
    if (overall >= 0 && overall <= 120 && each >= 0 && each <= 30)
      return { overall, listening: each, reading: each, writing: each, speaking: each, confidence: 90 };
  }

  // P3: All four subscores in order
  m = text.match(
    /toefl(?:\s+ibt)?.*?overall\s*([\d.]+).*?listening\s*([\d.]+).*?reading\s*([\d.]+).*?writing\s*([\d.]+).*?speaking\s*([\d.]+)/i,
  );
  if (m) {
    return {
      overall: Number(m[1]),
      listening: Number(m[2]),
      reading: Number(m[3]),
      writing: Number(m[4]),
      speaking: Number(m[5]),
      confidence: 92,
    };
  }

  // P4: "TOEFL overall X" + individual sections
  const overallM = text.match(/toefl(?:\s+ibt)?.{0,150}?overall\s*([\d.]+)/i);
  if (overallM) {
    const overall = Number(overallM[1]);
    if (overall >= 0 && overall <= 120) {
      const listenM = text.match(/listening\s*([\d.]+)/i);
      const readM = text.match(/reading\s*([\d.]+)/i);
      const writeM = text.match(/writing\s*([\d.]+)/i);
      const speakM = text.match(/speaking\s*([\d.]+)/i);
      return {
        overall,
        listening: listenM ? Number(listenM[1]) : null,
        reading: readM ? Number(readM[1]) : null,
        writing: writeM ? Number(writeM[1]) : null,
        speaking: speakM ? Number(speakM[1]) : null,
        confidence: 70,
      };
    }
  }

  // P5: "TOEFL iBT: 79" or "TOEFL: 79"
  const plainM = text.match(/toefl(?:\s+ibt)?[:\s]+([\d.]+)/i);
  if (plainM) {
    const overall = Number(plainM[1]);
    if (overall >= 0 && overall <= 120)
      return { overall, listening: null, reading: null, writing: null, speaking: null, confidence: 60 };
  }

  return emptyBandScore();
}

// ── CAE / Cambridge Parser ────────────────────────────────────────────────────

export function parseCae(rawText: string): BandScore {
  const text = normalizeWhitespace(rawText);
  const lower = text.toLowerCase();
  if (!lower.includes("cae") && !lower.includes("cambridge")) return emptyBandScore();

  // "CAE overall X" or "Cambridge score of X" (Cambridge scale: 140–230)
  const overallM =
    text.match(/cae.{0,120}?overall\s*([\d.]+)/i) ||
    text.match(/cambridge(?:\s+english)?.{0,120}?score\s*of\s*([\d.]+)/i) ||
    text.match(/cambridge(?:\s+english)?.{0,120}?(1[4-9][0-9]|2[0-2][0-9]|230)/i);

  if (overallM) {
    const overall = Number(overallM[1]);
    if (overall >= 140 && overall <= 230)
      return { overall, listening: null, reading: null, writing: null, speaking: null, confidence: 70 };
  }

  return emptyBandScore();
}

// ── Duolingo English Test (DET) Parser ────────────────────────────────────────

export function parseDet(rawText: string): SimpleScore {
  const text = normalizeWhitespace(rawText);
  const lower = text.toLowerCase();
  if (!lower.includes("duolingo") && !lower.includes("det")) return emptySimpleScore();

  // "Duolingo score of X" / "DET score of X" — scale: 10–160
  const m =
    text.match(/duolingo(?:\s+english\s+test)?.{0,120}?(?:score\s*of|score:|minimum)\s*([\d.]+)/i) ||
    text.match(/\bdet\b.{0,120}?(?:score\s*of|score:|minimum)\s*([\d.]+)/i) ||
    text.match(/duolingo[:\s]+([\d.]+)/i);

  if (m) {
    const overall = Number(m[1]);
    if (overall >= 10 && overall <= 160) return { overall, confidence: 80 };
  }

  return emptySimpleScore();
}

// ── Other Tests Parser ────────────────────────────────────────────────────────

export function parseOtherTests(rawText: string): OtherTest[] {
  const text = normalizeWhitespace(rawText);
  const results: OtherTest[] = [];

  const patterns: Array<{ name: string; regex: RegExp }> = [
    { name: "OET",         regex: /oet.{0,80}?([a-e](?:\s*in\s*each\s*component)?)/i },
    { name: "TOEIC",       regex: /toeic.{0,80}?([\d]{3,4})/i },
    { name: "LanguageCert", regex: /languagecert.{0,80}?(b[12]|c1|c2|[a-z0-9\s]+)/i },
  ];

  for (const p of patterns) {
    const m = text.match(p.regex);
    if (m) results.push({ name: p.name, score: m[1].trim(), notes: null });
  }

  return results;
}

// ── Universal Entry Point ─────────────────────────────────────────────────────

/**
 * Parse ALL English test requirements from a block of text.
 * Safe to call with any text — returns emptyEnglishResult if nothing found.
 */
export function parseEnglishRequirementsFromText(
  rawText: string | null | undefined,
  source: EnglishRequirementResult["source"],
): EnglishRequirementResult {
  if (!rawText) return emptyEnglishResult(source);
  return {
    source,
    ielts: parseIelts(rawText),
    pte: parsePte(rawText),
    toefl: parseToefl(rawText),
    cae: parseCae(rawText),
    det: parseDet(rawText),
    otherTests: parseOtherTests(rawText),
  };
}

// ── Merge Logic ───────────────────────────────────────────────────────────────

/**
 * Score a BandScore — higher is better (more fields + higher confidence).
 * Used to decide which of two overlapping results to prefer.
 */
function bandScoreWeight(b: BandScore): number {
  return (
    (b.overall != null ? 20 : 0) +
    (b.listening != null ? 15 : 0) +
    (b.reading != null ? 15 : 0) +
    (b.writing != null ? 15 : 0) +
    (b.speaking != null ? 15 : 0) +
    (b.confidence || 0)
  );
}

function pickBetterBandScore(a: BandScore, b: BandScore): BandScore {
  return bandScoreWeight(b) > bandScoreWeight(a) ? b : a;
}

function pickBetterSimpleScore(a: SimpleScore, b: SimpleScore): SimpleScore {
  const wa = (a.overall != null ? 20 : 0) + (a.confidence || 0);
  const wb = (b.overall != null ? 20 : 0) + (b.confidence || 0);
  return wb > wa ? b : a;
}

/**
 * Merge two results — the incoming result fills/improves slots in the base.
 * Best data wins per-field (determined by field completeness + confidence).
 */
export function mergeEnglishResults(
  base: EnglishRequirementResult,
  incoming: EnglishRequirementResult,
): EnglishRequirementResult {
  return {
    source: incoming.source !== "none" ? incoming.source : base.source,
    ielts: pickBetterBandScore(base.ielts, incoming.ielts),
    pte: pickBetterBandScore(base.pte, incoming.pte),
    toefl: pickBetterBandScore(base.toefl, incoming.toefl),
    cae: pickBetterBandScore(base.cae, incoming.cae),
    det: pickBetterSimpleScore(base.det, incoming.det),
    otherTests: [...base.otherTests, ...incoming.otherTests],
  };
}

// ── Apply to Course Data ──────────────────────────────────────────────────────

/**
 * Write the parsed English results back into a course data object.
 * Only fills slots that are still null/undefined — never overwrites.
 */
export function applyEnglishResultToCourse<T extends EnglishCourseFields>(
  course: T,
  english: EnglishRequirementResult,
): void {
  // IELTS
  if (english.ielts.overall != null) {
    if (!course.ieltsOverall)   course.ieltsOverall   = english.ielts.overall;
    if (!course.ieltsListening && english.ielts.listening != null) course.ieltsListening = english.ielts.listening;
    if (!course.ieltsReading   && english.ielts.reading   != null) course.ieltsReading   = english.ielts.reading;
    if (!course.ieltsWriting   && english.ielts.writing   != null) course.ieltsWriting   = english.ielts.writing;
    if (!course.ieltsSpeaking  && english.ielts.speaking  != null) course.ieltsSpeaking  = english.ielts.speaking;
  }
  // PTE
  if (english.pte.overall != null) {
    if (!course.pteOverall)   course.pteOverall   = english.pte.overall;
    if (!course.pteListening && english.pte.listening != null) course.pteListening = english.pte.listening;
    if (!course.pteReading   && english.pte.reading   != null) course.pteReading   = english.pte.reading;
    if (!course.pteWriting   && english.pte.writing   != null) course.pteWriting   = english.pte.writing;
    if (!course.pteSpeaking  && english.pte.speaking  != null) course.pteSpeaking  = english.pte.speaking;
  }
  // TOEFL
  if (english.toefl.overall != null) {
    if (!course.toeflOverall)   course.toeflOverall   = english.toefl.overall;
    if (!course.toeflListening && english.toefl.listening != null) course.toeflListening = english.toefl.listening;
    if (!course.toeflReading   && english.toefl.reading   != null) course.toeflReading   = english.toefl.reading;
    if (!course.toeflWriting   && english.toefl.writing   != null) course.toeflWriting   = english.toefl.writing;
    if (!course.toeflSpeaking  && english.toefl.speaking  != null) course.toeflSpeaking  = english.toefl.speaking;
  }
  // CAE (Cambridge)
  if (english.cae.overall != null && !course.cambridgeOverall) {
    course.cambridgeOverall = english.cae.overall;
  }
  // DET (Duolingo)
  if (english.det.overall != null && !course.duolingoOverall) {
    course.duolingoOverall = english.det.overall;
  }
}

/**
 * One-line summary for logs.
 * e.g. "[ENGLISH] course=X source=browser IELTS=6.5 PTE=58 TOEFL=- CAE=- DET=-"
 */
export function englishResultSummary(courseName: string, result: EnglishRequirementResult): string {
  const fmt = (v: number | null) => (v != null ? String(v) : "-");
  return (
    `[ENGLISH] course="${courseName.slice(0, 50)}" source=${result.source}` +
    ` IELTS=${fmt(result.ielts.overall)}` +
    ` PTE=${fmt(result.pte.overall)}` +
    ` TOEFL=${fmt(result.toefl.overall)}` +
    ` CAE=${fmt(result.cae.overall)}` +
    ` DET=${fmt(result.det.overall)}`
  );
}
