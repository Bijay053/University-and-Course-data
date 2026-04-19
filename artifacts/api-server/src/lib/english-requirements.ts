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

export interface EnglishParseContext {
  courseName?: string | null;
  degreeLevel?: string | null;
}

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

function bandScore(
  overall: number | null,
  each: number | null,
  confidence: number,
): BandScore {
  return {
    overall,
    listening: each,
    reading: each,
    writing: each,
    speaking: each,
    confidence,
  };
}

function normalizeCourseMatcher(input: string | null | undefined): string {
  return normalizeWhitespace(input)
    .toLowerCase()
    .replace(/\([^)]*\)/g, " ")
    .replace(/\bwith\s+specialisations?\b/g, " ")
    .replace(/\bwith\s+specializations?\b/g, " ")
    .replace(/[^a-z0-9]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function scorePattern(value: number): string {
  const normalized = Number(value).toFixed(1);
  if (normalized.endsWith(".0")) {
    return `${parseInt(normalized, 10)}(?:\\.0)?`;
  }
  return normalized.replace(".", "\\.");
}

function looksLikeCsuTieredPolicyPage(rawText: string): boolean {
  const text = normalizeWhitespace(rawText).toLowerCase();
  return (
    text.includes("charles sturt") &&
    text.includes("undergraduate courses") &&
    text.includes("postgraduate coursework courses") &&
    text.includes("higher degree by research courses")
  );
}

function looksLikeKbsEnglishRequirementsPage(rawText: string): boolean {
  const text = normalizeWhitespace(rawText).toLowerCase();
  return (
    text.includes("kaplan business school") &&
    text.includes("bachelor and all postgraduate programs") &&
    text.includes("academic ielts") &&
    text.includes("pte (pearson test of english academic)")
  );
}

export function sharedEnglishPageNeedsCourseContext(rawText: string | null | undefined): boolean {
  if (!rawText) return false;
  return looksLikeCsuTieredPolicyPage(rawText) || looksLikeKbsEnglishRequirementsPage(rawText);
}

function determineCourseTrack(context: EnglishParseContext): "undergraduate" | "postgraduate" | "research" | null {
  const degree = normalizeWhitespace(context.degreeLevel).toLowerCase();
  const courseName = normalizeWhitespace(context.courseName).toLowerCase();
  const combined = `${degree} ${courseName}`.trim();
  if (!combined) return null;
  if (/\b(phd|doctor of philosophy|professional doctoral|doctorate|higher degree by research|master by research)\b/i.test(combined)) {
    return "research";
  }
  if (/\b(master|graduate certificate|graduate diploma|postgraduate)\b/i.test(combined)) {
    return "postgraduate";
  }
  if (/\b(bachelor|associate degree|certificate|diploma|undergraduate)\b/i.test(combined)) {
    return "undergraduate";
  }
  return null;
}

function courseHasHigherEnglishOverride(rawText: string, context: EnglishParseContext): boolean {
  const courseName = normalizeCourseMatcher(context.courseName);
  if (!courseName) return false;
  const lower = rawText.toLowerCase();
  const start = lower.indexOf("courses with higher english language proficiency");
  if (start === -1) return false;

  const nextStarts = [
    lower.indexOf("if you have previous studies in english", start),
    lower.indexOf("if you've completed ielts or another english language test", start),
    lower.indexOf("charles sturt english language proficiency concordance tables", start),
  ].filter((idx) => idx > start);
  const end = nextStarts.length ? Math.min(...nextStarts) : Math.min(rawText.length, start + 8000);
  const section = normalizeCourseMatcher(rawText.slice(start, end));
  return section.includes(courseName);
}

function buildCsuContextualResult(
  rawText: string,
  source: EnglishRequirementResult["source"],
  context: EnglishParseContext,
): EnglishRequirementResult | null {
  if (!looksLikeCsuTieredPolicyPage(rawText)) return null;
  if (courseHasHigherEnglishOverride(rawText, context)) {
    return emptyEnglishResult(source);
  }

  const track = determineCourseTrack(context);
  if (!track) return emptyEnglishResult(source);

  const text = normalizeWhitespace(rawText);
  const branchPatterns: Record<typeof track, RegExp> = {
    undergraduate:
      /for[^a-z0-9]+undergraduate[^a-z0-9]+courses.*?minimum\s+overall\s+score\s+of\s+([\d.]+).*?no\s+individual\s+score\s+below\s+([\d.]+)/i,
    postgraduate:
      /for[^a-z0-9]+postgraduate[^a-z0-9]+coursework[^a-z0-9]+courses.*?minimum\s+overall\s+score\s+of\s+([\d.]+).*?no\s+individual\s+score\s+below\s+([\d.]+)/i,
    research:
      /for[^a-z0-9]+higher[^a-z0-9]+degree[^a-z0-9]+by[^a-z0-9]+research[^a-z0-9]+courses.*?minimum\s+overall\s+score\s+of\s+([\d.]+).*?no\s+individual\s+score\s+below\s+([\d.]+)/i,
  };

  const branchMatch = text.match(branchPatterns[track]);
  if (!branchMatch) return emptyEnglishResult(source);

  const ieltsOverall = Number(branchMatch[1]);
  const ieltsEach = Number(branchMatch[2]);
  const result = emptyEnglishResult(source);
  result.ielts = bandScore(ieltsOverall, ieltsEach, 99);

  const lower = rawText.toLowerCase();
  const concordanceStart = lower.indexOf("charles sturt english language proficiency concordance tables");
  if (concordanceStart === -1) return result;

  const skillHeader = lower.indexOf("individual skill requirement", concordanceStart);
  const overallTable = normalizeWhitespace(
    rawText.slice(concordanceStart, skillHeader > concordanceStart ? skillHeader : rawText.length),
  );
  const skillTable = skillHeader > concordanceStart
    ? normalizeWhitespace(rawText.slice(skillHeader))
    : "";
  const compactOverallTable = overallTable.replace(/\|/g, " ");
  const compactSkillTable = skillTable.replace(/\|/g, " ");

  const overallRow = compactOverallTable.match(
    new RegExp(
      `\\b${scorePattern(ieltsOverall)}\\b\\s+(\\d{2,3})\\s+(\\d{2,3})\\s+(\\d{2,3})\\s+(\\d{2,3})\\s+(?:\\d{3}|N/?A)\\b`,
      "i",
    ),
  );
  if (overallRow) {
    const toeflOverall = Number(overallRow[1]);
    const pteOverall = Number(overallRow[4]);
    if (toeflOverall >= 0 && toeflOverall <= 120) {
      result.toefl.overall = toeflOverall;
      result.toefl.confidence = Math.max(result.toefl.confidence, 95);
    }
    if (pteOverall >= 10 && pteOverall <= 90) {
      result.pte.overall = pteOverall;
      result.pte.confidence = Math.max(result.pte.confidence, 95);
    }
  }

  const skillRow = compactSkillTable.match(
    new RegExp(
      `\\b${scorePattern(ieltsEach)}\\b\\s+(\\d{2,3})\\s+(\\d{2,3})\\s+(\\d{2,3})\\s+(\\d{2,3})\\s+(\\d{1,2})\\s+(\\d{1,2})\\s+(\\d{1,2})\\s+(\\d{1,2})\\s+(\\d{2,3})\\b`,
      "i",
    ),
  );
  if (skillRow) {
    const toeflListening = Number(skillRow[5]);
    const toeflSpeaking = Number(skillRow[6]);
    const toeflReading = Number(skillRow[7]);
    const toeflWriting = Number(skillRow[8]);
    const pteEach = Number(skillRow[9]);
    if ([toeflListening, toeflSpeaking, toeflReading, toeflWriting].every((v) => v >= 0 && v <= 30)) {
      result.toefl.listening = toeflListening;
      result.toefl.speaking = toeflSpeaking;
      result.toefl.reading = toeflReading;
      result.toefl.writing = toeflWriting;
      result.toefl.confidence = Math.max(result.toefl.confidence, 98);
    }
    if (pteEach >= 10 && pteEach <= 90) {
      result.pte.listening = pteEach;
      result.pte.reading = pteEach;
      result.pte.writing = pteEach;
      result.pte.speaking = pteEach;
      result.pte.confidence = Math.max(result.pte.confidence, 98);
    }
  }

  return result;
}

function extractBetween(text: string, startPattern: RegExp, endPattern: RegExp): string {
  const start = text.search(startPattern);
  if (start === -1) return "";
  const rest = text.slice(start);
  const end = rest.search(endPattern);
  return normalizeWhitespace(end === -1 ? rest : rest.slice(0, end));
}

function determineKbsProgramTrack(context: EnglishParseContext): "diploma" | "bachelor_postgraduate" | null {
  const combined = `${normalizeWhitespace(context.degreeLevel)} ${normalizeWhitespace(context.courseName)}`.toLowerCase();
  if (!combined) return null;
  if (/\bdiploma\b/.test(combined) && !/\bgraduate\s+diploma\b/.test(combined)) {
    return "diploma";
  }
  if (/\b(bachelor|master|graduate certificate|graduate diploma|postgraduate)\b/.test(combined)) {
    return "bachelor_postgraduate";
  }
  return null;
}

function buildKbsContextualResult(
  rawText: string,
  source: EnglishRequirementResult["source"],
  context: EnglishParseContext,
): EnglishRequirementResult | null {
  if (!looksLikeKbsEnglishRequirementsPage(rawText)) return null;

  const track = determineKbsProgramTrack(context);
  if (!track) return emptyEnglishResult(source);

  const text = normalizeWhitespace(rawText);
  const result = emptyEnglishResult(source);

  const ieltsRow = extractBetween(
    text,
    /academic\s+ielts,\s*including\s+one\s+skill\s+retake/i,
    /pte\s*\(\s*pearson\s+test\s+of\s+english\s+academic\s*\)/i,
  );
  if (ieltsRow) {
    if (track === "bachelor_postgraduate") {
      const m = ieltsRow.match(
        /overall\s+6(?:\.0)?\s*,\s*with\s+not\s+less\s+than\s+6(?:\.0)?\s+for\s+speaking\s+and\s+writing\s+and\s+5\.5\s+for\s+listening\s+and\s+reading/i,
      );
      if (m) {
        result.ielts = {
          overall: 6.0,
          listening: 5.5,
          reading: 5.5,
          writing: 6.0,
          speaking: 6.0,
          confidence: 99,
        };
      }
    } else {
      const m = ieltsRow.match(/overall\s+5\.5\s*\(\s*with\s+a\s+minimum\s+5\.0\s+in\s+writing\s*\)/i);
      if (m) {
        result.ielts = {
          overall: 5.5,
          listening: null,
          reading: null,
          writing: 5.0,
          speaking: null,
          confidence: 96,
        };
      }
    }
  }

  const pteRow = extractBetween(
    text,
    /pte\s*\(\s*pearson\s+test\s+of\s+english\s+academic\s*\)/i,
    /toefl\s+ibt/i,
  );
  if (pteRow) {
    const pteTarget = track === "bachelor_postgraduate"
      ? pteRow.match(/academic\s+score\s+of\s+50/i)
      : pteRow.match(/academic\s+score\s+of\s+46/i);
    if (pteTarget) {
      const score = track === "bachelor_postgraduate" ? 50 : 46;
      result.pte = {
        overall: score,
        listening: null,
        reading: null,
        writing: null,
        speaking: null,
        confidence: 97,
      };
    }
  }

  const toeflRow = extractBetween(
    text,
    /toefl\s+ibt\s*\(\s*test\s+of\s+english\s+as\s+a\s+foreign\s+language\s*\)/i,
    /kte\s*\(\s*kaplan\s+test\s+of\s+english\s*\)/i,
  );
  if (toeflRow) {
    if (track === "bachelor_postgraduate") {
      const m = toeflRow.match(
        /total\s+band\s+score\s+of\s+72\s+or\s+total\s+score\s+of\s+4\s*\(\s*with\s+a\s+minimum\s+4\.0\s+for\s+speaking\s+and\s+a\s+minimum\s+3\.5\s+for\s+listening,\s*reading\s+and\s+writing\s*\)/i,
      );
      if (m) {
        result.toefl = {
          overall: 72,
          listening: 3.5,
          reading: 3.5,
          writing: 3.5,
          speaking: 4.0,
          confidence: 98,
        };
      }
    } else {
      const m = toeflRow.match(
        /total\s+band\s+score\s+of\s+58\s+or\s+total\s+score\s+of\s+3\.5\s*\(\s*with\s+a\s+minimum\s+3\.0\s+in\s+all\s+papers\s*\)/i,
      );
      if (m) {
        result.toefl = {
          overall: 58,
          listening: 3.0,
          reading: 3.0,
          writing: 3.0,
          speaking: 3.0,
          confidence: 96,
        };
      }
    }
  }

  const caeRow = extractBetween(text, /cae\s*\(\s*cambridge\s+c1\s+advanced\s+test\s*\)/i, /duolingo\s+english\s+test/i);
  if (caeRow && track === "bachelor_postgraduate") {
    const m = caeRow.match(/overall\s+band\s+score\s+of\s+169/i);
    if (m) {
      result.cae = { overall: 169, listening: null, reading: null, writing: null, speaking: null, confidence: 95 };
    }
  }

  const detRow = extractBetween(text, /duolingo\s+english\s+test/i, /oet\s*\(\s*occupational\s+english\s+test\s*\)/i);
  if (detRow) {
    const m = track === "bachelor_postgraduate"
      ? detRow.match(/overall\s+score\s+of\s+110/i)
      : detRow.match(/overall\s+score\s+of\s+100/i);
    if (m) {
      result.det = { overall: track === "bachelor_postgraduate" ? 110 : 100, confidence: 95 };
    }
  }

  return result;
}

// ── IELTS Parser ──────────────────────────────────────────────────────────────

export function parseIelts(rawText: string): BandScore {
  const text = normalizeWhitespace(rawText);
  if (!text.toLowerCase().includes("ielts")) return emptyBandScore();

  // P1: "IELTS[Academic]: Overall X with no [individual] band below Y"
  //     Also handles "IELTS: Overall score X, with no band below Y" and
  //     "IELTS Academic overall score of 6.5, with no band below 6.0" (VIT format)
  let m = text.match(
    /ielts(?:\s+academic)?(?:[^a-z0-9]{0,40}|.{0,40}?)overall\s+(?:score\s+)?(?:of\s+)?([\d.]+)[^.]{0,80}?no\s+(?:individual\s+)?band\s+(?:score\s+)?(?:below|less\s+than|lower\s+than)\s*([\d.]+)/i,
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
    /ielts(?:\s+academic)?(?:[^a-z0-9]{0,40}|.{0,40}?)overall\s*(?:score\s+)?(?:of\s+)?([\d.]+)[^.]{0,80}?(?:minimum\s+of|no\s+score\s+less\s+than|no\s+section\s+below)\s*([\d.]+)/i,
  );
  if (m) {
    const overall = Number(m[1]);
    const min = Number(m[2]);
    if (overall >= 4 && overall <= 9 && min >= 4 && min <= 9)
      return { overall, listening: min, reading: min, writing: min, speaking: min, confidence: 90 };
  }

  // P3b: "Academic IELTS band score of 5.5" / "IELTS band score of 5.5"
  m = text.match(
    /(?:academic\s+)?ielts(?:\s+academic)?[^a-z0-9]{0,40}?(?:band\s+score|score)\s+(?:of\s+)?([4-9](?:\.[05])?)/i,
  );
  if (m) {
    const overall = Number(m[1]);
    if (overall >= 4 && overall <= 9) {
      return {
        overall,
        listening: null,
        reading: null,
        writing: null,
        speaking: null,
        confidence: 72,
      };
    }
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
  const overallM = text.match(/ielts(?:\s+academic)?.{0,180}?overall\s*(?:score\s+)?(?:of\s+)?([\d.]+)/i);
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
    text.match(/(?:minimum\s+)?ielts(?:\s+academic)?(?:[^a-z0-9]{0,60}|.{0,80}?\bscore\s+of\s+)([4-9](?:\.[05])?)/i) ||
    text.match(/(?:academic\s+)?ielts(?:\s+academic)?[^a-z0-9]{0,80}?\bband\s+score\s+of\s+([4-9](?:\.[05])?)/i) ||
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

  // P6: policy-table wording — "PTE (Pearson Test of English Academic) Academic Score of N"
  // ONLY accept this loose match if the value is in a realistic per-course entry-requirement
  // band (>=51). The wording "Academic Score of 50" is universal CEFR-floor boilerplate that
  // almost every Australian university lists for ELICOS/foundation pathways — it is NOT the
  // bachelor/master entry requirement and should NOT pollute every course's PTE field.
  // For real per-course requirements we rely on P1–P5 (explicit "PTE 58 with no skill below 50"
  // wording) or, on image-based pages like ASA, the vision-AI fallback.
  m = text.match(
    /pte(?:\s*\([^)]*\))?(?:\s+academic)?[^0-9]{0,120}?(?:academic\s+)?score\s+of\s+([\d.]+)/i,
  );
  if (m) {
    const overall = Number(m[1]);
    if (overall >= 51 && overall <= 90)
      return { overall, listening: null, reading: null, writing: null, speaking: null, confidence: 60 };
  }

  return emptyBandScore();
}

// ── TOEFL Parser ──────────────────────────────────────────────────────────────

export function parseToefl(rawText: string): BandScore {
  const text = normalizeWhitespace(rawText);
  if (!text.toLowerCase().includes("toefl")) return emptyBandScore();

  // P0: ASA-style policy table — "TOEFL (Test of English as a Foreign Language) Internet
  // Based Test (iBT) 60 Overall" — number is explicitly followed by the word "Overall".
  // This is highest-priority because the "Overall" suffix anchors the score unambiguously.
  let m = text.match(/toefl[^a-z\d]{0,300}?(\d{2,3})\s+overall\b/i);
  if (m) {
    const overall = Number(m[1]);
    if (overall >= 30 && overall <= 120)
      return { overall, listening: null, reading: null, writing: null, speaking: null, confidence: 88 };
  }

  // P1: "TOEFL iBT 79 with no section/band below 18"
  m = text.match(
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

  // P6: policy-table wording like "TOEFL iBT (Test of English as a Foreign Language) 60"
  m = text.match(/toefl(?:\s+ibt)?(?:\s*\([^)]*\))?[^0-9]{0,120}?([\d.]{2,3})\b/i);
  if (m) {
    const overall = Number(m[1]);
    if (overall >= 0 && overall <= 120)
      return { overall, listening: null, reading: null, writing: null, speaking: null, confidence: 72 };
  }

  return emptyBandScore();
}

// ── CAE / Cambridge Parser ────────────────────────────────────────────────────

export function parseCae(rawText: string): BandScore {
  const text = normalizeWhitespace(rawText);
  const lower = text.toLowerCase();
  if (!lower.includes("cae") && !lower.includes("cambridge")) return emptyBandScore();

  // "CAE overall X" or "Cambridge score of X" (Cambridge scale: 140–230)
  // ASA-style: "CAE (Cambridge English: Advanced from Cambridge ESOL) 169"
  const overallM =
    text.match(/cae.{0,120}?overall\s*([\d.]+)/i) ||
    text.match(/cambridge(?:\s+english)?.{0,120}?score\s*of\s*([\d.]+)/i) ||
    text.match(/\bcae\b[^0-9]{0,250}?(1[4-9][0-9]|2[0-2][0-9]|230)\b/i) ||
    text.match(/cambridge(?:\s+english)?.{0,250}?(1[4-9][0-9]|2[0-2][0-9]|230)\b/i);

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
  context: EnglishParseContext = {},
): EnglishRequirementResult {
  if (!rawText) return emptyEnglishResult(source);
  const generic = {
    source,
    ielts: parseIelts(rawText),
    pte: parsePte(rawText),
    toefl: parseToefl(rawText),
    cae: parseCae(rawText),
    det: parseDet(rawText),
    otherTests: parseOtherTests(rawText),
  };

  const contextual = buildCsuContextualResult(rawText, source, context);
  const kbsContextual = buildKbsContextualResult(rawText, source, context);
  if (sharedEnglishPageNeedsCourseContext(rawText)) {
    return kbsContextual ?? contextual ?? emptyEnglishResult(source);
  }
  if (kbsContextual) {
    return mergeEnglishResults(generic, kbsContextual);
  }
  if (contextual) {
    return mergeEnglishResults(generic, contextual);
  }

  return generic;
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
