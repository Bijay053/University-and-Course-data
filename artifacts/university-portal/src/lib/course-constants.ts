import taxonomy from "../../../../shared/course-taxonomy.json";

export const CATEGORIES: Record<string, string[]> = taxonomy.categories;

export const CATEGORY_NAMES = Object.keys(CATEGORIES).sort();

export const DEGREE_LEVELS = [
  "Associate Degree or Equivalent",
  "Associate Degree",
  "Certificate",
  "Diploma",
  "Certificate & Diploma",
  "Pathway to Undergraduate",
  "Bachelor",
  "Graduate Certificate",
  "Graduate Diploma",
  "Graduate Certificate & Diploma",
  "Master",
  "Bachelor Dual Degree",
  "Master Dual Degree",
  "Dual Degree",
  "PhD",
  "Doctor/Doctorate",
  "English Language",
];

export const STUDY_MODES = ["On Campus", "Online", "Blended", "Both"];

export const STUDY_LOADS = ["Full Time", "Part Time", "Both"];

export const INTAKE_MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

export const FEE_TERM_OPTIONS = [
  { value: "Annual", label: "Annual (Per Year)" },
  { value: "Semester", label: "Per Semester" },
  { value: "Trimester", label: "Per Trimester" },
  { value: "Term", label: "Per Term" },
  { value: "Session", label: "Per Session" },
  { value: "Quarter", label: "Per Quarter" },
  { value: "Full Course", label: "Full Course (Total)" },
  { value: "Total", label: "Total" },
  { value: "Per Unit", label: "Per Unit" },
  { value: "Per Credit", label: "Per Credit" },
  { value: "Per Credit Hour", label: "Per Credit Hour" },
  { value: "Per Subject", label: "Per Subject" },
  { value: "Per Module", label: "Per Module" },
  { value: "Per Course", label: "Per Course" },
  { value: "Per Month", label: "Per Month" },
  { value: "Per Week", label: "Per Week" },
] as const;

export const FEE_TERMS = FEE_TERM_OPTIONS.map(({ value }) => value);

export const CURRENCIES = ["AUD", "USD", "GBP", "EUR", "NZD", "CAD", "SGD"];

export const ENGLISH_TEST_TYPES = ["IELTS", "PTE", "TOEFL", "Duolingo", "Cambridge", "Other"];

export function getSubCategories(category: string): string[] {
  return CATEGORIES[category] ?? [];
}
